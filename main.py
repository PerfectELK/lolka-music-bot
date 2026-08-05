"""Точка входа музыкального бота для lolka.app.

Слои:
  config.py       — константы и настройки
  util.py         — консоль, патчи lolka.py, ffmpeg, токен, spawn
  resolver.py     — yt-dlp: получение аудио-URL (в отдельном потоке)
  engine.py       — MusicEngine: очередь/плейлисты/предзагрузка/цикл
  cogs/           — команды: music.py (голос/очередь), playlists.py (!pl), listener.py
  playlist_db.py  — SQLite-хранилище плейлистов

Ссылки принимаются в текстовом канале с именем "music" (или "музыка"),
либо командой !play.
"""

import asyncio
import logging
import signal

import lolka as discord
from lolka.ext import commands

from cogs.admin import AdminCog
from cogs.help import HelpCog
from cogs.listener import ListenerCog
from cogs.music import MusicCog
from cogs.panel import PanelCog
from cogs.perms import PermsCog
from cogs.playlists import PlaylistCog
from config import LOCK_FILE, TOKEN_ENV, TOKEN_FILE, UPLOADS_DIR
from engine import MusicEngine
from permissions_db import PermissionsDB
from playlist_db import PlaylistDB
from util import acquire_single_instance_lock, find_ffmpeg, get_token, patch_parse_time, setup_console

setup_console()
patch_parse_time()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
# Без members-интента guild.members пуст (GUILD_CREATE не несёт участников) —
# панель !perms строит Select участников из guild.members.
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.help_command = None  # свой !help в cogs/help.py (встроенный был бы в конфликте с ним)
# Упоминания в сообщениях бота запрещены: названия роликов/запросы приходят
# из YouTube и чата, они не должны пинать @everyone/роли/пользователей.
bot.allowed_mentions = discord.AllowedMentions(everyone=False, roles=False, users=False)

_log = logging.getLogger("music_bot")


def _add_file_logging() -> None:
    """Дублировать логи в файл с ротацией (на сервере удобнее разбирать
    падения, чем крутить journald). Неудача не должна ронять бота —
    файловый лог опционален."""
    try:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "music_bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
    except OSError:
        _log.warning("не удалось включить файловый лог (logs/) — продолжаю без него")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _add_file_logging()
    lock_fd = acquire_single_instance_lock(LOCK_FILE)
    if lock_fd is None:
        raise SystemExit(
            "Бот уже запущен — эксклюзивная блокировка занята (bot.lock). "
            "Запусти только один инстанс."
        )

    engine = MusicEngine(bot, PlaylistDB(), find_ffmpeg(), PermissionsDB())
    bot.engine = engine
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    await bot.add_cog(MusicCog(bot, engine))
    await bot.add_cog(PlaylistCog(bot, engine))
    await bot.add_cog(PanelCog(bot, engine))
    await bot.add_cog(PermsCog(bot, engine))
    await bot.add_cog(AdminCog(bot, engine))
    await bot.add_cog(ListenerCog(bot, engine))
    await bot.add_cog(HelpCog(bot))

    token = get_token(TOKEN_ENV, TOKEN_FILE)
    if token is None:
        raise SystemExit(
            f"Токен не найден: задай переменную окружения {TOKEN_ENV} "
            f"или заполни файл {TOKEN_FILE}"
        )

    # SIGINT/SIGTERM (systemd restart/stop) — корректно закрыть gateway-сессию,
    # чтобы Lolka сразу инвалидировала её, а не висела ghost-сессией до таймаута.
    async def _shutdown() -> None:
        _log.info("получен сигнал остановки: закрываю gateway-сессию")
        engine.shutdown()
        await bot.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
