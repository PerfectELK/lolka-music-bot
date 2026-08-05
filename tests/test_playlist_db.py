"""Тесты PlaylistDB: CRUD, сдвиг позиций, дедупликация, миграция схемы."""

import sqlite3

import pytest

from playlist_db import PlaylistDB

GUILD = 12345


@pytest.fixture()
def db(tmp_path):
    return PlaylistDB(tmp_path / "test.db")


def test_create_and_duplicate(db):
    assert db.create_playlist(GUILD, "lofi") is True
    assert db.create_playlist(GUILD, "lofi") is False
    assert db.list_playlists(GUILD) == [("lofi", 0)]


def test_ensure_playlist(db):
    assert db.ensure_playlist(GUILD, "x") is not None
    assert db.ensure_playlist(GUILD, "x") == db.ensure_playlist(GUILD, "x")


def test_rename(db):
    db.create_playlist(GUILD, "old")
    assert db.rename_playlist(GUILD, "old", "new") is True
    assert db.rename_playlist(GUILD, "old", "new") is False
    assert db.rename_playlist(GUILD, "new", "new") is False
    assert db.get_playlist(GUILD, "new") == []


def test_delete(db):
    db.create_playlist(GUILD, "tmp")
    assert db.delete_playlist(GUILD, "tmp") is True
    assert db.delete_playlist(GUILD, "tmp") is False


def test_add_track_unknown_playlist(db):
    assert db.add_track(GUILD, "nope", "u1", "t1") is False


def test_add_remove_shift_positions(db):
    db.create_playlist(GUILD, "p")
    for i in range(1, 5):
        assert db.add_track(GUILD, "p", f"u{i}", f"t{i}", duration=i * 60) is True
    tracks = db.get_playlist(GUILD, "p")
    assert tracks == [("u1", "t1", 60), ("u2", "t2", 120), ("u3", "t3", 180), ("u4", "t4", 240)]

    assert db.remove_track(GUILD, "p", 2) is True
    tracks = db.get_playlist(GUILD, "p")
    assert [(u, t) for u, t, _ in tracks] == [("u1", "t1"), ("u3", "t3"), ("u4", "t4")]

    assert db.remove_track(GUILD, "p", 99) is False
    assert db.remove_track(GUILD, "p", 0) is False


def test_add_track_duration_none(db):
    db.create_playlist(GUILD, "p")
    db.add_track(GUILD, "p", "u1", "t1")
    assert db.get_playlist(GUILD, "p") == [("u1", "t1", None)]


def test_has_track(db):
    db.create_playlist(GUILD, "p")
    db.add_track(GUILD, "p", "u1", "t1")
    assert db.has_track(GUILD, "p", "u1") is True
    assert db.has_track(GUILD, "p", "u2") is False
    assert db.has_track(GUILD, "nope", "u1") is False


def test_referenced_local_files(db):
    db.create_playlist(GUILD, "p")
    db.add_track(GUILD, "p", "local:a.mp3", "a")
    db.add_track(GUILD, "p", "https://youtube.com/watch?v=x", "x")
    assert db.referenced_local_files() == {"a.mp3"}


def test_migration_adds_duration_column(tmp_path):
    """Старая БД без колонки duration должна получить её при открытии."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (guild_id, name)
        );
        CREATE TABLE playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            UNIQUE (playlist_id, position)
        );
        """
    )
    conn.commit()
    conn.close()

    db = PlaylistDB(path)
    db.create_playlist(GUILD, "p")
    db.add_track(GUILD, "p", "u1", "t1", duration=42)
    assert db.get_playlist(GUILD, "p") == [("u1", "t1", 42)]
