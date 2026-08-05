"""Хранилище плейлистов в SQLite (файл playlists.db в корне проекта).

Таблицы:
  playlists        — плейлисты, уникальность по (guild_id, name)
  playlist_tracks  — треки плейлиста, порядок задаётся полем position
"""

import sqlite3
import threading
from pathlib import Path

from config import LOCAL_URL_PREFIX

DB_PATH = Path(__file__).resolve().parent / "playlists.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    duration INTEGER,
    UNIQUE (playlist_id, position)
);
"""


class PlaylistDB:
    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_duration()
            self._conn.commit()

    def _migrate_duration(self) -> None:
        """Добавить колонку duration в playlist_tracks, если её нет (старые БД)."""
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(playlist_tracks)").fetchall()
        }
        if "duration" not in cols:
            self._conn.execute("ALTER TABLE playlist_tracks ADD COLUMN duration INTEGER")

    def _playlist_id(self, guild_id: int, name: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM playlists WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        ).fetchone()
        return row["id"] if row else None

    def ensure_playlist(self, guild_id: int, name: str) -> int:
        """Создать плейлист, если его нет; вернуть его id."""
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                self._conn.execute(
                    "INSERT INTO playlists (guild_id, name) VALUES (?, ?)",
                    (guild_id, name),
                )
                self._conn.commit()
                pid = self._playlist_id(guild_id, name)
            return pid

    def create_playlist(self, guild_id: int, name: str) -> bool:
        """False, если плейлист с таким именем уже есть."""
        with self._lock:
            if self._playlist_id(guild_id, name) is not None:
                return False
            self._conn.execute(
                "INSERT INTO playlists (guild_id, name) VALUES (?, ?)",
                (guild_id, name),
            )
            self._conn.commit()
            return True

    def delete_playlist(self, guild_id: int, name: str) -> bool:
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                return False
            self._conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))
            self._conn.commit()
            return True

    def rename_playlist(self, guild_id: int, name: str, new_name: str) -> bool:
        with self._lock:
            if self._playlist_id(guild_id, name) is None:
                return False
            if self._playlist_id(guild_id, new_name) is not None:
                return False
            self._conn.execute(
                "UPDATE playlists SET name = ? WHERE guild_id = ? AND name = ?",
                (new_name, guild_id, name),
            )
            self._conn.commit()
            return True

    def list_playlists(self, guild_id: int) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT p.name, COUNT(t.id) AS n
                   FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id
                   WHERE p.guild_id = ?
                   GROUP BY p.id ORDER BY p.name""",
                (guild_id,),
            ).fetchall()
            return [(row["name"], row["n"]) for row in rows]

    def has_track(self, guild_id: int, name: str, url: str) -> bool:
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                return False
            row = self._conn.execute(
                "SELECT 1 FROM playlist_tracks WHERE playlist_id = ? AND url = ? LIMIT 1",
                (pid, url),
            ).fetchone()
            return row is not None

    def add_track(self, guild_id: int, name: str, url: str, title: str, duration: int | None = None) -> bool:
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                return False
            pos_row = self._conn.execute(
                "SELECT MAX(position) AS m FROM playlist_tracks WHERE playlist_id = ?",
                (pid,),
            ).fetchone()
            pos = (pos_row["m"] or 0) + 1
            self._conn.execute(
                "INSERT INTO playlist_tracks (playlist_id, position, url, title, duration) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, pos, url, title, duration),
            )
            self._conn.commit()
            return True

    def remove_track(self, guild_id: int, name: str, position: int) -> bool:
        """Удалить трек по позиции (1-based); позиции следующих сдвигаются."""
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                return False
            cur = self._conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ? AND position = ?",
                (pid, position),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return False
            rows = self._conn.execute(
                "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                (pid,),
            ).fetchall()
            for i, row in enumerate(rows, start=1):
                self._conn.execute(
                    "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                    (i, row["id"]),
                )
            self._conn.commit()
            return True

    def get_playlist(self, guild_id: int, name: str) -> list[tuple[str, str, int | None]] | None:
        """Список (url, title, duration) в порядке очереди; None, если плейлиста нет."""
        with self._lock:
            pid = self._playlist_id(guild_id, name)
            if pid is None:
                return None
            rows = self._conn.execute(
                "SELECT url, title, duration FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
                (pid,),
            ).fetchall()
            return [(row["url"], row["title"], row["duration"]) for row in rows]

    def referenced_local_files(self) -> set[str]:
        """Имена файлов uploads/, на которые ссылаются плейлисты (url вида local:...).

        Используется LRU-очисткой uploads/, чтобы не удалять файлы из плейлистов.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT url FROM playlist_tracks WHERE url LIKE '{LOCAL_URL_PREFIX}%'"
            ).fetchall()
            return {row["url"][len(LOCAL_URL_PREFIX):] for row in rows}
