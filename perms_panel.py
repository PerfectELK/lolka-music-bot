"""Панель прав бота (!perms): выдать/забрать права на управление ботом.

Панель открывается по префиксной команде: Lolka умеет ephemeral только в
интеракциях (lolka.py для префиксных команд interaction не создаёт), поэтому
панель отправляется обычным сообщением в канал команды. Кнопки/селекты —
обычные компоненты (клики приходят как INTERACTION_CREATE), право клика
проверяется по engine.has_perm(guild_id, user.id, "permissions") — иначе
ephemeral-отказ deny_text.

Флоу «Выдать»: Select участников глиды (до 25 на страницу, исключая ботов и
владельца) → Select прав (multi) → grant → подтверждение + перерисовка.
Флоу «Забрать»: Select пользователей с ≥1 правом (исключая владельца) →
Select прав (multi) → revoke → подтверждение + перерисовка. ❌ в флоу —
возврат к списку.

Модуль не импортирует engine (только duck typing) — конвенция now_playing.py:
engine импортирует этот модуль, круговая зависимость недопустима.
"""

import asyncio
import logging

import lolka as discord
from lolka import ui

from permissions_db import PERM_LABELS, PERM_NAMES, deny_text
from ui_utils import esc

_log = logging.getLogger("music_bot")

_PAGE_SIZE = 10
_SELECT_PAGE_SIZE = 25

# Короткие ярлыки для списка выданных прав (полные — в PERM_LABELS).
_PERM_SHORT = {
    "control": "🎛 Управление",
    "playlists": "🎵 Плейлисты",
    "permissions": "👑 Права",
}


