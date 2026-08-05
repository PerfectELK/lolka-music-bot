"""Тесты прав движка: ensure_owner (аудит-лог → владелец сервера → None),
has_perm/is_owner как обёртки над PermissionsDB."""

import asyncio
from types import SimpleNamespace

import pytest

from engine import MusicEngine
from permissions_db import PERM_NAMES, PermissionsDB


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeEntry:
    def __init__(self, user):
        self.user = user


class _FakeGuild:
    def __init__(self, guild_id, entries=None, owner_id=None, error=None):
        self.id = guild_id
        self.owner_id = owner_id
        self._entries = entries or []
        self._error = error
        self.audit_calls = 0

    async def audit_logs(self, **kwargs):
        self.audit_calls += 1
        if self._error is not None:
            raise self._error
        for e in self._entries:
            yield e


@pytest.fixture()
def db(tmp_path):
    return PermissionsDB(tmp_path / "perms.db")


def _engine(db, bot_id=999):
    engine = MusicEngine.__new__(MusicEngine)
    engine.bot = SimpleNamespace(user=_FakeUser(bot_id))
    engine.perms = db
    engine._owner_locks = {}
    return engine


def test_ensure_owner_audit_log_inviter(db):
    guild = _FakeGuild(1, entries=[_FakeEntry(_FakeUser(111))], owner_id=222)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) == 111


def test_ensure_owner_skips_bot_itself(db):
    guild = _FakeGuild(
        1,
        entries=[_FakeEntry(_FakeUser(999)), _FakeEntry(_FakeUser(111))],
        owner_id=222,
    )
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) == 111


def test_ensure_owner_forbidden_falls_back_to_guild_owner(db):
    guild = _FakeGuild(1, error=PermissionError("Forbidden"), owner_id=222)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) == 222


def test_ensure_owner_empty_audit_falls_back_to_guild_owner(db):
    guild = _FakeGuild(1, entries=[], owner_id=222)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) == 222


def test_ensure_owner_user_left_audit_falls_back(db):
    # entry.user is None — добавивший ушёл с сервера
    guild = _FakeGuild(1, entries=[_FakeEntry(None)], owner_id=222)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) == 222


def test_ensure_owner_no_owner_id_keeps_none(db):
    guild = _FakeGuild(1, entries=[], owner_id=None)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    assert db.get_owner(1) is None


def test_ensure_owner_idempotent_no_second_audit_call(db):
    guild = _FakeGuild(1, entries=[_FakeEntry(_FakeUser(111))], owner_id=222)
    engine = _engine(db)
    asyncio.run(engine.ensure_owner(guild))
    asyncio.run(engine.ensure_owner(guild))
    assert guild.audit_calls == 1
    assert db.get_owner(1) == 111


def test_ensure_owner_concurrent_first_request_sets_once(db):
    # гонка двух параллельных вызовов: БД получает одного владельца
    guild = _FakeGuild(1, entries=[_FakeEntry(_FakeUser(111))], owner_id=222)
    engine = _engine(db)

    async def _race():
        await asyncio.gather(engine.ensure_owner(guild), engine.ensure_owner(guild))

    asyncio.run(_race())
    assert guild.audit_calls == 1
    assert db.get_owner(1) == 111


def test_has_perm_and_is_owner(db):
    db.set_owner(1, 111, "audit")
    db.grant(1, 222, ["control"])
    engine = _engine(db)
    assert engine.is_owner(1, 111) is True
    assert engine.is_owner(1, 222) is False
    assert engine.has_perm(1, 111, "playlists") is True  # владелец имеет всё
    assert engine.has_perm(1, 222, "control") is True
    assert engine.has_perm(1, 222, "playlists") is False
    assert engine.has_perm(2, 111, "control") is False  # другая глида


def test_has_perm_owner_has_all_perms(db):
    db.set_owner(1, 111, "audit")
    engine = _engine(db)
    for perm in PERM_NAMES:
        assert engine.has_perm(1, 111, perm) is True
