"""Аудио-вложения из чата: скачивание в uploads/ и работа с local-ссылками.

Файлы сохраняются как uploads/<attachment_id>_<санитизированное имя>.
В плейлистах локальные файлы представлены ссылками вида `local:<имя файла>`,
абсолютный путь собирается через local_path().
"""

import logging
import re
from pathlib import Path

from config import AUDIO_EXTS, LOCAL_URL_PREFIX, UPLOADS_DIR, UPLOADS_MAX_FILE

_log = logging.getLogger("music_bot")


def sanitize_name(name: str) -> str:
    """Имя файла без мусорных символов; расширение сохраняется."""
    p = Path(name)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", p.stem).strip()[:76] or "audio"
    return stem + p.suffix.lower()


def is_audio(attachment) -> bool:
    """Аудио ли это вложение (по расширению имени файла)."""
    return Path(attachment.filename).suffix.lower() in AUDIO_EXTS


async def download_attachment(attachment) -> Path:
    """Скачать вложение в uploads/ и вернуть путь. Бросает ValueError при превышении лимита."""
    if attachment.size > UPLOADS_MAX_FILE:
        raise ValueError(
            f"{attachment.filename} слишком большой "
            f"({attachment.size // 1024 // 1024} МБ, лимит {UPLOADS_MAX_FILE // 1024 // 1024} МБ)."
        )
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOADS_DIR / f"{attachment.id}_{sanitize_name(attachment.filename)}"
    await attachment.save(path)
    return path


def local_url(path: Path) -> str:
    """URL для хранения в плейлисте (local:<имя файла>)."""
    return LOCAL_URL_PREFIX + path.name


def local_path(url: str) -> Path:
    """Абсолютный путь файла uploads/ по local-ссылке из плейлиста."""
    return UPLOADS_DIR / url[len(LOCAL_URL_PREFIX):]