class _PermsButton(ui.Button):
    """Кнопка панели прав: делегирует в owner.handle(interaction, action, index)."""

    def __init__(
        self,
        owner,
        action: str,
        *,
        label=None,
        emoji=None,
        style=None,
        disabled: bool = False,
        row=None,
        index=None,
    ):
        custom_id = f"perms_panel:{action}" if index is None else f"perms_panel:{action}:{index}"
        super().__init__(
            label=label[:80] if label else None,
            emoji=emoji,
            custom_id=custom_id,
            style=style or discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self.owner = owner
        self.action = action
        self.index = index

    async def callback(self, interaction):
        await self.owner.handle(interaction, self.action, self.index)


class _PermsSelect(ui.Select):
    """Select панели прав; options — пары (value, label), value — user_id или право."""

    def __init__(self, owner, action: str, options: list, *, placeholder: str, row: int = 0):
        super().__init__(
            custom_id=f"perms_panel:select:{action}",
            placeholder=placeholder,
            min_values=1,
            max_values=max(1, len(options)),
            row=row,
        )
        self.owner = owner
        self.action = action
        for value, label in options:
            # value обязан быть строкой (API Lolka/Discord): id участников —
            # числа, без str() API отвечает 400 Invalid request body
            self.add_option(label=label[:80], value=str(value))

    async def callback(self, interaction):
        await self.owner.handle_select(interaction, self.action, self.values)


class PermissionsPanelView(ui.View):
    """Панель прав (timeout=None, пересборка clear_items + add_item + edit).

    Состояние: mode (list | grant_member | grant_perm | revoke_user |
    revoke_perm), страницы списка и селектов, выбранные пользователи.
    """

    def __init__(self, engine, guild_id: int):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id
        self.message = None
        self.mode = "list"
        self.page = 0
        self.pages = 1
        self.member_page = 0
        self.member_pages = 1
        self.revoke_page = 0
        self.revoke_pages = 1
        self.pending_user = None
        self.pending_revoke_user = None
        self.refresh()

    # ---------- данные ----------

    def _member_name(self, user_id) -> str:
        guild = self.engine.bot.get_guild(self.guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return member.display_name or str(user_id)
        return str(user_id)

    def _grant_rows(self) -> list:
        """Список (имя, права) по всем выданным правам, без владельца."""
        owner = self.engine.perms.get_owner(self.guild_id)
        rows = []
        for uid, perms in self.engine.perms.all_grants(self.guild_id).items():
            if uid == owner or not perms:
                continue
            rows.append((self._member_name(uid), perms))
        return rows

    def _grantable_members(self) -> list:
        """Участники глиды для выдачи: без ботов и без владельца."""
        guild = self.engine.bot.get_guild(self.guild_id)
        if guild is None:
            return []
        owner = self.engine.perms.get_owner(self.guild_id)
        return [
            (m.id, m.display_name or str(m.id))
            for m in sorted(guild.members, key=lambda m: m.display_name.lower())
            if not m.bot and m.id != owner
        ]

    def _revokable_users(self) -> list:
        """Пользователи с ≥1 правом (без владельца) для отзыва."""
        owner = self.engine.perms.get_owner(self.guild_id)
        users = [
            (uid, self._member_name(uid))
            for uid, perms in self.engine.perms.all_grants(self.guild_id).items()
            if uid != owner and perms
        ]
        return sorted(users, key=lambda u: u[1].lower())

    # ---------- построение ----------

    def _content(self) -> str:
        lines = ["🔐 **Права бота**", ""]
        owner = self.engine.perms.get_owner(self.guild_id)
        lines.append(f"👑 **{esc(self._member_name(owner))}** — владелец (все права)")
        lines.append("")
        if self.mode == "list":
            rows = self._grant_rows()
            if not rows:
                lines.append("Права ещё никому не выданы.")
            else:
                chunk = rows[self.page * _PAGE_SIZE:(self.page + 1) * _PAGE_SIZE]
                for name, perms in chunk:
                    labels = ", ".join(_PERM_SHORT[p] for p in PERM_NAMES if p in perms)
                    lines.append(f"{esc(name)} — {labels}")
                if self.pages > 1:
                    lines.append(f"Стр. {self.page + 1}/{self.pages}")
        elif self.mode == "grant_member":
            lines.append("Кому выдать права?")
        elif self.mode == "grant_perm":
            lines.append(f"Какие права выдать **{esc(self._member_name(self.pending_user))}**?")
        elif self.mode == "revoke_user":
            lines.append("У кого забрать права?")
        elif self.mode == "revoke_perm":
            lines.append(
                f"Какие права забрать у **{esc(self._member_name(self.pending_revoke_user))}**?"
            )
        lines += ["", "Выдать права: ➕"]
        return "\n".join(lines)

    def refresh(self) -> None:
        """Пересобрать элементы под текущее состояние."""
        self.clear_items()
        if self.mode == "list":
            rows = self._grant_rows()
            self.pages = max(1, -(-len(rows) // _PAGE_SIZE))
            if self.page >= self.pages:
                self.page = self.pages - 1
            self.add_item(
                _PermsButton(
                    self, "grant", emoji="➕", label="Выдать",
                    style=discord.ButtonStyle.success, row=0,
                )
            )
            self.add_item(
                _PermsButton(
                    self, "revoke", emoji="🗑", label="Забрать",
                    style=discord.ButtonStyle.danger, row=0,
                )
            )
            self.add_item(
                _PermsButton(self, "close", emoji="❌", style=discord.ButtonStyle.danger, row=0)
            )
            if self.pages > 1:
                self.add_item(
                    _PermsButton(self, "page", emoji="⬅️", index=-1, disabled=self.page == 0, row=1)
                )
                self.add_item(
                    _PermsButton(
                        self, "page", emoji="➡️", index=1,
                        disabled=self.page + 1 >= self.pages, row=1,
                    )
                )
        elif self.mode == "grant_member":
            members = self._grantable_members()
            self.member_pages = max(1, -(-len(members) // _SELECT_PAGE_SIZE))
            if self.member_page >= self.member_pages:
                self.member_page = self.member_pages - 1
            chunk = members[self.member_page * _SELECT_PAGE_SIZE:
                            (self.member_page + 1) * _SELECT_PAGE_SIZE]
            if chunk:
                select = _PermsSelect(self, "grant_member", chunk, placeholder="Кому выдать права?")
                self.add_item(select)
            else:
                # Пустой Select невалиден для API — показываем заглушку
                self.add_item(
                    _PermsButton(
                        self, "none", label="Нет участников для выдачи",
                        disabled=True, row=0,
                    )
                )
            has_prev = self.member_page > 0
            has_next = self.member_page + 1 < self.member_pages
            self.add_item(
                _PermsButton(
                    self, "member_page", emoji="⬅️", index=-1,
                    disabled=not has_prev, row=1,
                )
            )
            self.add_item(
                _PermsButton(self, "cancel", emoji="❌", style=discord.ButtonStyle.danger, row=1)
            )
            self.add_item(
                _PermsButton(
                    self, "member_page", emoji="➡️", index=1,
                    disabled=not has_next, row=1,
                )
            )
        elif self.mode == "grant_perm":
            select = _PermsSelect(
                self,
                "grant_perm",
                [(p, PERM_LABELS[p]) for p in PERM_NAMES],
                placeholder="Какие права выдать?",
            )
            self.add_item(select)
            self.add_item(
                _PermsButton(self, "cancel", emoji="❌", style=discord.ButtonStyle.danger, row=1)
            )
        elif self.mode == "revoke_user":
            users = self._revokable_users()
            self.revoke_pages = max(1, -(-len(users) // _SELECT_PAGE_SIZE))
            if self.revoke_page >= self.revoke_pages:
                self.revoke_page = self.revoke_pages - 1
            chunk = users[self.revoke_page * _SELECT_PAGE_SIZE:
                          (self.revoke_page + 1) * _SELECT_PAGE_SIZE]
            if chunk:
                select = _PermsSelect(self, "revoke_user", chunk, placeholder="У кого забрать права?")
                self.add_item(select)
            else:
                # Пустой Select невалиден для API — показываем заглушку
                self.add_item(
                    _PermsButton(
                        self, "none", label="Нет прав для отзыва",
                        disabled=True, row=0,
                    )
                )
            has_prev = self.revoke_page > 0
            has_next = self.revoke_page + 1 < self.revoke_pages
            self.add_item(
                _PermsButton(
                    self, "revoke_page", emoji="⬅️", index=-1,
                    disabled=not has_prev, row=1,
                )
            )
            self.add_item(
                _PermsButton(self, "cancel", emoji="❌", style=discord.ButtonStyle.danger, row=1)
            )
            self.add_item(
                _PermsButton(
                    self, "revoke_page", emoji="➡️", index=1,
                    disabled=not has_next, row=1,
                )
            )
        elif self.mode == "revoke_perm":
            perms = self.engine.perms.grants_for(
                self.guild_id, self.pending_revoke_user
            ) if self.pending_revoke_user else set()
            if perms:
                select = _PermsSelect(
                    self,
                    "revoke_perm",
                    [(p, PERM_LABELS[p]) for p in sorted(perms)],
                    placeholder="Какие права забрать?",
                )
                self.add_item(select)
            else:
                # Пустой Select невалиден для API — показываем заглушку
                self.add_item(
                    _PermsButton(
                        self, "none", label="Нет прав для отзыва",
                        disabled=True, row=0,
                    )
                )
            self.add_item(
                _PermsButton(self, "cancel", emoji="❌", style=discord.ButtonStyle.danger, row=1)
            )

    # ---------- диспетчеризация ----------

    async def _can_use(self, interaction) -> bool:
        if self.engine.has_perm(self.guild_id, interaction.user.id, "permissions"):
            return True
        try:
            await interaction.response.send_message(deny_text("permissions"), ephemeral=True)
        except Exception:
            pass
        return False

    async def handle(self, interaction, action: str, index) -> None:
        if not await self._can_use(interaction):
            return
        if action == "grant":
            self.mode = "grant_member"
            self.member_page = 0
            self.refresh()
            await self._re_render(interaction)
        elif action == "revoke":
            self.mode = "revoke_user"
            self.revoke_page = 0
            self.refresh()
            await self._re_render(interaction)
        elif action == "close":
            self.stop()
            try:
                await interaction.response.edit_message(content="Панель прав закрыта.", view=None)
            except Exception:
                pass
        elif action == "cancel":
            self.mode = "list"
            self.refresh()
            await self._re_render(interaction)
        elif action == "page":
            self.page = max(0, min(self.pages - 1, self.page + index))
            self.refresh()
            await self._re_render(interaction)
        elif action == "member_page":
            self.member_page = max(0, min(self.member_pages - 1, self.member_page + index))
            self.refresh()
            await self._re_render(interaction)
        elif action == "revoke_page":
            self.revoke_page = max(0, min(self.revoke_pages - 1, self.revoke_page + index))
            self.refresh()
            await self._re_render(interaction)

    async def handle_select(self, interaction, action: str, values) -> None:
        if not await self._can_use(interaction):
            return
        if action == "grant_member":
            self.pending_user = int(values[0])
            self.mode = "grant_perm"
            self.refresh()
            await self._re_render(interaction)
        elif action == "grant_perm":
            uid = self.pending_user
            perms = list(values)
            await asyncio.to_thread(self.engine.perms.grant, self.guild_id, uid, perms)
            self.mode = "list"
            self.refresh()
            await self._re_render(interaction)
            labels = ", ".join(esc(PERM_LABELS[p]) for p in perms)
            try:
                await interaction.followup.send(
                    f"Выдал права **{esc(self._member_name(uid))}**: {labels}",
                    ephemeral=True,
                )
            except Exception:
                pass
        elif action == "revoke_user":
            self.pending_revoke_user = int(values[0])
            self.mode = "revoke_perm"
            self.refresh()
            await self._re_render(interaction)
        elif action == "revoke_perm":
            uid = self.pending_revoke_user
            perms = list(values)
            await asyncio.to_thread(self.engine.perms.revoke, self.guild_id, uid, perms)
            self.mode = "list"
            self.refresh()
            await self._re_render(interaction)
            labels = ", ".join(esc(PERM_LABELS[p]) for p in perms)
            try:
                await interaction.followup.send(
                    f"Забрал права у **{esc(self._member_name(uid))}**: {labels}",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def _re_render(self, interaction) -> None:
        try:
            await interaction.response.edit_message(content=self._content(), view=self)
        except Exception as exc:
            _log.warning(
                "perms_panel: не удалось перерисовать панель (guild=%s, mode=%s): %s",
                self.guild_id, self.mode, exc,
            )


async def open_perms_panel(engine, ctx, guild_id: int) -> None:
    """Открыть панель прав (!perms).

    Убеждаемся, что владелец известен (ensure_owner); если БД пуста и для
    владельца (фолбэк-фолбэк) — назначаем первого открывшего панель
    (source="first_perms").

    Панель отправляется эфемерной, если команда пришла из интеракции
    (ctx.interaction), иначе — обычным сообщением в канал (префиксные
    команды lolka.py интеракций не создают, а Lolka умеет ephemeral только
    в интеракциях).
    """
    await engine.ensure_owner(ctx.guild)
    if engine.perms.get_owner(guild_id) is None:
        await asyncio.to_thread(
            engine.perms.set_owner, guild_id, ctx.author.id, "first_perms"
        )
    panel = PermissionsPanelView(engine, guild_id)
    interaction = getattr(ctx, "interaction", None)
    try:
        if interaction is not None:
            await interaction.response.send_message(
                content=panel._content(), view=panel, ephemeral=True
            )
            msg = await interaction.original_response()
        else:
            msg = await ctx.channel.send(content=panel._content(), view=panel)
        panel.message = msg
    except Exception as exc:
        _log.warning("perms_panel: не удалось отправить панель (guild=%s): %s", guild_id, exc)
        panel.stop()
