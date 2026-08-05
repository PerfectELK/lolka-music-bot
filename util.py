"""Общие утилиты: консоль, патчи для lolka.py, ffmpeg, токен, запуск корутин."""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import lolka.utils as lolka_utils

_log = logging.getLogger("music_bot")


def setup_console() -> None:
    """Консоль Windows (cp866) коверкает кириллицу — переключаем вывод на UTF-8."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def patch_parse_time() -> None:
    """lolka.py 0.5.1 на Python < 3.11: datetime.fromisoformat не понимает
    'Z'-суффикс и дробные секунды короче 6 цифр, а API Lolka шлёт даты вида
    '2026-01-17T22:07:27Z' или '2026-08-05T14:43:55.08+00:00' → ValueError.
    Заменяем parse_time во всех загруженных модулях пакета: сначала пробуем
    оригинал, при ValueError нормализуем строку. Убрать, если починят."""
    import re as _re

    _orig = lolka_utils.parse_time
    _frac_re = _re.compile(r"\.(\d+)([+-]\d{2}:\d{2})?$")

    def _parse_time(timestamp):
        if not isinstance(timestamp, str):
            return _orig(timestamp)
        try:
            return _orig(timestamp)
        except ValueError:
            s = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
            m = _frac_re.search(s)
            if m is not None and len(m.group(1)) != 6:
                frac = m.group(1).ljust(6, "0")[:6]
                s = s[: m.start(1)] + frac + s[m.end(1):]
            return _orig(s)

    lolka_utils.parse_time = _parse_time
    for name, mod in list(sys.modules.items()):
        if name.startswith("lolka.") and name != "lolka.utils":
            if getattr(mod, "parse_time", None) is _orig:
                mod.parse_time = _parse_time


def find_ffmpeg() -> Optional[str]:
    """ffmpeg из winget не попадает в PATH — ищем в типовых местах Windows."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
        Path("C:/Program Files/ffmpeg"),
        Path("C:/ffmpeg"),
        Path("C:/ProgramData/chocolatey/bin"),
    ]
    for root in roots:
        if root.is_dir():
            for exe in root.rglob("ffmpeg.exe"):
                return str(exe)
    return None


def get_token(env_name: str, token_file: Path) -> Optional[str]:
    """Токен из переменной окружения или token.txt (создаётся с заглушкой)."""
    token = os.environ.get(env_name)
    if token:
        return token
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token and not token.startswith("вставь"):
            return token
    token_file.write_text("вставь-сюда-токен-бота", encoding="utf-8")
    return None


def acquire_single_instance_lock(lock_path: Path) -> Optional[int]:
    """Эксклюзивная блокировка на время жизни процесса — защита от дублей бота.

    Вернёт fd блокировки или None, если другой инстанс уже держит её.
    Блокировка снимается автоматически при выходе процесса.
    Linux — fcntl.flock, Windows — msvcrt.locking.
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            os.write(fd, b"1")  # файл не должен быть пустым для msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    return fd


def spawn(coro, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Запустить корутину в loop бота из любого потока, не роняя исключения молча.

    after-колбэк FFmpegPCMAudio вызывается из потока aiortc-задачи, поэтому
    asyncio.ensure_future/ensure_future тут не подходит — нужен run_coroutine_threadsafe.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.error("spawn: нет ни running loop, ни переданного loop")
            return

    def _done(task: asyncio.Future) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.exception("ошибка в фоновой задаче", exc_info=exc)

    task = asyncio.run_coroutine_threadsafe(coro, loop)
    task.add_done_callback(_done)
