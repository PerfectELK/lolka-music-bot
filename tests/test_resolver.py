"""Тесты resolver.py: resolve_cached, fetch_info, search_youtube (моки на yt-dlp)."""

from unittest.mock import MagicMock, patch

import pytest

from resolver import _fetch_info_sync, _search_sync


@pytest.mark.asyncio
async def test_resolve_cached_calls_cache_ensure():
    cache = MagicMock()
    cache.ensure.return_value = ("/cache/test.opus", "Трек", "https://youtu.be/abc", 120)
    mock_loop = MagicMock()

    async def fake_run_in_executor(exc, fn, *args):
        return fn(*args)

    mock_loop.run_in_executor = fake_run_in_executor

    with patch("resolver.asyncio.get_running_loop", return_value=mock_loop):
        from resolver import resolve_cached
        path, title, canonical, dur = await resolve_cached(cache, "https://youtu.be/abc")
        assert path == "/cache/test.opus"
        assert title == "Трек"
        assert canonical == "https://youtu.be/abc"
        assert dur == 120
        cache.ensure.assert_called_once_with("https://youtu.be/abc", on_info=None)


@pytest.mark.asyncio
async def test_resolve_cached_passes_on_info():
    cache = MagicMock()
    on_info = lambda info: None
    mock_loop = MagicMock()

    async def fake_run_in_executor(exc, fn, *args):
        return fn(*args)

    mock_loop.run_in_executor = fake_run_in_executor

    with patch("resolver.asyncio.get_running_loop", return_value=mock_loop):
        from resolver import resolve_cached
        await resolve_cached(cache, "https://youtu.be/abc", on_info=on_info)
        cache.ensure.assert_called_once_with("https://youtu.be/abc", on_info=on_info)


def test_fetch_info_sync_returns_metadata():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "title": "Тестовый трек",
            "webpage_url": "https://youtu.be/abc",
            "duration": 90,
        }

        title, page_url, duration = _fetch_info_sync("https://youtu.be/abc")
        assert title == "Тестовый трек"
        assert page_url == "https://youtu.be/abc"
        assert duration == 90


def test_fetch_info_sync_missing_fields():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        # Должен быть truthy, но без title и duration
        mock_ydl.extract_info.return_value = {"webpage_url": "https://youtu.be/abc"}

        title, page_url, duration = _fetch_info_sync("https://youtu.be/abc")
        assert title == "неизвестно"
        assert page_url == "https://youtu.be/abc"
        assert duration is None


def test_fetch_info_sync_empty_info_raises():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = None

        with pytest.raises(RuntimeError):
            _fetch_info_sync("https://youtu.be/abc")


def test_search_sync_returns_results():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Трек 1",
                    "webpage_url": "https://youtu.be/abc",
                    "duration": 120,
                    "uploader": "Канал",
                },
                {
                    "title": "Трек 2",
                    "webpage_url": "https://youtu.be/def",
                    "duration": 90,
                    "uploader": "Канал 2",
                },
            ]
        }

        results = _search_sync("запрос", limit=2)
        assert len(results) == 2
        assert results[0]["title"] == "Трек 1"
        assert results[0]["page_url"] == "https://youtu.be/abc"
        assert results[0]["duration"] == 120
        assert results[0]["uploader"] == "Канал"


def test_search_sync_skips_missing_entries():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                None,
                {"title": None, "url": None},
                {"title": "Годный", "webpage_url": "https://youtu.be/xyz"},
            ]
        }

        results = _search_sync("запрос", limit=3)
        assert len(results) == 1
        assert results[0]["title"] == "Годный"


def test_search_sync_empty():
    with patch("resolver.yt_dlp.YoutubeDL") as mock_ydl_class:
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}

        results = _search_sync("запрос", limit=5)
        assert results == []
