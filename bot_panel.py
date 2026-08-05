"""Панель управления ботом (!bot): кнопочное управление плейлистами.

Одно общее сообщение-панель на сервер: ui.Select с плейлистами (до 25 на
страницу, пагинация ⬅️➡️) + кнопки ▶ Играть, 🔀 Вперемешку, 📋 Показать,
➕ Создать (модалка), 🗑 Удалить (подтверждение), ❌ Закрыть. Повторный !bot
переоткрывает панель; в новом канале — снимает старую.

Право на ▶/🔀 — бот подключается к голосовому каналу кликера; если бот уже
на связи, кликер должен быть в том же канале (иначе эфемерная ошибка).
остальные кнопки доступны всем. Действия с БД (создание/удаление плейлиста)
выполняются через asyncio.to_thread; проигрывание — engine.play_playlist со
stub-контекстом (прецедент: handle_search_reaction в engine.py).

Модуль не импортирует engine (только duck typing) — конвенция now_playing.py:
engine импортирует этот модуль, круговая зависимость недопустима.
"""

import asyncio
from types import SimpleNamespace

import lolka as discord
from lolka import ui

from permissions_db import deny_text
from ui_utils import esc, fmt_duration, paginate

_SELECT_PAGE_SIZE = 25
_TRACK_PAGE_SIZE = 10
_NOT_SAME_CHANNEL = (
    "Ты не в том же голосовом канале, что и бот — кнопки только для слушателей."
)
_PICK_FIRST = "Сначала выбери плейлист в списке."


class _PanelButton(ui.Button):
    """Кнопка панели/пейджера: делегирует вызов в owner.handle(interaction, action, index)."""

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
        custom_id = f"bot_panel:{action}" if index is None else f"bot_panel:{action}:{index}"
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


