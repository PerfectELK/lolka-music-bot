"""Константы и настройки бота."""

import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

TOKEN_ENV = "LOLKA_TOKEN"
TOKEN_FILE = BASE_DIR / "token.txt"
LOCK_FILE = BASE_DIR / "bot.lock"

MUSIC_CHANNEL_NAMES = {"music", "музыка"}
# Только видео-паттерны: youtube.com/watch, youtu.be, shorts/embed/live.
# Не пускать любые пути youtube.com — yt-dlp следует редиректам (SSRF через
# /redirect?q=<target>).
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:"
    r"youtube\.com/watch\?(?:[^#\s]*&)?v=[\w-]{11}"
    r"|youtu\.be/[\w-]{11}"
    r"|youtube\.com/(?:shorts|embed|live)/[\w-]{11}"
    r")(?:[^\s]*)?"
)

DEFAULT_PLAYLIST = "default"
HISTORY_LIMIT = 30
# Сколько треков заранее докачивать в фоне (играющий + следующие). 3 — плейлист
# стартует быстрее, лишний префетч дёшев (кеш LRU, см. CACHE_MAX_BYTES).
PREFETCH_AHEAD = 3
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

# Защита от «сжигания» очереди при сбое аудио-пайплайна (молчаливый EOF
# источника через 1-2 с после старта): трек, оборвавшийся быстрее
# TRACK_MIN_RETRY_SEC без ошибки, возвращается в начало очереди
# (до MAX_TRACK_RETRIES попыток) вместо перехода к следующему.
TRACK_MIN_RETRY_SEC = 8.0
MAX_TRACK_RETRIES = 3

# Нормализация громкости (EBU R128, ReplayGain-подход): трек измеряется один
# раз фильтром ebur128, при воспроизведении добавляется компенсирующий
# фильтр -af volume=<gain>dB. Целевая громкость — стандарт YouTube.
NORMALIZE_ENABLED = True
NORMALIZE_TARGET_LUFS = -14.0
NORMALIZE_MAX_GAIN_DB = 15.0
NORMALIZE_TP_LIMIT_DB = -1.0

# Выход при пустом голосовом канале: пауза (сек) перед выходом — шанс
# человеку вернуться (мигнул интернет); если за это время кто-то зашёл —
# выход отменяется.
EMPTY_CHANNEL_GRACE = 10

# Поиск по YouTube: сколько результатов показывать, сколько ждать клик
# по реакции и минимальный интервал между поисками одной глиды.
SEARCH_RESULTS = 5
SEARCH_TIMEOUT = 120
SEARCH_COOLDOWN = 10

# Локальный кеш скачанных треков (Opus 96k), лимит размера с LRU-очисткой.
# 1.5 ГБ — под свободное место на сервере (2.6 ГБ из 20 ГБ); поднять при
# нехватке кеша или большем диске.
CACHE_DIR = BASE_DIR / "cache"
CACHE_MAX_BYTES = int(1.5 * 1024**3)

# Локальные аудио-вложения из #music и !pl add: файлы кладутся в uploads/
# (<attachment_id>_<filename>) и играются напрямую через ffmpeg. В плейлистах
# они хранятся как local:<имя файла>. LRU-очистка по mtime при превышении
# суммарного лимита; файлы, на которые ссылаются плейлисты, не удаляются.
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_MAX_BYTES = int(0.5 * 1024**3)
UPLOADS_MAX_FILE = 64 * 1024 * 1024
AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".oga",
    ".flac", ".wav", ".webm", ".wma", ".mp4",
}
LOCAL_URL_PREFIX = "local:"

# Per-guild громкость.
DEFAULT_VOLUME = 1.0
VOLUME_MIN = 0.0
VOLUME_MAX = 2.0

# Пул потоков скачивания yt-dlp: при нескольких одновременно играющих глидах
# префетчится до PREFETCH_AHEAD=3 треков на глиду — все они стоят в очереди
# этого пула (старт трека во второй глиде может ждать слот). Поднять при
# большом числе серверов (5+); рост пула увеличивает давление на rate limit
# YouTube.
YTDLP_DOWNLOAD_WORKERS = 6
YTDLP_META_WORKERS = 2

YTDLP_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 20,
    # Сервер бота сидит на IPv6-адресе с мёртвым маршрутом к YouTube: yt-dlp
    # (urllib, без happy-eyeballs) сначала пробует IPv6 — каждый запрос висит
    # ~25 с (socket_timeout) и только потом падает на IPv4. Итог: докачка
    # трека ~50 с вместо ~2 с. Принудительный IPv4 убирает задержку целиком
    # (это CLI-опция -4/--force-ipv4: source_address='0.0.0.0').
    "source_address": "0.0.0.0",
}
