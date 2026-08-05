"""Хранилище прав бота в SQLite (файл permissions.db в корне проекта).

Таблицы:
  owners   — владелец бота на сервере (кто добавил бота): одна строка на глиду
  grants   — выданные права: строка (guild_id, user_id, perm)

Права хранятся строками — расширение (новое право) = новая строка в grants,
миграции не нужны. Владелец не хранится в grants: can() возвращает True для
владельца по любой проверке (права владельца неотзываемы).

Паттерн playlist_db.py: sqlite3 с check_same_thread=False + threading.Lock,
все вызовы — синхронные, из асинхронного кода через asyncio.to_thread.
"""

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "permissions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    guild_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'audit'
);
CREATE TABLE IF NOT EXISTS grants (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    perm TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, perm)
);
"""

PERM_NAMES = ["control", "playlists", "permissions"]
PERM_LABELS = {
    "control": "🎛 Управление воспроизведением",
    "playlists": "🎵 Управление плейлистами",
    "permissions": "👑 Выдача прав",
}


def deny_text(perm: str) -> str:
    """Сообщение об отказе: «нет права» + как получить право."""
    label = PERM_LABELS.get(perm, perm)
    return (
        f"❌ У тебя нет права **{label}**. "
        "Выдать права может владелец бота — `!perms`."
    )


class PermissionsDB:
    def __init__(self, path: Path = DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- владелец ----------

    def get_owner(self, guild_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id FROM owners WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            return row["user_id"] if row else None

    def set_owner(self, guild_id: int, user_id: int, source: str = "audit") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO owners (guild_id, user_id, source) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET user_id = excluded.user_id, "
                "source = excluded.source",
                (guild_id, user_id, source),
            )
            self._conn.commit()

    # ---------- права ----------

    def can(self, guild_id: int, user_id: int, perm: str) -> bool:
        if user_id == self.get_owner(guild_id):
            return True
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM grants WHERE guild_id = ? AND user_id = ? AND perm = ? "
                "LIMIT 1",
                (guild_id, user_id, perm),
            ).fetchone()
            return row is not None

    def grant(self, guild_id: int, user_id: int, perms: list[str]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO grants (guild_id, user_id, perm) VALUES (?, ?, ?)",
                [(guild_id, user_id, p) for p in perms],
            )
            self._conn.commit()

    def revoke(self, guild_id: int, user_id: int, perms: list[str] | None) -> None:
        """Отозвать права: по списку или все (perms=None). Владельца не трогает."""
        if user_id == self.get_owner(guild_id):
            return
        with self._lock:
            if perms is None:
                self._conn.execute(
                    "DELETE FROM grants WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            else:
                self._conn.executemany(
                    "DELETE FROM grants WHERE guild_id = ? AND user_id = ? AND perm = ?",
                    [(guild_id, user_id, p) for p in perms],
                )
            self._conn.commit()

    def grants_for(self, guild_id: int, user_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT perm FROM grants WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchall()
            return {row["perm"] for row in rows}

    def all_grants(self, guild_id: int) -> dict[int, set[str]]:
        """Все выданные права по пользователям (для панели !perms)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, perm FROM grants WHERE guild_id = ? ORDER BY user_id",
                (guild_id,),
            ).fetchall()
            out: dict[int, set[str]] = {}
            for row in rows:
                out.setdefault(row["user_id"], set()).add(row["perm"])
            return out