class _PanelSelect(ui.Select):
    """Select с плейлистами страницы; делегирует в owner.handle_select."""

    def __init__(self, panel, names: list, default_name, *, placeholder: str):
        super().__init__(
            custom_id=f"bot_panel:select:{panel.guild_id}",
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.panel = panel
        for name in names:
            if len(name) > 100:
                continue
            self.add_option(
                label=name[:80],
                value=name,
                default=(name == default_name),
            )

    async def callback(self, interaction):
        await self.panel.handle_select(interaction, self.values[0])


class BotPanelView(ui.View):
    """Панель управления (одна на глиду, timeout=None).

    Состояние: выбранный плейлист, страница select, подтверждение удаления.
    Пересборка — clear_items + add_item + edit_message(view=self) (паттерн
    PlaylistPickerView); финал — edit_message(view=None) + stop().
    """

    def __init__(self, engine, guild_id: int, panels: dict):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id
        self.panels = panels
        self.message = None
        self.page = 0
        self.pages = 1
        self.selected = None
        self.pending_delete = None
        self.refresh()

    # ---------- построение ----------

    def _names(self) -> list:
        return [name for name, _ in self.engine.db.list_playlists(self.guild_id)]

    def _content(self) -> str:
        if self.pending_delete is not None:
            return (
                f"🗑 Удалить плейлист **{esc(self.pending_delete)}**? "
                "Его треки будут потеряны."
            )
        lines = ["🎛 **Панель управления**", ""]
        if self.selected:
            lines.append(f"Плейлист: **{esc(self.selected)}**")
        else:
            lines.append("Плейлист: не выбран")
        lines += ["", "▶/🔀 — бот подключится к твоему голосовому каналу.", "❌ — закрыть панель."]
        return "\n".join(lines)

    def refresh(self) -> None:
        """Пересобрать элементы под текущее состояние."""
        self.clear_items()
        if self.pending_delete is not None:
            self.add_item(
                _PanelButton(
                    self, "confirm_yes", emoji="✅",
                    label=f"Удалить «{self.pending_delete}»",
                    style=discord.ButtonStyle.danger, row=0,
                )
            )
            self.add_item(_PanelButton(self, "confirm_no", emoji="❌", label="Отмена", row=0))
            return
        names = self._names()
        self.pages = max(1, -(-len(names) // _SELECT_PAGE_SIZE))
        if self.page >= self.pages:
            self.page = self.pages - 1
        chunk, self.pages, has_prev, has_next, _ = paginate(names, self.page, _SELECT_PAGE_SIZE, zero_based=True)
        default_name = None
        nav = self.engine.pl_nav.get(self.guild_id)
        if nav and nav.get("name") in chunk:
            default_name = nav["name"]
        if names:
            placeholder = "🎵 Выбери плейлист…"
        else:
            placeholder = "🎵 Плейлистов нет — создай через ➕"
        select = _PanelSelect(self, chunk, default_name, placeholder=placeholder)
        if not names:
            select.disabled = True
        self.add_item(select)
        self.add_item(
            _PanelButton(
                self, "play", emoji="▶", label="Играть",
                style=discord.ButtonStyle.primary, row=1,
            )
        )
        self.add_item(_PanelButton(self, "shuffle", emoji="🔀", label="Вперемешку", row=1))
        self.add_item(_PanelButton(self, "show", emoji="📋", label="Показать", row=2))
        self.add_item(
            _PanelButton(
                self, "create", emoji="➕", label="Создать",
                style=discord.ButtonStyle.success, row=2,
            )
        )
        self.add_item(
            _PanelButton(
                self, "delete", emoji="🗑", label="Удалить",
                style=discord.ButtonStyle.danger, row=2,
            )
        )
        self.add_item(_PanelButton(self, "close", emoji="❌", style=discord.ButtonStyle.danger, row=2))
        if self.pages > 1:
            self.add_item(
                _PanelButton(self, "page", emoji="⬅️", index=-1, disabled=not has_prev, row=3)
            )
            self.add_item(
                _PanelButton(self, "page", emoji="➡️", index=1, disabled=not has_next, row=3)
            )

    # ---------- диспетчеризация ----------

    async def handle(self, interaction, action: str, index) -> None:
        if action == "page":
            self.page = max(0, min(self.pages - 1, self.page + index))
            self.refresh()
            await self._re_render(interaction)
        elif action == "play":
            await self._play(interaction, shuffle=False)
        elif action == "shuffle":
            await self._play(interaction, shuffle=True)
        elif action == "show":
            await self._show(interaction)
        elif action == "create":
            if not self.engine.has_perm(self.guild_id, interaction.user.id, "playlists"):
                await self._reply_ephemeral(interaction, deny_text("playlists"))
                return
            try:
                await interaction.response.send_modal(PanelNewPlaylistModal(self))
            except Exception:
                pass
        elif action == "delete":
            if not self.engine.has_perm(self.guild_id, interaction.user.id, "playlists"):
                await self._reply_ephemeral(interaction, deny_text("playlists"))
                return
            if self.selected is None:
                await self._reply_ephemeral(interaction, _PICK_FIRST)
                return
            self.pending_delete = self.selected
            self.refresh()
            await self._re_render(interaction)
        elif action == "confirm_yes":
            await self._confirm_delete(interaction)
        elif action == "confirm_no":
            self.pending_delete = None
            self.refresh()
            await self._re_render(interaction)
        elif action == "close":
            self.stop()
            try:
                await interaction.response.edit_message(
                    content="Панель управления закрыта.", view=None
                )
            except Exception:
                pass

    async def handle_select(self, interaction, value: str) -> None:
        self.selected = value
        self.refresh()
        await self._re_render(interaction)

    # ---------- действия ----------

    async def _can_control(self, interaction) -> bool:
        """Проверить право на ▶/🔀.

        Бот не в голосовом — разрешаем: play_playlist сам подключится к
        каналу кликера (ensure_voice). Бот уже на связи — кликер должен
        быть в том же канале, чтобы не угонять чужую сессию.
        """
        guild = self.engine.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild is not None else None
        if vc is None or vc.channel is None:
            return True
        user_vc = getattr(interaction.user, "voice", None)
        if (
            user_vc is None
            or user_vc.channel is None
            or user_vc.channel.id != vc.channel.id
        ):
            await self._reply_ephemeral(interaction, _NOT_SAME_CHANNEL)
            return False
        return True

    async def _play(self, interaction, *, shuffle: bool) -> None:
        if not await self._can_control(interaction):
            return
        if self.selected is None:
            await self._reply_ephemeral(interaction, _PICK_FIRST)
            return
        ctx = SimpleNamespace(
            guild=interaction.guild,
            channel=interaction.channel,
            message=SimpleNamespace(guild=interaction.guild, author=interaction.user),
            send=lambda text: interaction.channel.send(text),
        )
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await self.engine.play_playlist(ctx, self.selected, shuffle=shuffle)

    async def _show(self, interaction) -> None:
        if self.selected is None:
            await self._reply_ephemeral(interaction, _PICK_FIRST)
            return
        try:
            await interaction.response.defer()
        except Exception:
            pass
        tracks = await asyncio.to_thread(
            self.engine.db.get_playlist, self.guild_id, self.selected
        ) or []
        pager = TrackListPagerView(self.engine, self.guild_id, self.selected, tracks)
        try:
            msg = await interaction.followup.send(
                content=pager._render(), view=pager, ephemeral=True
            )
            pager.message = msg
        except Exception:
            pass

    async def _confirm_delete(self, interaction) -> None:
        name = self.pending_delete
        self.pending_delete = None
        if name is None:
            self.refresh()
            await self._re_render(interaction)
            return
        try:
            await interaction.response.defer()
        except Exception:
            pass
        ok = await asyncio.to_thread(self.engine.db.delete_playlist, self.guild_id, name)
        if self.selected == name:
            self.selected = None
        self.page = 0
        self.refresh()
        try:
            await interaction.message.edit(content=self._content(), view=self)
            await interaction.followup.send(
                f"Плейлист **{esc(name)}** удалён."
                if ok
                else f"Плейлист **{esc(name)}** не найден — он мог быть удалён ранее.",
                ephemeral=True,
            )
        except Exception:
            pass

    async def _re_render(self, interaction) -> None:
        """Перерисовать панель с обновлённым текстом и кнопками."""
        try:
            await interaction.response.edit_message(content=self._content(), view=self)
        except Exception:
            pass

    async def _reply_ephemeral(self, interaction, text: str) -> None:
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass


class PanelNewPlaylistModal(ui.Modal):
    """Создать плейлист через панель (без добавления треков)."""

    name = ui.TextInput(
        label="Название", placeholder="например: качалка", min_length=1, max_length=100
    )

    def __init__(self, panel: BotPanelView):
        super().__init__(title="Новый плейлист")
        self.panel = panel

    async def on_submit(self, interaction) -> None:
        name = self.name.value.strip()
        if not name:
            try:
                await interaction.response.send_message(
                    "Название не может быть пустым.", ephemeral=True
                )
            except Exception:
                pass
            return
        engine = self.panel.engine
        ok = await asyncio.to_thread(engine.db.create_playlist, self.panel.guild_id, name)
        text = (
            f"Создал плейлист **{esc(name)}**."
            if ok
            else f"Плейлист **{esc(name)}** уже существует."
        )
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass
        panel = self.panel
        if panel.is_finished():
            panel = (panel.panels or {}).get(panel.guild_id)
        if panel is not None and not panel.is_finished():
            if panel.selected is None:
                panel.selected = name
            panel.page = 0
            panel.refresh()
            msg = panel.message
            if msg is not None:
                try:
                    await msg.edit(content=panel._content(), view=panel)
                except Exception:
                    pass


class TrackListPagerView(ui.View):
    """Эфемерный список треков плейлиста с пагинацией (10 на страницу)."""

    def __init__(self, engine, guild_id: int, name: str, tracks: list):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id
        self.name = name
        self.message = None
        self.page = 1
        self.pages = 1
        self.tracks = list(tracks)
        self.refresh()

    def _render(self) -> str:
        chunk, self.pages, _, _, start = paginate(self.tracks, self.page, _TRACK_PAGE_SIZE)
        if not self.tracks:
            return f"Плейлист **{esc(self.name)}** пуст."
        lines = [
            f"{i}. {esc(title)}" + (f" ({fmt_duration(d)})" if d else "")
            for i, (_, title, d) in enumerate(chunk, start=start)
        ]
        header = f"Плейлист **{esc(self.name)}** ({len(self.tracks)} треков)"
        if self.pages > 1:
            header += f" — стр. {self.page}/{self.pages}"
        return header + ":\n" + "\n".join(lines)

    def refresh(self) -> None:
        _, self.pages, _, _, _ = paginate(self.tracks, self.page, _TRACK_PAGE_SIZE)
        self.clear_items()
        has_prev = self.page > 1
        has_next = self.page < self.pages
        self.add_item(
            _PanelButton(self, "page", emoji="⬅️", index=-1, disabled=not has_prev, row=0)
        )
        self.add_item(
            _PanelButton(self, "close", emoji="❌", style=discord.ButtonStyle.danger, row=0)
        )
        self.add_item(
            _PanelButton(self, "page", emoji="➡️", index=1, disabled=not has_next, row=0)
        )

    async def handle(self, interaction, action: str, index) -> None:
        if action == "page":
            self.page = max(1, min(self.pages, self.page + index))
            self.refresh()
            try:
                await interaction.response.edit_message(
                    content=self._render(), view=self
                )
            except Exception:
                pass
        elif action == "close":
            self.stop()
            try:
                await interaction.response.edit_message(
                    content="Список закрыт.", view=None
                )
            except Exception:
                pass


async def open_panel(engine, guild_id: int, channel, panels: dict) -> None:
    """Открыть (или переоткрыть) панель управления в канале.

    Если панель уже открыта на сервере — снять её кнопки и stop() старую
    вью (клики по ней больше не диспатчатся), затем отправить новую.
    """
    old = panels.get(guild_id)
    if old is not None:
        old.stop()
        msg = old.message
        if msg is not None:
            try:
                await msg.edit(view=None)
            except Exception:
                pass
    panel = BotPanelView(engine, guild_id, panels)
    try:
        msg = await channel.send(content=panel._content(), view=panel)
        panel.message = msg
        panels[guild_id] = panel
    except Exception:
        panel.stop()
