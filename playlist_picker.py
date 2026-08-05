"""Кнопка ➕ «В плейлист»: добавление трека в плейлист без команд.

Два входа:
  * сообщение «Сыграю: ...» после выбора результата поиска реакцией
    (AddToPlaylistView прикрепляется в engine.handle_search_reaction);
  * панель «Сейчас играет» (кнопка ➕ в NowPlayingView) — текущий трек.

Обе точки открывают пикер PlaylistPickerView в ephemeral-сообщении:
кнопка на каждый плейлист (до _PAGE_SIZE на страницу, пагинация ⬅️➡️),
«✨ Создать» — модалка NewPlaylistModal, «❌» — отмена. Дубли пропускаются
через db.has_track (как автодобавление в default из #music).

Модуль не импортирует engine (только duck typing) — engine импортирует
этот модуль, круговая зависимость недопустима (конвенция now_playing.py).
"""

import asyncio

import lolka as discord
from lolka import ui

from permissions_db import deny_text
from ui_utils import esc, paginate

_PAGE_SIZE = 20
_OWNER_ONLY = "Эта кнопка только для автора поиска."


def add_track_dedupe(db, guild_id: int, name: str, track: dict) -> str:
    """Добавить трек в плейлист без дублей (по каноническому page_url).

    Статусы: "added" — добавлен; "dup" — уже был (БД не тронута);
    "missing" — плейлист не существует.
    """
    if db.has_track(guild_id, name, track["page_url"]):
        return "dup"
    if db.add_track(guild_id, name, track["page_url"], track["title"], track.get("duration")):
        return "added"
    return "missing"


class _PickerButton(ui.Button):
    """Кнопка пикера: делегирует вызов в picker.handle(interaction, action, index)."""

    def __init__(
        self,
        picker,
        action: str,
        *,
        label=None,
        emoji=None,
        index=None,
        disabled: bool = False,
        style=None,
        row=None,
    ):
        custom_id = f"pl_pick:{action}" if index is None else f"pl_pick:{action}:{index}"
        super().__init__(
            label=label[:80] if label else None,
            emoji=emoji,
            custom_id=custom_id,
            style=style or discord.ButtonStyle.secondary,
            disabled=disabled,
            row=row,
        )
        self.picker = picker
        self.action = action
        self.index = index

    async def callback(self, interaction):
        await self.picker.handle(interaction, self.action, self.index)


class PlaylistPickerView(ui.View):
    """Выбор плейлиста для трека (живёт в ephemeral-сообщении).

    Тот же объект view пересобирается при смене страницы (clear_items +
    add_item + edit_message(view=self)); финал — edit_message(view=None) +
    stop() (ViewStore снимает отслеживание, кнопки исчезают).
    """

    def __init__(self, engine, guild_id: int, user_id: int, track: dict, names: list):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id
        self.user_id = user_id
        self.track = track
        self.names = list(names)
        self.page = 0
        self.pages = 1
        self.message = None
        self.refresh()

    def refresh(self) -> None:
        """Пересобрать кнопки под текущую страницу."""
        self.clear_items()
        chunk, self.pages, has_prev, has_next, _ = paginate(self.names, self.page, _PAGE_SIZE, zero_based=True)
        base = self.page * _PAGE_SIZE
        for i, name in enumerate(chunk or []):
            self.add_item(_PickerButton(self, "pick", label=name, index=base + i))
        self.add_item(_PickerButton(self, "page", emoji="⬅️", index=-1, disabled=not has_prev, row=4))
        self.add_item(
            _PickerButton(
                self, "create", emoji="✨", label="Создать",
                style=discord.ButtonStyle.success, row=4,
            )
        )
        self.add_item(_PickerButton(self, "cancel", emoji="❌", style=discord.ButtonStyle.danger, row=4))
        self.add_item(_PickerButton(self, "page", emoji="➡️", index=1, disabled=not has_next, row=4))

    async def handle(self, interaction, action: str, index) -> None:
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message(_OWNER_ONLY, ephemeral=True)
            except Exception:
                pass
            return
        if action == "pick":
            await self._pick(interaction, index)
        elif action == "page":
            self.page = max(0, min(self.pages - 1, self.page + index))
            self.refresh()
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
        elif action == "create":
            try:
                await interaction.response.send_modal(NewPlaylistModal(self, self.track))
            except Exception:
                pass
        elif action == "cancel":
            await self._finish(interaction, "Отменено.")

    async def _pick(self, interaction, index) -> None:
        if index is None or index < 0 or index >= len(self.names):
            await self._finish(interaction, "Плейлист не найден — обнови список.")
            return
        name = self.names[index]
        status = await asyncio.to_thread(
            add_track_dedupe, self.engine.db, self.guild_id, name, self.track
        )
        if status == "added":
            text = f"Добавил в плейлист **{esc(name)}**: **{esc(self.track['title'])}**"
            if self.engine.on_playlist_track_added(
                self.guild_id, name, self.track["page_url"], self.track["title"],
                self.track.get("duration"),
            ):
                text += " (поставил в конец очереди)"
        elif status == "dup":
            text = f"**{esc(self.track['title'])}** уже есть в плейлисте **{esc(name)}**"
        else:
            text = f"Плейлист **{esc(name)}** не найден — он мог быть удалён."
        await self._finish(interaction, text)

    async def _finish(self, interaction, text: str) -> None:
        try:
            await interaction.response.edit_message(content=text, view=None)
        except Exception:
            pass
        self.stop()


