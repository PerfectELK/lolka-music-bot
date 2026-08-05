"""Тесты чистых хелперов пикера плейлистов: пагинация, добавление без дублей."""

import pytest

from playlist_db import PlaylistDB
from playlist_picker import add_track_dedupe
from ui_utils import paginate

GUILD = 12345

TRACK = {"page_url": "https://youtu.be/abc", "title": "Трек", "duration": 90}


@pytest.fixture()
def db(tmp_path):
    return PlaylistDB(tmp_path / "test.db")


def test_paginate_playlists_basic():
    names = [f"p{i}" for i in range(3)]
    chunk, pages, has_prev, has_next, _ = paginate(names, 0, 20, zero_based=True)
    assert chunk == names
    assert pages == 1
    assert has_prev is False
    assert has_next is False


def test_paginate_playlists_pages():
    names = [f"p{i}" for i in range(25)]
    chunk, pages, has_prev, has_next, _ = paginate(names, 0, 20, zero_based=True)
    assert len(chunk) == 20
    assert pages == 2
    assert has_prev is False
    assert has_next is True
    chunk, pages, has_prev, has_next, _ = paginate(names, 1, 20, zero_based=True)
    assert chunk == [f"p{i}" for i in range(20, 25)]
    assert has_prev is True
    assert has_next is False


def test_paginate_playlists_exact_page():
    names = [f"p{i}" for i in range(20)]
    chunk, pages, _, _, _ = paginate(names, 0, 20, zero_based=True)
    assert len(chunk) == 20
    assert pages == 1


def test_paginate_playlists_empty():
    chunk, pages, has_prev, has_next, _ = paginate([], 0, 20, zero_based=True)
    assert chunk == []
    assert pages == 1
    assert has_prev is False
    assert has_next is False


def test_paginate_playlists_out_of_range():
    names = [f"p{i}" for i in range(5)]
    chunk, pages, has_prev, has_next, _ = paginate(names, 5, 20, zero_based=True)
    assert chunk is None
    assert pages == 1
    assert has_prev is False
    assert has_next is False
    chunk, _, _, _, _ = paginate(names, -1, 20, zero_based=True)
    assert chunk is None


def test_add_track_dedupe_added(db):
    db.create_playlist(GUILD, "lofi")
    assert add_track_dedupe(db, GUILD, "lofi", TRACK) == "added"
    assert db.get_playlist(GUILD, "lofi") == [(TRACK["page_url"], TRACK["title"], TRACK["duration"])]


def test_add_track_dedupe_dup(db):
    db.create_playlist(GUILD, "lofi")
    add_track_dedupe(db, GUILD, "lofi", TRACK)
    assert add_track_dedupe(db, GUILD, "lofi", TRACK) == "dup"
    tracks = db.get_playlist(GUILD, "lofi")
    assert len(tracks) == 1


def test_add_track_dedupe_missing(db):
    assert add_track_dedupe(db, GUILD, "nope", TRACK) == "missing"


def test_add_track_dedupe_dup_without_duration(db):
    db.create_playlist(GUILD, "lofi")
    no_dur = {"page_url": TRACK["page_url"], "title": TRACK["title"]}
    assert add_track_dedupe(db, GUILD, "lofi", no_dur) == "added"
    assert add_track_dedupe(db, GUILD, "lofi", TRACK) == "dup"
