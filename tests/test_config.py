"""Тесты YOUTUBE_RE: что принимается, а что должно отвергаться."""

from config import YOUTUBE_RE

GOOD = [
    "https://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtube.com/shorts/dQw4w9WgXcQ",
    "https://youtube.com/embed/dQw4w9WgXcQ",
    "https://youtube.com/live/dQw4w9WgXcQ",
    "https://youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
    "текст https://youtu.be/dQw4w9WgXcQ ещё текст",
]

BAD = [
    "https://youtube.com/redirect?q=https://evil.com",
    "https://youtube.com/playlist?list=PL123",
    "https://youtube.com/watch?v=short",
    "https://youtu.be/short",
    "https://example.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com",
    "просто текст без ссылки",
    "",
]


def test_good_urls():
    for url in GOOD:
        m = YOUTUBE_RE.search(url)
        assert m is not None, url


def test_bad_urls():
    for url in BAD:
        assert YOUTUBE_RE.search(url) is None, url
