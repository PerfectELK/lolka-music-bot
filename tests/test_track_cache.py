"""Тесты TrackCache: извлечение id ролика и LRU-лимит (без сети, на фейковых файлах)."""

import json
import os
import time
from pathlib import Path

from track_cache import TrackCache, video_id


def test_video_id():
    assert video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://music.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://youtube.com/watch?v=dQw4w9WgXcQ&list=abc") == "dQw4w9WgXcQ"
    assert video_id("https://youtube.com/playlist?list=abc") is None
    assert video_id("https://example.com/video") is None
    assert video_id("") is None


def test_enforce_cap_lru(tmp_path):
    cache = TrackCache(tmp_path / "cache", max_bytes=1000)
    # три «скачанных» трека: 500 + 400 + 200 = 1100 > 1000 → удалится один самый старый
    files = []
    for i, size in enumerate((500, 400, 200)):
        p = cache.dir / f"v{i}.opus"
        p.write_bytes(b"x" * size)
        os.utime(p, (time.time() + i, time.time() + i))  # v0 старее всех
        (cache.dir / f"v{i}.json").write_text("{}", encoding="utf-8")
        files.append(p)
    cache._enforce_cap()
    assert not files[0].exists()  # самый старый удалён
    assert files[1].exists()
    assert files[2].exists()
    # sidecar тоже удалён
    assert not (cache.dir / "v0.json").exists()
    # лимит больше не превышен
    total = sum(p.stat().st_size for p in cache.dir.glob("*.opus"))
    assert total <= 1000


def test_enforce_cap_under_limit(tmp_path):
    cache = TrackCache(tmp_path / "cache", max_bytes=1000)
    p = cache.dir / "v1.opus"
    p.write_bytes(b"x" * 500)
    cache._enforce_cap()
    assert p.exists()


def test_audio_path(tmp_path):
    cache = TrackCache(tmp_path / "cache", max_bytes=1000)
    assert cache.audio_path("https://youtube.com/watch?v=dQw4w9WgXcQ").name == "dQw4w9WgXcQ.opus"


def test_ensure_old_sidecar_without_loudness(tmp_path):
    # регрессия: sidecar старого формата (без loudness) продолжает читаться
    cache = TrackCache(tmp_path / "cache", max_bytes=1000)
    vid = "dQw4w9WgXcQ"
    (cache.dir / f"{vid}.opus").write_bytes(b"x")
    (cache.dir / f"{vid}.json").write_text(
        json.dumps(
            {"title": "Старый трек", "page_url": f"https://youtube.com/watch?v={vid}"}
        ),
        encoding="utf-8",
    )
    path, title, url, duration = cache.ensure(f"https://youtube.com/watch?v={vid}")
    assert title == "Старый трек"
    assert url == f"https://youtube.com/watch?v={vid}"
    assert duration is None
    assert path == str(cache.dir / f"{vid}.opus")


def test_ensure_sidecar_with_loudness(tmp_path):
    # sidecar нового формата с loudness читается и отдаёт duration
    cache = TrackCache(tmp_path / "cache", max_bytes=1000)
    vid = "dQw4w9WgXcQ"
    (cache.dir / f"{vid}.opus").write_bytes(b"x")
    (cache.dir / f"{vid}.json").write_text(
        json.dumps(
            {
                "title": "Трек",
                "page_url": f"https://youtube.com/watch?v={vid}",
                "duration": 213,
                "loudness": {"i": -12.3, "tp": -1.0},
            }
        ),
        encoding="utf-8",
    )
    path, title, url, duration = cache.ensure(f"https://youtube.com/watch?v={vid}")
    assert title == "Трек"
    assert duration == 213
    assert path == str(cache.dir / f"{vid}.opus")


def test_make_hook_calls_on_info():
    # хук передаёт info_dict в on_info; исключения в on_info не роняют хук
    cache = TrackCache(Path(".") / "cache", max_bytes=1000)
    received = []

    def on_info(info):
        received.append(info.get("title"))

    hook = cache._make_hook(on_info)
    hook({"title": "Трек 1"})
    hook({"title": "Трек 2"})
    assert received == ["Трек 1", "Трек 2"]

    def on_info_boom(info):
        raise RuntimeError("boom")

    hook = cache._make_hook(on_info_boom)
    hook({"title": "Трек 3"})  # не должен бросить


def test_make_hook_none_on_info():
    cache = TrackCache(Path(".") / "cache", max_bytes=1000)
    hook = cache._make_hook(None)
    hook({"title": "Трек"})  # не должен бросить
