"""Слушатели событий: готовность, ссылки/поиск/вложения в #music,
выбор результата поиска реакцией, отключение бота извне."""

import logging

from lolka.ext import commands

from config import MUSIC_CHANNEL_NAMES, YOUTUBE_RE
from upload_util import download_attachment, is_audio

_log = logging.getLogger("music_bot")

# Короткий текст (мусор в #music) не должен запускать поиск.
SEARCH_MIN_LEN = 3


class ListenerCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    @commands.Cog.listener()
    async def on_ready(self):
        _log.info("Бот %s готов, серверов: %d", self.bot.user, len(self.bot.guilds))
        print(f"Бот {self.bot.user} готов, серверов: {len(self.bot.guilds)}")
        self.engine.start_watchdog()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        # process_commands НЕ вызываем: BotBase.on_message уже делает это
        # при каждой dispatch (lolka/ext/commands/bot.py), повторный вызов
        # приводит к двойному выполнению команд.
        if message.content.lstrip().startswith(self.bot.command_prefix):
            return
        if message.channel.name not in MUSIC_CHANNEL_NAMES:
            return
        m = YOUTUBE_RE.search(message.content)
        if m:
            await self.engine.enqueue_single(message, m.group(0))
            return
        for att in message.attachments:
            if not is_audio(att):
                continue
            try:
                path = await download_attachment(att)
            except Exception as exc:
                _log.exception("не удалось скачать вложение %s", att.filename)
                await message.channel.send(
                    f"Не удалось получить вложение **{att.filename}**: {exc}"
                )
                continue
            await self.engine.enqueue_local(message, path)
        if any(is_audio(a) for a in message.attachments):
            self.engine.cleanup_uploads()
            return
        # Свободный текст в #music — поиск по YouTube (выбор реакцией).
        # silent=True: при частых сообщениях подряд поиск молча пропускается.
        text = message.content.strip()
        if len(text) >= SEARCH_MIN_LEN:
            await self.engine.start_search(
                message.guild.id, message.author.id, message.channel, text, silent=True
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.bot.user.id:
            return
        await self.engine.handle_search_reaction(payload)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id == self.bot.user.id:
            if after.channel is None:
                if member.guild.id in self.engine._voice_recovering:
                    return
                self.engine.clear_guild(member.guild.id)
            return
        # Человек вышел из канала бота: если людей не осталось, движок
        # запланирует выход через EMPTY_CHANNEL_GRACE (перепроверка отменит
        # его, если кто-то вернулся). События ботов не считаем людьми.
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return
        if before.channel is None or before.channel.id != vc.channel.id:
            return
        if after.channel is not None and after.channel.id == vc.channel.id:
            return
        self.engine.on_humans_left(member.guild)
