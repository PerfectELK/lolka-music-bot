"""Локальный кеш скачанных треков.

Сначала ищется локальная копия по id ролика — если её нет, трек скачивается
через yt-dlp с перекодированием в Opus 96k (мало места). Всё — синхронные
функции, вызывать через потоковый executor (см. resolver.resolve_cached).

Измерение громкости (ffmpeg ebur128, ~2-10 с) выполняется в фоновом пуле
(_loudness_executor) ПОСЛЕ докачки: слот потока скачивания освобождается
сразу, следующий трек начинает качаться раньше. Пока измерение не записано
в sidecar, gain_for_path (loudness.py) лениво измерит при старте трека —
гонка идемпотентна, оба пишут одно значение.

Структура каталога:
  cache/<video_id>.opus   — аудио
  cache/<video_id>.json   — sidecar: title, канонический page_url, время скачивания,
                            loudness: {"i", "tp"} — измерение громкости EBU R128
                            (см. loudness.py); у старых треков ключа может не быть,
                            у свежескачанных он дописывается в фоне
  cache/tmp/              — временные файлы (чистятся при старте)

Кеш ограничен CACHE_MAX_BYTES: после каждого скачивания удаляются самые
старые по mtime файлы (LRU). Скачивание одного и того же ролика защищено
per-id threading.Lock, чтобы параллельные префетчи не качали дважды.
"""

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, Optional

import yt_dlp

from config import YTDLP_OPTS
from loudness import measure_loudness

_log = logging.getLogger("music_bot")

# Отдельный небольшой пул для фонового измерения громкости: измерение (ffmpeg
# ebur128) не должно занимать слот потока скачивания yt-dlp — докачка следующего
# трека не ждёт измерения предыдущего.
_loudness_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="loudness"
)

_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def video_id(url: str) -> Optional[str]:
    m = _ID_RE.search(url)
    return m.group(1) if m else None


