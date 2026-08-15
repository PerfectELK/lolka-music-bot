"""Музыкальный движок: очередь, плейлисты, фоновая предзагрузка треков, цикл.

Состояние на глиду (атрибуты MusicEngine):
  queues[guild_id]      — очередь треков. Элемент:
      {"title", "page_url", "url" (None, пока не докачан), "duration",
       "resolved" (True после попытки докачать), "error", "future" (Task)}
  play_state[guild_id]  — {"channel", "now_message", "title", "page_url",
                          "duration", "view", "new_session"} — канал и
                          сообщение «Сейчас играет», переиспользуемое через
                          edit в рамках сессии; "view" — NowPlayingView с
                          кнопками ⏮ ⏯ ⏭ 🔁 ⏹; "new_session" — при старте
                          новой сессии (плейлист, первый трек, !now)
                          сообщение отправляется заново, старая панель
                          лишается кнопок
  pl_nav[guild_id]      — курсор плейлиста: {"name", "items", "index"},
                          инвариант: очередь == items[index:]
  history[guild_id]     — последние HISTORY_LIMIT проигранных треков (для !prev и цикла)
  loop_on[guild_id]     — цикл воспроизведения (по умолчанию включён)
  last_played[guild_id] — имя последнего плейлиста (!pl replay)
  pending_search[guild_id] — активный поиск по YouTube: {"message", "user_id",
                          "results", "expire_task"} — выбор результата реакцией
  _prefetch[guild_id]   — активная задача предзагрузки

Предзагрузка: в очереди заранее докачивается не более PREFETCH_AHEAD треков
(текущий + следующий), остальные ждут. Освободившийся слот (докачался или
упал) сразу занимает следующий трек — так плейлист из 1000 треков не
выкачивается целиком. Остановка (!stop/!leave/замена очереди) отменяет
активные задачи; результат отброшенных загрузок не используется.
"""

import asyncio
import logging
import math
import random
import time
from types import SimpleNamespace
from typing import Optional

from pathlib import Path

import lolka as discord

from config import (
    CACHE_DIR,
    CACHE_MAX_BYTES,
    DEFAULT_PLAYLIST,
    DEFAULT_VOLUME,
    EMPTY_CHANNEL_GRACE,
    FFMPEG_BEFORE_OPTIONS,
    HISTORY_LIMIT,
    LOCAL_URL_PREFIX,
    MAX_TRACK_RETRIES,
    PREFETCH_AHEAD,
    SEARCH_COOLDOWN,
    SEARCH_RESULTS,
    SEARCH_TIMEOUT,
    TRACK_MIN_RETRY_SEC,
    UPLOADS_DIR,
    UPLOADS_MAX_BYTES,
    VOLUME_MAX,
    VOLUME_MIN,
)
from now_playing import NowPlayingView
from loudness import gain_for_path
from permissions_db import PermissionsDB
from playlist_picker import AddToPlaylistView
from resolver import resolve_cached, search_youtube
from track_cache import TrackCache
from engine_types import PlNav, PendingSearch, PlayState, QueueEntry
from ui_utils import esc, fmt_duration, paginate
from upload_util import local_path
from util import spawn

_log = logging.getLogger("music_bot")


def _lazy_entry(page_url: str, title: str) -> QueueEntry:
    return {
        "title": title or page_url,
        "page_url": page_url,
        "url": None,
        "duration": None,
        "resolved": False,
        "error": None,
        "future": None,
    }


def _local_entry(path) -> QueueEntry:
    return {
        "title": path.stem,
        "page_url": None,
        "url": str(path),
        "duration": None,
        "resolved": True,
        "error": None,
        "future": None,
    }


def friendly_error(exc) -> str:
    """Человекочитаемая версия типичных ошибок yt-dlp для чата.

    Полный текст ошибки всегда остаётся в логах (см. _resolve_entry).
    """
    low = str(exc).lower()
    if "sign in to confirm your age" in low:
        return "видео с возрастным ограничением — YouTube требует авторизацию (18+)"
    if "video unavailable" in low or "private" in low:
        return "видео недоступно или удалено"
    if "http error 403" in low:
        return "YouTube временно блокирует загрузку (HTTP 403) — попробуй позже"
    if "unable to download video data" in low:
        return "не удалось скачать видео — временная ошибка YouTube, попробуй позже"
    if "rate limit" in low or "too many requests" in low:
        return "YouTube ограничил частоту запросов — подожди пару минут и попробуй снова"
    if "unsupported url" in low or "no url" in low:
        return "ссылка не поддерживается или не похожа на YouTube"
    if "sign in to confirm you're not a bot" in low or "captcha" in low:
        return "YouTube требует капчу/авторизацию — попробуй позже"
    return str(exc)


def rotate_tracks(tracks: list, start: int) -> list:
    """Повернуть список так, чтобы элемент с индексом start-1 стал первым
    (1-based): !pl play <имя> <номер> играет весь плейлист, начиная с номера.
    """
    if start <= 1 or not tracks:
        return list(tracks)
    return tracks[start - 1:] + tracks[:start - 1]


def _rewind_queue(history: list, queue: list):
    """Откат на предыдущий трек без плейлиста (логика ⏮/!prev).

    Текущий трек возвращается в начало очереди, перед ним встаёт предыдущий
    (из истории) — переключение «назад-вперёд» не теряет треки: очередь
    становится [предыдущий, текущий, ...остаток].

    Возвращает (новая_история, новая_очередь) или None, если предыдущего
    трека нет (в истории меньше двух записей). Списки не мутирует.
    """
    if len(history) < 2:
        return None
    h = list(history)
    current = h.pop()
    prev_item = h[-1]
    q = [current, *queue]
    q.insert(0, prev_item)
    return h, q


def _rewind_playlist(items: list, index: int, queue: list):
    """Откат на предыдущий трек в плейлисте (логика ⏮/!prev).

    Инвариант плейлиста: очередь == items[index:] (index — следующий к
    игре трек). Возвращает (новая_очередь, новый_index) или None, если
    предыдущего трека нет (index < 2 — играет первый трек).

    В очередь возвращаются два трека перед её головой ([предыдущий,
    текущий]), так что порядок плейлиста (включая shuffle) сохраняется:
    ⏮ ходит строго назад по позициям, ⏭ после ⏮ возвращает тот же трек.
    """
    if index < 2:
        return None
    return [*items[index - 2:index], *queue], index - 2


def _cycle_entries(nav, history):
    """Источник очереди для цикла: для плейлиста — весь плейлист
    (pl_nav items), для простой очереди — история. None, если пусто."""
    if nav is not None and nav.get("items"):
        return list(nav["items"])
    return list(history) if history else None


