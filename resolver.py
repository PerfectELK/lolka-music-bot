"""Доступ к YouTube: кеш треков, метаданные и поиск (всё через yt-dlp).

Три пути:
  resolve_cached  — локальная копия из кеша или скачивание (единственный путь
                    получения аудио; см. track_cache.TrackCache).
  fetch_info      — только метаданные (title, page_url, duration), без скачивания.
  search_youtube  — поиск по текстовому запросу (ytsearch), тоже без скачивания.

Сетевые вызовы выполняются в отдельном потоке (executor), чтобы не блокировать
event loop. Полная отмена потока невозможна, но задачи, запущенные для
отброшенных треков, отменяются на уровне asyncio — их результаты не используются.
"""

import asyncio
import concurrent.futures
from functools import partial
from typing import Optional

import yt_dlp

from config import YTDLP_DOWNLOAD_WORKERS, YTDLP_META_WORKERS, YTDLP_OPTS

# Общий лимит потоков yt-dlp, не зависящий от числа глид (регулируется
# YTDLP_DOWNLOAD_WORKERS): дефолтный executor (min(32, cpu+4)) легко насытить
# 3+ плейлистами по PREFETCH_AHEAD=2.
_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=YTDLP_DOWNLOAD_WORKERS, thread_name_prefix="ytdlp"
)

# Метаданные и поиск — на отдельном пуле (YTDLP_META_WORKERS), чтобы пакетный
# fetch_info (!pl add) или ytsearch не стояли в очереди перед докачкой треков,
# которую ждёт play_next (старт воспроизведения не должен ждать метаданные).
_meta_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=YTDLP_META_WORKERS, thread_name_prefix="ytdlp-meta"
)


async def resolve_cached(
    cache, url: str, *, on_info=None
) -> tuple[str, str, str, Optional[int]]:
    """Локальная копия трека (или скачивание в кеш).

    Вернёт (путь к файлу, title, канонический page_url, duration).
    on_info — необязательный колбэк: вызывается из потока скачивания после
    извлечения метаданных (info_dict от yt-dlp), до начала докачки. Хук
    вызывается из воркер-потока — асинхронный код из него запускать нельзя,
    только через потокобезопасный spawn() (см. engine._resolve_entry).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, partial(cache.ensure, url, on_info=on_info))


async def fetch_info(url: str) -> tuple[str, str, Optional[int]]:
    """Метаданные ролика без скачивания: (title, канонический page_url, duration).

    Используется там, где нужен только title для плейлиста (!pl add) — чтобы
    не качать трек целиком ради названия.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_meta_executor, _fetch_info_sync, url)


def _fetch_info_sync(url: str) -> tuple[str, str, Optional[int]]:
    with yt_dlp.YoutubeDL(YTDLP_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("yt-dlp не вернул информацию о ролике")
    return (
        info.get("title") or "неизвестно",
        info.get("webpage_url") or url,
        info.get("duration"),
    )


async def search_youtube(query: str, limit: int = 5) -> list[dict]:
    """Поиск по YouTube (ytsearch). Вернёт список dict:
    {"title", "page_url", "duration", "uploader"}. Без скачивания.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_meta_executor, _search_sync, query, limit)


def _search_sync(query: str, limit: int) -> list[dict]:
    opts = {
        **YTDLP_OPTS,
        "extract_flat": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = info.get("entries") or []
    results = []
    for e in entries:
        if not e:
            continue
        title = e.get("title")
        url = e.get("url") or e.get("webpage_url")
        if not title or not url:
            continue
        results.append(
            {
                "title": title,
                "page_url": e.get("webpage_url") or url,
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel"),
            }
        )
    return results