class TrackCache:
    def __init__(self, cache_dir: Path, max_bytes: int, ffmpeg_exe: Optional[str] = None):
        self.dir = Path(cache_dir)
        self.tmp_dir = self.dir / "tmp"
        self.max_bytes = max_bytes
        self.ffmpeg_exe = ffmpeg_exe
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._locks: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()
        self._cleanup_tmp()

    def _cleanup_tmp(self) -> None:
        for f in self.tmp_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass

    def _lock_for(self, vid: str) -> threading.Lock:
        lock = self._locks.get(vid)
        if lock is None:
            lock = threading.Lock()
            self._locks[vid] = lock
        return lock

    def audio_path(self, page_url: str) -> Path:
        vid = video_id(page_url)
        return self.dir / f"{vid}.opus"

    def get(self, page_url: str) -> Optional[Path]:
        """Путь к локальной копии, если она есть, иначе None."""
        path = self.audio_path(page_url)
        return path if path.is_file() else None

    def ensure(
        self, page_url: str, *, on_info: Optional[Callable[[dict], None]] = None
    ) -> tuple[str, str, str, Optional[int]]:
        """Вернуть (путь к локальному файлу, title, канонический page_url, duration).

        Сначала ищет локальную копию, при отсутствии скачивает и сохраняет.
        Попадание в кеш обновляет mtime файла — LRU-очистка учитывает
        реальное использование, а не только момент скачивания.

        on_info — колбэк на метаданные трека, вызывается из потока
        скачивания сразу после извлечения (до докачки); см. _make_hook.
        """
        vid = video_id(page_url)
        if vid is None:
            raise RuntimeError("не удалось определить id ролика")
        final = self.dir / f"{vid}.opus"
        if final.is_file():
            self._touch(final)
            meta = self._read_meta(vid)
            return str(final), meta.get("title") or "неизвестно", meta.get("page_url") or page_url, meta.get("duration")
        lock = self._lock_for(vid)
        with lock:
            if final.is_file():
                self._touch(final)
                meta = self._read_meta(vid)
                return str(final), meta.get("title") or "неизвестно", meta.get("page_url") or page_url, meta.get("duration")
            self._download(page_url, vid, final, on_info)
        self._enforce_cap()
        meta = self._read_meta(vid)
        return str(final), meta.get("title") or "неизвестно", meta.get("page_url") or page_url, meta.get("duration")

    @staticmethod
    def _touch(path: Path) -> None:
        """Обновить mtime файла (LRU по использованию). Ошибку не роняем."""
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _read_meta(self, vid: str) -> dict:
        meta_path = self.dir / f"{vid}.json"
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _make_hook(on_info: Optional[Callable[[dict], None]]):
        """Прогресс-хук yt-dlp, вызывающий on_info(info_dict) после извлечения.

        ОБЯЗАТЕЛЬНО в try/except: исключение в progress-хуке yt-dlp абортит
        скачивание. Хук дергается многократно и из воркер-потока — on_info
        обязан быть идемпотентным и не делать asyncio-await напрямую.
        """

        def hook(info: dict) -> None:
            if on_info is None:
                return
            try:
                on_info(info)
            except Exception:
                _log.exception("progress-hook on_info упал (игнорирую)")

        return hook

    def _download(
        self,
        page_url: str,
        vid: str,
        final: Path,
        on_info: Optional[Callable[[dict], None]] = None,
    ) -> None:
        opts = {
            **YTDLP_OPTS,
            "outtmpl": str(self.tmp_dir / f"{vid}.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "opus",
                    "preferredquality": "96K",
                }
            ],
        }
        if on_info is not None:
            opts["progress_hooks"] = [self._make_hook(on_info)]
        if self.ffmpeg_exe:
            opts["ffmpeg_location"] = str(Path(self.ffmpeg_exe).parent)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page_url, download=True)
            if not info:
                raise RuntimeError("yt-dlp ничего не скачал")
            src = self.tmp_dir / f"{vid}.opus"
            if not src.is_file():
                candidates = list(self.tmp_dir.glob(f"{vid}.*"))
                if not candidates:
                    raise RuntimeError("скачанный файл не найден во временной папке")
                src = candidates[0]
            os.replace(src, final)
            meta = {
                "title": info.get("title") or "неизвестно",
                "page_url": info.get("webpage_url") or page_url,
                "duration": info.get("duration"),
                "downloaded_at": time.time(),
            }
            (self.dir / f"{vid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            size_mb = final.stat().st_size / 1024 / 1024
            _log.info("скачано в кеш: %s (%.1f МБ)", meta["title"], size_mb)
            # Громкость измеряется в фоне (отдельный пул): слот потока
            # скачивания освобождается сразу, пока ebur128 крутит ffmpeg
            # (~2-10 с), следующий трек уже может качаться. Если трек стартует
            # раньше, чем измерение дописано, gain_for_path (loudness.py)
            # лениво измерит при проигрывании — гонка идемпотентна.
            _loudness_executor.submit(self._measure_and_write_sidecar, vid, final)
        finally:
            for f in self.tmp_dir.glob(f"{vid}.*"):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _measure_and_write_sidecar(self, vid: str, final: Path) -> None:
        """Фоновое измерение громкости + допись в sidecar (паттерн _write_sidecar).

        Все исключения — только в лог: падение измерения не должно ничего
        ронять (трек уже в кеше, gain подхватится лениво при старте).
        """
        try:
            loudness = measure_loudness(self.ffmpeg_exe, final)
            if loudness is None:
                return
            meta_path = self.dir / f"{vid}.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            meta["loudness"] = loudness
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            _log.warning("не сохранил измерение громкости %s: %s", final, exc)

    def _enforce_cap(self) -> None:
        if self.max_bytes <= 0:
            return
        files = [p for p in self.dir.glob("*.opus") if p.is_file()]
        total = sum(f.stat().st_size for f in files)
        if total <= self.max_bytes:
            return
        for f in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= self.max_bytes:
                break
            size = f.stat().st_size
            meta = self.dir / f"{f.stem}.json"
            try:
                f.unlink()
                meta.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            _log.info("кеш: удалён %s (лимит %.1f ГБ)", f.name, self.max_bytes / 1024**3)