class MusicEngine:
    def __init__(self, bot, db, ffmpeg_exe: Optional[str], perms: Optional[PermissionsDB] = None):
        self.bot = bot
        self.db = db
        self.ffmpeg = ffmpeg_exe
        self.perms = perms or PermissionsDB()
        self.cache = TrackCache(CACHE_DIR, CACHE_MAX_BYTES, ffmpeg_exe)
        self.queues: dict[int, list[QueueEntry]] = {}
        self.play_state: dict[int, PlayState] = {}
        self.last_played: dict[int, str] = {}
        self.pl_nav: dict[int, PlNav] = {}
        self.history: dict[int, list[QueueEntry]] = {}
        self.loop_on: dict[int, bool] = {}
        self._prefetch: dict[int, asyncio.Task] = {}
        self._drain_tasks: dict[int, set] = {}
        self._voice_recovering: set[int] = set()
        self._leave_tasks: dict[int, asyncio.Task] = {}
        self._watchdog: Optional[asyncio.Task] = None
        self._voice_locks: dict[int, asyncio.Lock] = {}
        self.pending_search: dict[int, PendingSearch] = {}
        self._last_search: dict[int, float] = {}
        self._owner_locks: dict[int, asyncio.Lock] = {}
        self.volumes: dict[int, float] = {}
        self.start_time = time.time()

    # ---------- утилиты ----------

    def spawn(self, coro) -> None:
        spawn(coro, self.bot.loop)

    async def ensure_owner(self, guild) -> None:
        """Определить владельца бота на сервере (кто добавил бота), если ещё нет.

        Порядок источников:
          1. аудит-лог bot_add (кто добавил бота; может быть Forbidden/пусто);
          2. guild.owner_id (владелец сервера);
          3. оставить пусто — владелец назначится первым открывшим !perms
             (source="first_perms", см. open_perms_panel в perms_panel.py).

        Per-guild asyncio.Lock защищает от гонки параллельных первых запросов.
        Исключения не роняют команду — только лог.
        """
        if self.perms.get_owner(guild.id) is not None:
            return
        lock = self._owner_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if self.perms.get_owner(guild.id) is not None:
                return
            try:
                async for entry in guild.audit_logs(
                    action=discord.AuditLogAction.bot_add, limit=10
                ):
                    user = entry.user
                    if user is not None and user.id != self.bot.user.id:
                        await asyncio.to_thread(
                            self.perms.set_owner, guild.id, user.id, "audit"
                        )
                        return
            except Exception:
                _log.warning(
                    "ensure_owner: аудит-лог недоступен (guild=%s), фолбэк на владельца сервера",
                    guild.id,
                )
            owner_id = getattr(guild, "owner_id", None)
            if owner_id is not None:
                await asyncio.to_thread(
                    self.perms.set_owner, guild.id, owner_id, "guild_owner"
                )

    def is_owner(self, guild_id: int, user_id: int) -> bool:
        """Владелец ли пользователь на этой глиде (обёртка над perms.get_owner)."""
        return self.perms.get_owner(guild_id) == user_id

    def has_perm(self, guild_id: int, user_id: int, perm: str) -> bool:
        """Синхронная проверка права для view/кнопок (владелец имеет всё)."""
        return self.perms.can(guild_id, user_id, perm)

    def set_volume(self, guild_id: int, vol: float) -> float:
        """Установить per-guild громкость (VOLUME_MIN..VOLUME_MAX).

        Возвращает установленное значение. Применяется к будущим трекам
        (для текущего трека gain вычислен при старте и не меняется).
        """
        clamped = max(VOLUME_MIN, min(VOLUME_MAX, vol))
        self.volumes[guild_id] = clamped
        return clamped

    def get_status(self) -> dict:
        """Собрать статистику для !status: сервера, очередь, кеш, uploads, uptime."""
        active = []
        for guild_id, state in self.play_state.items():
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            queue = self.queues.get(guild_id, [])
            vc = guild.voice_client
            info = {
                "name": guild.name,
                "queue": len(queue),
                "playing": None,
            }
            if state.get("title"):
                status = "играет"
                info["playing"] = state["title"]
            elif vc is not None and vc.is_paused():
                status = "пауза"
                info["playing"] = state.get("title")
            elif vc is not None and vc.is_connected():
                status = "подключён"
            else:
                status = "неизвестно"
            info["status"] = status
            active.append(info)

        cache_bytes = 0
        cache_files = 0
        try:
            for f in self.cache.dir.glob("*.opus"):
                if f.is_file():
                    cache_bytes += f.stat().st_size
                    cache_files += 1
        except OSError:
            pass

        upload_bytes = 0
        try:
            for f in UPLOADS_DIR.glob("*"):
                if f.is_file():
                    upload_bytes += f.stat().st_size
        except OSError:
            pass

        uptime = time.time() - self.start_time

        return {
            "servers": len(self.bot.guilds),
            "active_sessions": len(active),
            "total_queue": sum(info["queue"] for info in active),
            "cache_bytes": cache_bytes,
            "cache_files": cache_files,
            "cache_max": self.cache.max_bytes,
            "upload_bytes": upload_bytes,
            "upload_max": UPLOADS_MAX_BYTES,
            "uptime": uptime,
            "active": active,
        }

    async def report(
        self, guild_id: int, text: str, *, fresh: bool = False, channel=None
    ) -> None:
        state = self.play_state.get(guild_id)
        if not state:
            return
        fresh = state.pop("new_session", False) or fresh
        if channel is not None:
            state["channel"] = channel
        if fresh:
            old = state.get("now_message")
            if old is not None:
                try:
                    await old.edit(view=None)  # снять кнопки со старой панели
                except Exception:
                    pass
            view = state.get("view")
            if view is not None:
                view.stop()  # клики по старой панели не диспатчатся
                state["view"] = None  # _now_view создаст новую
            state["now_message"] = None
        channel = state.get("channel")
        old = state.get("now_message")
        try:
            if old is not None:
                await old.edit(content=text)
                return
        except Exception:
            pass
        try:
            state["now_message"] = await channel.send(text, view=self._now_view(guild_id))
        except Exception:
            _log.exception("не удалось отправить сообщение")

    async def ensure_voice(self, message) -> tuple[Optional[discord.VoiceClient], Optional[str]]:
        """Подключить бота к голосовому каналу автора сообщения.

        Per-guild asyncio.Lock защищает от гонки: два параллельных вызова
        (два !play одновременно) иначе оба видят vc is None и вызывают
        channel.connect() дважды.
        """
        guild_id = message.guild.id
        lock = self._voice_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if message.author.voice is None or message.author.voice.channel is None:
                return None, "Ты не в голосовом канале — зайди в него и повтори."
            target = message.author.voice.channel
            vc = message.guild.voice_client
            if vc is None:
                vc = await target.connect()
                self._watch_voice(vc, guild_id)
            elif vc.channel != target:
                await vc.move_to(target)
            elif not self._voice_alive(vc):
                await self._recover_voice(guild_id)
                vc = message.guild.voice_client
                if vc is None or not self._voice_alive(vc):
                    return None, "Голосовое соединение оборвалось и не восстановилось — попробуй ещё раз."
            self._drain_incoming(vc)
            return vc, None

    def _drain_incoming(self, vc: discord.VoiceClient) -> None:
        """Сливать входящие аудио-треки участников голосового канала.

        aiortc складывает декодированные кадры в неограниченную очередь
        RemoteStreamTrack (rtcrtpreceiver), и если трек никто не читает,
        память растёт до OOM-килла. Мы не обрабатываем входящий звук —
        просто вычитываем кадры и отбрасываем.

        Таски дренажа регистрируются в _drain_tasks[guild_id] и отменяются
        при повторном вызове (ensure_voice зовётся на каждую команду), при
        !stop/!leave и при остановке бота — иначе они копятся бесконечно и
        висят до конца процесса (asyncio: Task was destroyed but it is pending).
        """
        guild = getattr(getattr(vc, "channel", None), "guild", None)
        if guild is None:
            return
        guild_id = guild.id
        self._stop_drain(guild_id)
        tasks = self._drain_tasks.setdefault(guild_id, set())

        async def _drain(track) -> None:
            task = asyncio.current_task()
            tasks.add(task)
            try:
                while True:
                    await track.recv()
            except Exception:
                pass
            finally:
                tasks.discard(task)

        def on_track(track, user_id, producer_id) -> None:
            self.spawn(_drain(track))

        vc.on_receive_track = on_track
        conn = getattr(vc, "_conn", None)
        if conn is not None:
            for consumer in list(getattr(conn, "consumers", {}).values()):
                track = getattr(consumer, "track", None)
                if track is not None:
                    self.spawn(_drain(track))

    def _stop_drain(self, guild_id: int) -> None:
        """Отменить все задачи дренажа входящего аудио глиды."""
        for task in list(self._drain_tasks.get(guild_id, ())):
            task.cancel()
        self._drain_tasks.pop(guild_id, None)

    # ---------- переживание обрыва голосового соединения ----------

    def _voice_alive(self, vc) -> bool:
        """Живо ли WebRTC-соединение. is_connected() тут не помощник:
        флаг VoiceClient._connected остаётся True даже после обрыва
        сигналинг-вебсокета (он меняется только при явном connect/disconnect).
        Настоящий признак жизни — активный reader-таск Signaling.
        """
        if vc is None or not vc.is_connected():
            return False
        conn = getattr(vc, "_conn", None)
        if conn is None or getattr(conn, "_closed", False):
            return False
        sig = getattr(conn, "signaling", None)
        if sig is None or getattr(sig, "_closed", False):
            return False
        reader = getattr(sig, "_reader", None)
        if reader is not None and reader.done():
            return False
        return True

    def _watch_voice(self, vc, guild_id: int) -> None:
        """Подписаться на обрыв сигналинг-вебсокета (Signaling._read_loop).

        Reader-таск завершается сам только когда вебсокет умирает; при
        штатном disconnect()/close() он отменяется, а Signaling._closed
        выставляется — такие случаи игнорируем.
        """
        conn = getattr(vc, "_conn", None)
        sig = getattr(conn, "signaling", None)
        reader = getattr(sig, "_reader", None)
        if sig is None or reader is None:
            return
        if reader.done():
            # гонка: сигналинг умер раньше, чем мы успели подписаться
            if not reader.cancelled() and not getattr(sig, "_closed", False):
                self.spawn(self._recover_voice(guild_id))
            return

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled() or getattr(sig, "_closed", False):
                return
            _log.warning("голосовой сигналинг оборвался (guild=%s)", guild_id)
            self.spawn(self._recover_voice(guild_id))

        reader.add_done_callback(_on_done)

    async def _reconnect_inplace(self, vc) -> bool:
        """Пересоздать WebRTC-сессию без смены состояния на шлюзе.

        Endpoint/token от оригинального VoiceServerUpdate не протухают при
        обрыве сигналинг-вебсокета — создаём новый VoiceConnection на тех же
        данных. Срабатывает в большинстве случаев: бот остаётся в канале
        без гонки voice state update.
        """
        conn = getattr(vc, "_conn", None)
        ms = getattr(vc, "_ms", None)
        if conn is None or ms is None or not vc.endpoint or not vc.token:
            return False
        endpoint, token = vc.endpoint, vc.token
        try:
            await conn.close()
        except Exception:
            pass
        new_conn = ms.VoiceConnection(endpoint, token, on_receive_track=vc._handle_track)
        try:
            await asyncio.wait_for(new_conn.start(), timeout=30.0)
        except Exception as exc:
            _log.warning("in-place переподключение голоса не удалось: %s", exc)
            try:
                await new_conn.close()
            except Exception:
                pass
            return False
        vc._conn = new_conn
        vc._connected = True
        return True

    async def _recover_voice(self, guild_id: int) -> None:
        """Восстановить оборвавшееся голосовое соединение и продолжить играть."""
        if guild_id in self._voice_recovering:
            return
        self._voice_recovering.add(guild_id)
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            vc = guild.voice_client
            if vc is None or self._voice_alive(vc):
                return
            await self.report(guild_id, "Голосовое соединение оборвалось — переподключаюсь…")
            ok = await self._reconnect_inplace(vc)
            if ok:
                _log.info("голос восстановлен на месте (guild=%s)", guild_id)
                self._watch_voice(vc, guild_id)
                self._drain_incoming(vc)
                await self.report(guild_id, "Переподключился, продолжаю воспроизведение.")
                self._schedule_leave_if_empty(guild_id, vc)
                if self.play_state.get(guild_id) is not None:
                    self.spawn(self.play_next(guild_id))
                return
            _log.warning("in-place не вышло, полное переподключение через шлюз (guild=%s)", guild_id)
            try:
                await vc.disconnect()
            except Exception:
                pass
            try:
                new_vc = await vc.channel.connect()
            except Exception as exc:
                _log.warning("полное переподключение голоса не удалось: %s", exc)
                self.clear_guild(guild_id)
                try:
                    await guild.change_voice_state(channel=None)
                except Exception:
                    pass
                return
            self._watch_voice(new_vc, guild_id)
            self._drain_incoming(new_vc)
            await self.report(guild_id, "Переподключился, продолжаю воспроизведение.")
            self._schedule_leave_if_empty(guild_id, new_vc)
            if self.play_state.get(guild_id) is not None:
                self.spawn(self.play_next(guild_id))
        finally:
            self._voice_recovering.discard(guild_id)

    def start_watchdog(self) -> None:
        """Фоновая проверка здоровья голоса (страховка, если сигналинг умер,
        но watcher не успел сработать или соединение зависло не через ws)."""
        if self._watchdog is not None:
            return
        self._watchdog = asyncio.create_task(self._watchdog_loop())

    def shutdown(self) -> None:
        """Остановка бота: отменить фоновые задачи.

        Дренажи входящего аудио, префетчи, watchdog и ожидание выбора
        в поиске — иначе они висят до конца процесса (asyncio: Task was
        destroyed but it is pending).
        """
        for guild_id in list(self._drain_tasks):
            self._stop_drain(guild_id)
        for guild_id in list(self._prefetch):
            task = self._prefetch.get(guild_id)
            if task is not None and not task.done():
                task.cancel()
        self._prefetch.clear()
        for guild_id in list(self._leave_tasks):
            task = self._leave_tasks.get(guild_id)
            if task is not None and not task.done():
                task.cancel()
        self._leave_tasks.clear()
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        for guild_id in list(self.pending_search):
            self._clear_pending_search(guild_id)

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                for guild_id in list(self.play_state.keys()):
                    guild = self.bot.get_guild(guild_id)
                    vc = guild.voice_client if guild is not None else None
                    if vc is None:
                        continue
                    if not self._voice_alive(vc):
                        _log.warning(
                            "watchdog: голосовое соединение мертво (guild=%s), восстанавливаю", guild_id
                        )
                        self.spawn(self._recover_voice(guild_id))
        except asyncio.CancelledError:
            pass

    # ---------- автовыход при пустом канале ----------

    def _channel_humans(self, channel) -> int:
        """Сколько людей (не ботов) сидит в голосовом канале.

        channel.voice_states — полная карта user_id → VoiceState (в отличие
        от channel.members не зависит от кеша участников); члены в голосовых
        каналах кешируются при voice-интенте, так что .bot доступен. Если
        участник не в кеше — считаем его человеком (безопасное направление:
        не выходим из канала, где кто-то есть).
        """
        n = 0
        for uid in channel.voice_states:
            if uid == self.bot.user.id:
                continue
            m = channel.guild.get_member(uid)
            if m is not None and m.bot:
                continue
            n += 1
        return n

    def on_humans_left(self, guild) -> None:
        """Человек покинул канал бота: если людей не осталось, запланировать
        выход через EMPTY_CHANNEL_GRACE. _delayed_leave перепроверяет канал
        перед выходом — вернувшийся человек отменяет его.

        Во время восстановления голоса (_voice_recovering) сигнал
        пропускается: _recover_voice сам перепроверит канал после
        переподключения и вызовет _schedule_leave_if_empty.
        """
        guild_id = guild.id
        if guild_id in self._voice_recovering:
            return
        self._schedule_leave_if_empty(guild_id, guild.voice_client)

    def _schedule_leave_if_empty(self, guild_id: int, vc) -> None:
        """Запланировать выход из пустого канала (общая для on_humans_left
        и финальной проверки после восстановления голоса)."""
        if guild_id in self._leave_tasks:
            return
        if vc is None or vc.channel is None or self._channel_humans(vc.channel) > 0:
            return
        task = asyncio.create_task(self._delayed_leave(guild_id))
        self._leave_tasks[guild_id] = task
        task.add_done_callback(
            lambda t: self._leave_tasks.pop(guild_id, None)
            if self._leave_tasks.get(guild_id) is t
            else None
        )

    async def _delayed_leave(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(EMPTY_CHANNEL_GRACE)
            # Во время восстановления голоса не дёргаем disconnect —
            # _recover_voice после переподключения сам решит, уходить ли.
            if guild_id in self._voice_recovering:
                return
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild is not None else None
            if vc is None or vc.channel is None:
                return
            if self._channel_humans(vc.channel) > 0:
                return
            _log.info("в голосовом канале не осталось людей, выхожу (guild=%s)", guild_id)
            self.clear_guild(guild_id)
            try:
                await vc.disconnect()
            except Exception:
                _log.exception("не удалось выйти из пустого канала (guild=%s)", guild_id)
        except asyncio.CancelledError:
            pass

    def clear_guild(self, guild_id: int) -> None:
        """Отменить загрузки и сбросить всё состояние глиды (stop/leave/отключение)."""
        self._cancel_fetches(guild_id)
        self._stop_drain(guild_id)
        self.queues.pop(guild_id, None)
        state = self.play_state.pop(guild_id, None)
        view = state.get("view") if state else None
        if view is not None:
            view.stop()
        self.pl_nav.pop(guild_id, None)
        self.history.pop(guild_id, None)

    def sync_nav_after_mutation(self, guild_id: int) -> None:
        """После ручной правки очереди (!prev/!remove/!move/!shuffle) пересобрать
        курсор плейлиста, сохраняя инвариант очередь == items[index:]."""
        nav = self.pl_nav.get(guild_id)
        if nav is None:
            return
        self.pl_nav[guild_id] = {
            "name": nav.get("name"),
            "items": list(self.queues.get(guild_id, [])),
            "index": 0,
        }

    def on_playlist_track_added(
        self, guild_id: int, name: str, url: str, title: str, duration=None
    ) -> bool:
        """Плейлист name сейчас играет? Добавить трек в конец очереди и курсора.

        Возвращает True, если трек поставлен в очередь. Инвариант
        очередь == items[index:] сохраняется: оба списка удлиняются одним
        хвостом. Локальные файлы (local:...) играются напрямую, без предзагрузки.
        """
        nav = self.pl_nav.get(guild_id)
        if nav is None or nav.get("name") != name:
            return False
        if url.startswith(LOCAL_URL_PREFIX):
            path = local_path(url)
            if not path.is_file():
                return False
            entry = _local_entry(path)
            entry["title"] = title or path.stem
        else:
            entry = _lazy_entry(url, title)
            entry["duration"] = duration
        self.queues.setdefault(guild_id, []).append(entry)
        nav["items"].append(entry)
        self._schedule_prefetch(guild_id)
        return True

    def cleanup_uploads(self) -> None:
        """LRU-очистка uploads/ по mtime; файлы, на которые ссылаются плейлисты, не трогаем."""
        try:
            referenced = self.db.referenced_local_files()
            files = sorted(
                (p for p in UPLOADS_DIR.glob("*") if p.is_file() and p.name not in referenced),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= UPLOADS_MAX_BYTES:
                break
            try:
                total -= p.stat().st_size
                p.unlink()
                _log.info("upload LRU: удалён %s", p.name)
            except OSError:
                pass

    # ---------- фоновая предзагрузка ----------

    def _cancel_fetches(self, guild_id: int) -> None:
        task = self._prefetch.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()
        for entry in self.queues.get(guild_id, []):
            fut = entry.get("future")
            if fut is not None and not fut.done():
                fut.cancel()

    def _schedule_prefetch(self, guild_id: int) -> None:
        task = self._prefetch.get(guild_id)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._prefetch_queued(guild_id))
        self._prefetch[guild_id] = task
        task.add_done_callback(
            lambda t: self._prefetch.pop(guild_id, None)
            if self._prefetch.get(guild_id) is t
            else None
        )

    async def _prefetch_queued(self, guild_id: int) -> None:
        budget = PREFETCH_AHEAD
        for entry in self.queues.get(guild_id, []):
            if entry.get("future") is not None or entry.get("url") is not None:
                budget -= 1
            elif not entry.get("resolved") and budget > 0:
                entry["future"] = asyncio.create_task(self._resolve_entry(guild_id, entry))
                budget -= 1
            if budget <= 0:
                break

    def _make_info_hook(self, guild_id: int, entry: dict):
        """on_info-хук для resolve_cached: подставить реальное название трека.

        Вызывается из потока скачивания (progress-hook yt-dlp) — здесь нельзя
        await; asyncio-часть (панель «Сейчас играет») запускается через
        self.spawn (run_coroutine_threadsafe). Хук идемпотентен: yt-dlp
        дёргает его многократно, обновляем entry только при смене title.
        Панель обновляется только если трек — голова очереди (иначе префетч
        более позднего трека затёр бы текст текущей панели); финальный report
        «Сейчас играет» всё равно перезапишет её.
        """
        def on_info(info: dict) -> None:
            t = (info.get("title") or "").strip()
            if not t or entry.get("title") == t:
                return
            entry["title"] = t
            q = self.queues.get(guild_id)
            if q and q[0] is entry and self.play_state.get(guild_id):
                self.spawn(
                    self.report(
                        guild_id,
                        f"Скачиваю: **{esc(t)}** ⏳ — заиграет, как будет готов.",
                    )
                )
        return on_info

    async def _resolve_entry(self, guild_id: int, entry: dict) -> None:
        try:
            path, title, canonical, duration = await resolve_cached(
                self.cache, entry["page_url"], on_info=self._make_info_hook(guild_id, entry)
            )
            entry["url"] = path
            entry["title"] = title or entry["title"]
            entry["duration"] = duration
            # Канонический URL ролика (webpage_url) — дедуп в default плейлисте
            # и хранение в БД должны использовать его, а не форму ссылки из чата.
            if canonical:
                entry["page_url"] = canonical
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry["error"] = str(exc)
            _log.warning("[guild=%s] yt-dlp: %s (%s)", guild_id, exc, entry["page_url"])
        else:
            if entry.get("save_to_default"):
                await self._save_to_default(guild_id, entry)
        finally:
            entry["resolved"] = True
            entry["future"] = None
            self._prefetch.pop(guild_id, None)
            self._schedule_prefetch(guild_id)

    async def _save_to_default(self, guild_id: int, entry: dict) -> None:
        """Сохранить резолвнутый трек в плейлист default (без дублей по page_url).

        Записи sqlite выполняются в отдельном потоке — синхронные коммиты
        (fsync) не должны блокировать event loop в горячем пути резолва.
        """
        try:
            await asyncio.to_thread(
                self.db.ensure_playlist, guild_id, DEFAULT_PLAYLIST
            )
            if not await asyncio.to_thread(
                self.db.has_track, guild_id, DEFAULT_PLAYLIST, entry["page_url"]
            ):
                await asyncio.to_thread(
                    self.db.add_track,
                    guild_id,
                    DEFAULT_PLAYLIST,
                    entry["page_url"],
                    entry["title"],
                    entry["duration"],
                )
        except Exception as exc:
            _log.warning("[guild=%s] не удалось сохранить трек в плейлист default: %s", guild_id, exc)

    # ---------- воспроизведение ----------

    def _on_track_end(self, guild_id: int, err: Optional[Exception]) -> None:
        if err is not None:
            _log.warning("[guild=%s] трек завершился с ошибкой: %s", guild_id, err)
        state = self.play_state.get(guild_id)
        if state is not None:
            started = state.pop("track_started_at", None)
            if started is not None:
                elapsed = time.monotonic() - started
                if err is None and elapsed < TRACK_MIN_RETRY_SEC:
                    state["track_ended_quick"] = True
                    _log.warning(
                        "[guild=%s] трек оборвался через %.1f с (сбой пайплайна?): %s",
                        guild_id, elapsed, state.get("title"),
                    )
        self.spawn(self.play_next(guild_id))

    async def play_next(self, guild_id: int) -> None:
        while True:
            guild = self.bot.get_guild(guild_id)
            vc = guild.voice_client if guild is not None else None
            if vc is None or not vc.is_connected():
                if guild_id in self._voice_recovering:
                    return
                _log.info("[guild=%s] play_next: нет голосового соединения, очищаю очередь", guild_id)
                self.clear_guild(guild_id)
                return

            q = self.queues.get(guild_id, [])
            state = self.play_state.get(guild_id)
            if state is not None and state.pop("track_ended_quick", False):
                # Трек оборвался за < TRACK_MIN_RETRY_SEC: возвращаем его в
                # начало очереди (до MAX_TRACK_RETRIES попыток), а не жжём
                # очередь — пайплайн мог временно сбоить (молчаливый EOF).
                hist = self.history.get(guild_id)
                if hist and not vc.is_playing():
                    entry = hist[-1]
                    if entry.get("retries", 0) < MAX_TRACK_RETRIES:
                        entry["retries"] = entry.get("retries", 0) + 1
                        q.insert(0, entry)
                        hist.pop()
                        nav = self.pl_nav.get(guild_id)
                        if nav is not None:
                            nav["index"] = max(0, nav["index"] - 1)
                        _log.warning(
                            "[guild=%s] возвращаю трек в начало очереди (попытка %d/%d): %s",
                            guild_id, entry["retries"], MAX_TRACK_RETRIES, entry["title"],
                        )
            if not q:
                if self.loop_on.get(guild_id, True):
                    nav = self.pl_nav.get(guild_id)
                    q = _cycle_entries(nav, self.history.get(guild_id))
                    if q:
                        self.queues[guild_id] = q
                        if nav is not None:
                            nav["items"] = list(q)
                            nav["index"] = 0
                        _log.info("[guild=%s] loop: очередь закончилась, повторяю %d треков", guild_id, len(q))
                    else:
                        self.clear_guild(guild_id)
                        await vc.disconnect()
                        return
                else:
                    self.clear_guild(guild_id)
                    await vc.disconnect()
                    return

            entry = q[0]
            if entry.get("url") is None:
                if not entry.get("resolved"):
                    self._schedule_prefetch(guild_id)
                fut = entry.get("future")
                if not entry.get("error") and (fut is None or fut.done()):
                    # Future так и не создан префетчем (гонка) или отменён —
                    # резолвим трек здесь и сейчас, не полагаясь на префетч.
                    _log.info("[guild=%s] резолвлю на месте: %s", guild_id, entry["title"])
                    entry["resolved"] = False
                    entry["future"] = asyncio.create_task(self._resolve_entry(guild_id, entry))
                    fut = entry["future"]
                self._schedule_prefetch(guild_id)
                if fut is not None and not fut.done():
                    await self.report(
                        guild_id,
                        f"Сейчас играет: **{esc(entry['title'])}** ⏳ "
                        "(скачиваю трек — начну, когда будет готов)",
                    )
                if fut is not None:
                    try:
                        await fut
                    except asyncio.CancelledError:
                        # Future отменён извне (например !remove в момент резолва) —
                        # перечитываем очередь, а не выходим: иначе очередь непуста,
                        # но играть некому (after-колбэка не будет).
                        continue
                if not q or q[0] is not entry:
                    # Очередь мутировали во время резолва (!remove/!move/!shuffle):
                    # entry больше не первый — перечитываем с начала.
                    continue
                if entry.get("url") is None:
                    q.pop(0)
                    nav = self.pl_nav.get(guild_id)
                    if nav is not None:
                        nav["index"] += 1
                    err = entry.get("error") or "неизвестная ошибка"
                    _log.warning("[guild=%s] пропускаю трек %s: %s", guild_id, entry["title"], err)
                    await self.report(
                        guild_id,
                        f"Не удалось получить: **{esc(entry['title'])}** — "
                        f"{friendly_error(err)}. Пропускаю.",
                    )
                    continue

            if vc.is_playing():
                _log.info("[guild=%s] play_next: уже что-то играет, выхожу (гонка двух play_next)", guild_id)
                return
            q.pop(0)
            nav = self.pl_nav.get(guild_id)
            if nav is not None:
                nav["index"] += 1
            hist = self.history.setdefault(guild_id, [])
            hist.append(entry)
            del hist[:-HISTORY_LIMIT]

            if self.ffmpeg is None:
                _log.error("[guild=%s] ffmpeg не найден в системе", guild_id)
                await self.report(guild_id, "ffmpeg не найден в системе — установи его (см. AGENTS.md).")
                return
            try:
                source_url = entry["url"]
                # reconnect-опции нужны только для http-потоков, для локальных
                # файлов ffmpeg на них ругается.
                before_options = (
                    FFMPEG_BEFORE_OPTIONS
                    if str(source_url).startswith(("http://", "https://"))
                    else None
                )
                # Нормализация громкости: измерение в потоке (блокирующий ffmpeg
                # на ленивом пути ~1-3 с), компенсация — -af volume=<gain>dB.
                gain = await asyncio.to_thread(
                    gain_for_path, self.ffmpeg, source_url, self.cache.dir
                )
                vol = self.volumes.get(guild_id, DEFAULT_VOLUME)
                if vol != 1.0 and vol > 0:
                    vol_db = 20 * math.log10(vol)
                    gain = (gain or 0) + vol_db
                elif vol == 0:
                    gain = -100  # практически тишина
                options = f"-af volume={gain:.2f}dB" if gain else None
                source = discord.FFmpegPCMAudio(
                    source_url,
                    executable=self.ffmpeg,
                    before_options=before_options,
                    options=options,
                )
            except Exception as exc:
                _log.exception("[guild=%s] ffmpeg не запустился", guild_id)
                await self.report(guild_id, f"Ошибка воспроизведения: {exc}. Пропускаю.")
                continue

            try:
                vc.play(source, after=lambda err: self._on_track_end(guild_id, err))
            except Exception as exc:
                _log.exception("[guild=%s] не удалось начать воспроизведение: %s", guild_id, exc)
                return
            if gain:
                _log.info("[guild=%s] играю: %s (gain %+.2f дБ)", guild_id, entry["title"], gain)
            else:
                _log.info("[guild=%s] играю: %s", guild_id, entry["title"])
            state = self.play_state.get(guild_id)
            if state is not None:
                state["title"] = entry["title"]
                state["page_url"] = entry.get("page_url")
                state["duration"] = entry.get("duration")
                state["track_started_at"] = time.monotonic()
            text = f"Сейчас играет: **{esc(entry['title'])}**"
            if entry.get("page_url"):
                text += f"\n{entry['page_url']}"
            await self.report(guild_id, text)
            return

    # ---------- кнопки на сообщении «Сейчас играет» ----------

    def _guild_vc(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        return guild.voice_client if guild is not None else None

    def _now_view(self, guild_id: int) -> NowPlayingView:
        """View кнопок плеера, привязанная к сообщению «Сейчас играет» — новая
        view на каждую сессию/!now (старая останавливается и снимается).

        Кнопки переживают смену треков: report() редактирует сообщение без
        передачи view, components при этом сохраняются. timeout=None, чтобы
        кнопки не отключались через 3 минуты.
        """
        state = self.play_state.setdefault(guild_id, {"channel": None, "now_message": None})
        view = state.get("view")
        if view is None:
            view = NowPlayingView(self, guild_id)
            state["view"] = view
        return view

    def skip_current(self, guild_id: int) -> str:
        """Следующий трек (кнопка ⏭, логика как !skip): "ok" / "nothing" / "last"."""
        vc = self._guild_vc(guild_id)
        if vc is None or not vc.is_playing():
            return "nothing"
        nav = self.pl_nav.get(guild_id)
        if (
            nav is not None
            and not self.queues.get(guild_id)
            and not self.loop_on.get(guild_id, True)
        ):
            return "last"
        vc.stop()
        self.spawn(self.play_next(guild_id))
        return "ok"

    def prev_current(self, guild_id: int) -> str:
        """Предыдущий трек (кнопка ⏮ и !prev): "ok" / "nothing" / "no_prev".

        В плейлисте откатываемся по курсору (index -= 2), в простой очереди —
        по истории с возвратом текущего трека в очередь. В обоих случаях ⏭
        после ⏮ снова даёт тот же трек, а не перескакивает через него.
        """
        vc = self._guild_vc(guild_id)
        if vc is None or not vc.is_playing():
            return "nothing"
        nav = self.pl_nav.get(guild_id)
        queue = self.queues.get(guild_id, [])
        if nav is not None:
            res = _rewind_playlist(nav["items"], nav["index"], queue)
            if res is None:
                return "no_prev"
            q, index = res
            self.queues[guild_id] = q
            nav["index"] = index
        else:
            res = _rewind_queue(self.history.get(guild_id, []), queue)
            if res is None:
                return "no_prev"
            h, q = res
            self.history[guild_id] = h
            self.queues[guild_id] = q
            self.sync_nav_after_mutation(guild_id)
        vc.stop()
        self.spawn(self.play_next(guild_id))
        return "ok"

    def pause_toggle(self, guild_id: int) -> str:
        """Пауза/продолжить (кнопка ⏯): "ok" / "nothing"."""
        vc = self._guild_vc(guild_id)
        if vc is None:
            return "nothing"
        if vc.is_playing():
            vc.pause()
            return "ok"
        if vc.is_paused():
            vc.resume()
            return "ok"
        return "nothing"

    def loop_toggle(self, guild_id: int) -> bool:
        """Переключить цикл (кнопка 🔁, логика как !loop); вернуть новое значение."""
        self.loop_on[guild_id] = not self.loop_on.get(guild_id, True)
        return self.loop_on[guild_id]

    async def stop_and_leave(self, guild_id: int) -> None:
        """Остановить и выйти (кнопка ⏹): сброс состояния + disconnect."""
        self.clear_guild(guild_id)
        vc = self._guild_vc(guild_id)
        if vc is not None:
            try:
                await vc.disconnect()
            except Exception:
                _log.exception("не удалось выйти при остановке (guild=%s)", guild_id)

    # ---------- добавление треков ----------

    async def enqueue_single(
        self, message, url: str, save: bool = True, title: Optional[str] = None
    ) -> None:
        """Поставить трек по ссылке (или уже известному page_url) в очередь.

        Ленивый путь: не ждём полной докачки — отвечаем сразу, трек
        докачивается префетчем и заигрывает, как только готов. При save=True
        трек после успешного резолва сохраняется в плейлист default
        (см. _resolve_entry, флаг save_to_default).
        """
        vc, err = await self.ensure_voice(message)
        if err is not None:
            await message.channel.send(err)
            return

        entry = _lazy_entry(url, title)
        if save:
            entry["save_to_default"] = True
        state = self.play_state.setdefault(message.guild.id, {})
        state["channel"] = message.channel
        self.queues.setdefault(message.guild.id, []).append(entry)
        self._schedule_prefetch(message.guild.id)

        if vc.is_playing():
            label = esc(entry["title"]) if title else "трек"
            await message.channel.send(f"В очередь: **{label}** ⏳ — скачается и заиграет следом.")
        else:
            state["new_session"] = True
            if title:
                await message.channel.send(
                    f"⏳ Скачиваю: **{esc(title)}** — заиграет, как будет готов."
                )
            else:
                await message.channel.send("⏳ Скачиваю трек — заиграет, как будет готов.")
            self.spawn(self.play_next(message.guild.id))

    async def enqueue_local(self, message, path) -> None:
        """Поставить локальный аудиофайл (вложение из #music) в очередь."""
        item = _local_entry(path)
        vc, err = await self.ensure_voice(message)
        if err is not None:
            await message.channel.send(err)
            return
        state = self.play_state.setdefault(message.guild.id, {})
        state["channel"] = message.channel
        self.queues.setdefault(message.guild.id, []).append(item)
        if vc.is_playing():
            await message.channel.send(f"В очередь: **{esc(item['title'])}**")
        else:
            state["new_session"] = True
            self.spawn(self.play_next(message.guild.id))

    async def play_playlist(self, ctx, name: str, start: int = 1, shuffle: bool = False) -> None:
        tracks = self.db.get_playlist(ctx.guild.id, name)
        if tracks is None:
            await ctx.send(f"Плейлист **{esc(name)}** не найден.")
            return
        if not tracks:
            await ctx.send(f"Плейлист **{esc(name)}** пуст.")
            return
        if not shuffle and (start < 1 or start > len(tracks)):
            await ctx.send(
                f"Трек {start} не найден — в плейлисте **{esc(name)}** всего {len(tracks)} треков."
            )
            return
        if shuffle:
            random.shuffle(tracks)
        else:
            tracks = rotate_tracks(tracks, start)

        entries = []
        skipped = 0
        for url, title, duration in tracks:
            if url.startswith(LOCAL_URL_PREFIX):
                path = local_path(url)
                if path.is_file():
                    entry = _local_entry(path)
                    entry["title"] = title or path.stem
                    entries.append(entry)
                else:
                    skipped += 1
            else:
                entry = _lazy_entry(url, title)
                entry["duration"] = duration
                entries.append(entry)

        if not entries:
            await ctx.send(
                "В плейлисте не осталось доступных треков (локальные файлы удалены)."
            )
            return
        if skipped:
            await ctx.send(
                f"Пропускаю {skipped} локальных треков: файлы не найдены на сервере."
            )

        vc, err = await self.ensure_voice(ctx.message)
        if err is not None:
            await ctx.send(err)
            return
        if vc.is_playing():
            vc.stop()

        self._cancel_fetches(ctx.guild.id)
        state = self.play_state.setdefault(ctx.guild.id, {})
        state["channel"] = ctx.channel
        state["new_session"] = True
        state["track_ended_quick"] = False
        self.queues[ctx.guild.id] = entries
        # Сброс истории: цикл при пустой очереди пересобирается из неё
        # (play_next), иначе треки из прошлых сессий/плейлистов подмешиваются
        # в новый плейлист.
        self.history[ctx.guild.id] = []
        self.last_played[ctx.guild.id] = name
        self.pl_nav[ctx.guild.id] = {
            "name": name,
            "items": list(self.queues[ctx.guild.id]),
            "index": 0,
        }
        self._schedule_prefetch(ctx.guild.id)

        _log.info(
            "[guild=%s] плейлист %s%s: %d треков, предзагрузка по ходу",
            ctx.guild.id,
            name, " (shuffle)" if shuffle else "", len(entries),
        )
        if shuffle:
            await ctx.send(
                f"Начинаю плейлист **{esc(name)}** вперемешку ({len(entries)} треков). "
                "Треки докачиваются в фоне по ходу проигрывания."
            )
        elif start > 1:
            await ctx.send(
                f"Начинаю плейлист **{esc(name)}** с трека {start} ({len(entries)} треков, "
                "дальше по кругу). Треки докачиваются в фоне по ходу проигрывания."
            )
        else:
            await ctx.send(
                f"Начинаю плейлист **{esc(name)}** ({len(entries)} треков). "
                "Треки докачиваются в фоне по ходу проигрывания."
            )
        if not vc.is_playing():
            self.spawn(self.play_next(ctx.guild.id))

    # ---------- поиск по YouTube (выбор реакциями) ----------

    SEARCH_NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
    SEARCH_CANCEL = "❌"

    async def start_search(
        self, guild_id: int, user_id: int, channel, query: str, silent: bool = False
    ) -> None:
        """Показать результаты поиска и ждать клик по реакции (или таймаут).

        silent=True (свободный текст в #music): при слишком частом поиске
        молча игнорируем, чтобы разговор в канале не превращался в DoS
        для yt-dlp (общий executor) и не упирался в rate limit YouTube.
        """
        now = time.monotonic()
        last = self._last_search.get(guild_id, 0.0)
        if now - last < SEARCH_COOLDOWN:
            if not silent:
                try:
                    await channel.send("Поиск слишком часто — подожди пару секунд.")
                except Exception:
                    pass
            return
        self._last_search[guild_id] = now

        self._clear_pending_search(guild_id)
        limit = min(SEARCH_RESULTS, len(self.SEARCH_NUMBERS))
        try:
            results = await search_youtube(query, limit)
        except Exception as exc:
            _log.warning("[guild=%s] поиск: %s", guild_id, exc)
            try:
                await channel.send(f"Не удалось найти: {friendly_error(exc)}")
            except Exception:
                pass
            return
        if not results:
            try:
                await channel.send(f"Ничего не нашёл по запросу «{esc(query)}».")
            except Exception:
                pass
            return

        lines = [f"Результаты поиска по «**{esc(query)}**»:"]
        for i, r in enumerate(results):
            dur = fmt_duration(r.get("duration"))
            uploader = r.get("uploader")
            line = f"{self.SEARCH_NUMBERS[i]} **{esc(r['title'])}**"
            if dur:
                line += f" — {dur}"
            if uploader:
                line += f" ({esc(uploader)})"
            lines.append(line)
        lines.append("Нажми цифру, чтобы сыграть, ❌ — отмена. Ссылку на трек пришлю, когда он заиграет.")

        try:
            msg = await channel.send("\n".join(lines))
        except Exception:
            _log.exception("не удалось отправить результаты поиска")
            return
        emojis = [self.SEARCH_NUMBERS[i] for i in range(len(results))] + [self.SEARCH_CANCEL]
        for emoji in emojis:
            try:
                await msg.add_reaction(emoji)
            except Exception:
                _log.warning("не удалось добавить реакцию %s", emoji)
        expire = asyncio.create_task(self._search_expire(guild_id, msg))
        self.pending_search[guild_id] = {
            "message": msg,
            "user_id": user_id,
            "results": results,
            "expire_task": expire,
        }

    async def _search_expire(self, guild_id: int, msg) -> None:
        await asyncio.sleep(SEARCH_TIMEOUT)
        pending = self.pending_search.get(guild_id)
        if pending is None or pending["message"].id != msg.id:
            return
        self.pending_search.pop(guild_id, None)
        try:
            await msg.clear_reactions()
        except Exception:
            pass
        try:
            await msg.edit(content=msg.content + "\n\nПоиск устарел — запусти заново.")
        except Exception:
            pass

    def _clear_pending_search(self, guild_id: int, cleanup_message: bool = True) -> None:
        """Отменить ожидание выбора (новый поиск, выбор сделан, остановка бота).

        cleanup_message=True: старое сообщение приводим в неактивное состояние
        (снять реакции, дописать пометку), иначе оно остаётся кликабельным,
        но клики молча игнорируются.
        """
        pending = self.pending_search.pop(guild_id, None)
        if pending is None:
            return
        task = pending.get("expire_task")
        if task is not None and not task.done():
            task.cancel()
        if cleanup_message:
            msg = pending.get("message")
            if msg is not None:
                self.spawn(self._cleanup_search_message(msg))

    async def _cleanup_search_message(self, msg) -> None:
        try:
            await msg.clear_reactions()
        except Exception:
            pass
        try:
            await msg.edit(content=msg.content + "\n\nПоиск заменён/отменён.")
        except Exception:
            pass

    async def handle_search_reaction(self, payload) -> None:
        """Клик по реакции поиска (из on_raw_reaction_add). Чужие клики снимаем."""
        if payload.guild_id is None:
            return
        pending = self.pending_search.get(payload.guild_id)
        if pending is None or pending["message"].id != payload.message_id:
            return
        if payload.user_id != pending["user_id"]:
            await self._remove_reaction(payload, payload.user_id)
            return
        name = getattr(payload.emoji, "name", "")
        if name == self.SEARCH_CANCEL:
            index = None
        else:
            try:
                index = self.SEARCH_NUMBERS.index(name)
            except ValueError:
                return
            if index >= len(pending["results"]):
                return
        # сообщение уже будет переоформлено ниже — фоновую чистку не запускаем
        self._clear_pending_search(payload.guild_id, cleanup_message=False)
        msg = pending["message"]
        try:
            await msg.clear_reactions()
        except Exception:
            pass
        if index is None:
            try:
                await msg.edit(content="Поиск отменён.")
            except Exception:
                pass
            return
        result = pending["results"][index]
        try:
            await msg.edit(
                content=f"Сыграю: **{esc(result['title'])}**",
                view=AddToPlaylistView(
                    self,
                    payload.guild_id,
                    payload.user_id,
                    {
                        "page_url": result["page_url"],
                        "title": result["title"],
                        "duration": result.get("duration"),
                    },
                ),
            )
        except Exception:
            pass
        member = msg.guild.get_member(payload.user_id)
        if member is None:
            try:
                await msg.channel.send("Не нашёл тебя на сервере — попробуй ещё раз.")
            except Exception:
                pass
            return
        stub = SimpleNamespace(author=member, guild=msg.guild, channel=msg.channel)
        await self.enqueue_single(stub, result["page_url"], save=True, title=result["title"])

    async def _remove_reaction(self, payload, user_id: int) -> None:
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            await channel.get_partial_message(payload.message_id).remove_reaction(payload.emoji, user_id)
        except Exception:
            pass
