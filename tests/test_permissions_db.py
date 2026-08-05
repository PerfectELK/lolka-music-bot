"""Тесты PermissionsDB: владелец, выдача/отзыв прав, can(), deny_text."""

import pytest

from permissions_db import PERM_LABELS, PERM_NAMES, PermissionsDB, deny_text

GUILD = 12345
OWNER = 111
USER = 222


@pytest.fixture()
def db(tmp_path):
    return PermissionsDB(tmp_path / "test.db")


# ---------- владелец ----------


def test_owner_get_set(db):
    assert db.get_owner(GUILD) is None
    db.set_owner(GUILD, OWNER, "audit")
    assert db.get_owner(GUILD) == OWNER


def test_owner_overwrite(db):
    db.set_owner(GUILD, OWNER, "audit")
    db.set_owner(GUILD, 333, "guild_owner")
    assert db.get_owner(GUILD) == 333


def test_owner_can_everything_without_grants(db):
    db.set_owner(GUILD, OWNER, "audit")
    for perm in PERM_NAMES:
        assert db.can(GUILD, OWNER, perm) is True


def test_owner_isolated_per_guild(db):
    db.set_owner(GUILD, OWNER, "audit")
    assert db.get_owner(GUILD + 1) is None
    assert db.can(GUILD + 1, OWNER, "control") is False


# ---------- grant / revoke ----------


def test_grant_and_can(db):
    db.grant(GUILD, USER, ["control", "playlists"])
    assert db.can(GUILD, USER, "control") is True
    assert db.can(GUILD, USER, "playlists") is True
    assert db.can(GUILD, USER, "permissions") is False


def test_grant_idempotent(db):
    db.grant(GUILD, USER, ["control"])
    db.grant(GUILD, USER, ["control"])
    assert db.grants_for(GUILD, USER) == {"control"}


def test_revoke_one_perm(db):
    db.grant(GUILD, USER, ["control", "playlists"])
    db.revoke(GUILD, USER, ["control"])
    assert db.can(GUILD, USER, "control") is False
    assert db.can(GUILD, USER, "playlists") is True


def test_revoke_all(db):
    db.grant(GUILD, USER, ["control", "playlists", "permissions"])
    db.revoke(GUILD, USER, None)
    assert db.grants_for(GUILD, USER) == set()
    for perm in PERM_NAMES:
        assert db.can(GUILD, USER, perm) is False


def test_revoke_owner_does_nothing(db):
    db.set_owner(GUILD, OWNER, "audit")
    db.grant(GUILD, OWNER, ["control"])
    db.revoke(GUILD, OWNER, None)
    for perm in PERM_NAMES:
        assert db.can(GUILD, OWNER, perm) is True


def test_revoke_unknown_user_noop(db):
    db.revoke(GUILD, 999, None)
    assert db.all_grants(GUILD) == {}


# ---------- all_grants / grants_for ----------


def test_all_grants_groups_by_user(db):
    db.grant(GUILD, USER, ["control", "permissions"])
    db.grant(GUILD, 333, ["playlists"])
    assert db.all_grants(GUILD) == {
        USER: {"control", "permissions"},
        333: {"playlists"},
    }


def test_grants_empty(db):
    assert db.grants_for(GUILD, USER) == set()
    assert db.all_grants(GUILD) == {}


# ---------- константы ----------


def test_deny_text_covers_all_perms():
    for perm in PERM_NAMES:
        text = deny_text(perm)
        assert "нет права" in text
        assert "!perms" in text


def test_perm_labels_cover_all_perms():
    for perm in PERM_NAMES:
        assert perm in PERM_LABELS
        assert PERM_LABELS[perm]
