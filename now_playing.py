"""Кнопки управления на сообщении «Сейчас играет»: ⏮ ⏯ ⏭ 🔁 ⏹.

Клики прилетают как INTERACTION_CREATE и диспатчатся в view store
(lolka/state.py: parse_interaction_create). Движок редактирует сообщение
через report() без передачи view — компоненты при этом сохраняются
(Message.edit не трогает components, если view не передан).

Право клика: участники того же голосового канала, что и бот, с правом
control (кнопка ➕ — право playlists); иначе — ephemeral-сообщение об ошибке
(deny_text). Тихие подтверждения (defer, тип 6) — для действий, которые сами
обновят текст через report(); для 🔁 используется ответ edit_message (тип 7)
с пометкой состояния.
"""

import lolka as discord
from lolka import ui

from permissions_db import deny_text
from playlist_picker import open_picker

_NOT_IN_VOICE = "Бот не в голосовом канале."
_NOT_SAME_CHANNEL = (
    "Ты не в том же голосовом канале, что и бот — кнопки только для слушателей."
)
_NO_PREV = "Нет предыдущего трека."
_NOTHING_PLAYING = "Сейчас ничего не играет."
_LAST_TRACK = "Это был последний трек плейлиста."


class NowPlayingView(ui.View):
    """Панель плеера под «Сейчас играет» (одна на глиду, timeout=None).

    Конструктор принимает движок и guild_id; engine импортирует этот класс,
    поэтому здесь нет импорта engine (только duck typing).
    """

    def __init__(self, engine, guild_id: int):
        super().__init__(timeout=None)
        self.engine = engine
        self.guild_id = guild_id

    def _vc(self):
        guild = self.engine.bot.get_guild(self.guild_id)
        return guild.voice_client if guild is not None else None

    async def _can_control(self, interaction) -> bool:
        """Проверить право на клик; при отказе ответить ephemeral и вернуть False.

        Сначала голосовой канал (чтобы не выдавать лишнего посторонним),
        затем право control. Права проверяются синхронно через has_perm:
        панель «Сейчас играет» могла быть создана до резолва владельца —
        владелец просто получит отказ до первого !perms (приемлемо).
        """
        vc = self._vc()
        if vc is None or vc.channel is None:
            try:
                await interaction.response.send_message(_NOT_IN_VOICE, ephemeral=True)
            except Exception:
                pass
            return False
        user_vc = getattr(interaction.user, "voice", None)
        if (
            user_vc is None
            or user_vc.channel is None
            or user_vc.channel.id != vc.channel.id
        ):
            try:
                await interaction.response.send_message(_NOT_SAME_CHANNEL, ephemeral=True)
            except Exception:
                pass
            return False
        if not self.engine.has_perm(self.guild_id, interaction.user.id, "control"):
            try:
                await interaction.response.send_message(deny_text("control"), ephemeral=True)
            except Exception:
                pass
            return False
        return True

    async def _reply_ephemeral(self, interaction, text: str) -> None:
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    @ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction, button):
        """Предыдущий трек"""
        if not await self._can_control(interaction):
            return
        status = self.engine.prev_current(self.guild_id)
        if status != "ok":
            await self._reply_ephemeral(
                interaction, _NO_PREV if status == "no_prev" else _NOTHING_PLAYING
            )
            return
        await interaction.response.defer()

    @ui.button(emoji="⏯", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction, button):
        """Пауза/продолжить"""
        if not await self._can_control(interaction):
            return
        self.engine.pause_toggle(self.guild_id)
        await interaction.response.defer()

    @ui.button(emoji="⏭", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction, button):
        """Следующий трек"""
        if not await self._can_control(interaction):
            return
        status = self.engine.skip_current(self.guild_id)
        if status == "nothing":
            await self._reply_ephemeral(interaction, _NOTHING_PLAYING)
            return
        if status == "last":
            await self._reply_ephemeral(interaction, _LAST_TRACK)
            return
        await interaction.response.defer()

    @ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction, button):
        """Цикл очереди (по умолчанию включён)"""
        if not await self._can_control(interaction):
            return
        on = self.engine.loop_toggle(self.guild_id)
        note = (
            "Цикл **включён**: после последнего трека заиграет первый."
            if on
            else "Цикл выключен."
        )
        try:
            await interaction.response.edit_message(
                content=interaction.message.content + "\n\n" + note
            )
        except Exception:
            pass

    @ui.button(emoji="⏹", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction, button):
        """Остановить и выйти"""
        if not await self._can_control(interaction):
            return
        await interaction.response.defer()
        await self.engine.stop_and_leave(self.guild_id)

    @ui.button(emoji="➕", label="В плейлист", style=discord.ButtonStyle.secondary)
    async def add_current_button(self, interaction, button):
        """Добавить текущий трек в плейлист"""
        if not await self._can_control(interaction):
            return
        if not self.engine.has_perm(self.guild_id, interaction.user.id, "playlists"):
            await self._reply_ephemeral(interaction, deny_text("playlists"))
            return
        state = self.engine.play_state.get(self.guild_id)
        if state is None or not state.get("page_url"):
            await self._reply_ephemeral(
                interaction,
                "Сейчас играет не YouTube-трек — добавить в плейлист нечего."
                if state and state.get("title")
                else _NOTHING_PLAYING,
            )
            return
        await open_picker(
            interaction,
            self.engine,
            self.guild_id,
            interaction.user.id,
            {
                "page_url": state["page_url"],
                "title": state["title"],
                "duration": state.get("duration"),
            },
        )