class NewPlaylistModal(ui.Modal):
    """Создать плейлист и сразу добавить в него выбранный трек."""

    name = ui.TextInput(
        label="Название", placeholder="например: качалка", min_length=1, max_length=100
    )

    def __init__(self, picker: PlaylistPickerView, track: dict):
        super().__init__(title="Новый плейлист")
        self.picker = picker
        self.track = track

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
        engine = self.picker.engine
        guild_id = self.picker.guild_id
        await asyncio.to_thread(engine.db.create_playlist, guild_id, name)
        status = await asyncio.to_thread(add_track_dedupe, engine.db, guild_id, name, self.track)
        if status == "added":
            text = f"Создал плейлист **{esc(name)}** и добавил: **{esc(self.track['title'])}**"
        elif status == "dup":
            text = f"**{esc(self.track['title'])}** уже есть в плейлисте **{esc(name)}**"
        else:
            text = "Не удалось добавить трек."
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass
        picker = self.picker
        msg = picker.message
        if msg is not None:
            try:
                await msg.edit(content=text, view=None)
            except Exception:
                pass
        picker.stop()


class AddToPlaylistView(ui.View):
    """Кнопка ➕ на сообщении «Сыграю: ...» после выбора трека из поиска.

    Открывает пикер плейлистов (open_picker); доступна только автору поиска.
    """

    def __init__(self, engine, guild_id: int, user_id: int, track: dict):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id
        self.user_id = user_id
        self.track = track

    @ui.button(emoji="➕", label="В плейлист", style=discord.ButtonStyle.secondary)
    async def add_button(self, interaction, button):
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message(_OWNER_ONLY, ephemeral=True)
            except Exception:
                pass
            return
        await open_picker(interaction, self.engine, self.guild_id, self.user_id, self.track)


async def open_picker(interaction, engine, guild_id: int, user_id: int, track: dict) -> None:
    """Открыть пикер плейлистов для трека (ephemeral-сообщение с кнопками).

    Единая точка всех входов «добавить в плейлист» (AddToPlaylistView и
    кнопка ➕ в NowPlayingView) — проверка права playlists закрывает оба.
    Права проверяются синхронно через has_perm (см. комментарий в
    now_playing._can_control).

    Ссылка на сообщение пикера сохраняется в picker.message — по ней модалка
    NewPlaylistModal гасит пикер после создания плейлиста.
    """
    if not engine.has_perm(guild_id, user_id, "playlists"):
        try:
            await interaction.response.send_message(deny_text("playlists"), ephemeral=True)
        except Exception:
            pass
        return
    names = [name for name, _ in engine.db.list_playlists(guild_id)]
    picker = PlaylistPickerView(engine, guild_id, user_id, track, names)
    header = f"Куда добавить **{esc(track['title'])}**?"
    try:
        await interaction.response.defer()
        msg = await interaction.followup.send(content=header, view=picker, ephemeral=True)
        picker.message = msg
    except Exception:
        pass
