"""Тесты панели прав: сериализация Select (value обязан быть строкой),
пустые списки не рендерят Select."""

from types import SimpleNamespace

from perms_panel import PermissionsPanelView


def _member(uid, name, bot=False):
    return SimpleNamespace(id=uid, display_name=name, bot=bot)


def _engine(guild, members, grants=None):
    perms = SimpleNamespace(
        get_owner=lambda gid: 999,
        all_grants=lambda gid: grants or {},
        grants_for=lambda gid, uid: (grants or {}).get(uid, set()),
    )
    return SimpleNamespace(
        bot=SimpleNamespace(get_guild=lambda gid: guild),
        perms=perms,
        has_perm=lambda gid, uid, p: True,
    )


def _view(guild, members, grants=None):
    engine = _engine(guild, members, grants)
    return PermissionsPanelView(engine, 1)


def _selects(view):
    return [i for i in view.children if type(i).__name__ == "_PermsSelect"]


def _option_values(view):
    out = []
    for sel in _selects(view):
        for opt in sel.options:
            out.append(opt.value)
    return out


def test_grant_member_select_values_are_strings():
    guild = SimpleNamespace(
        get_member=lambda uid: None,
        members=[_member(111, "Alice"), _member(222, "Bob")],
    )
    view = _view(guild, guild.members)
    view.mode = "grant_member"
    view.refresh()
    assert _option_values(view) == ["111", "222"]


def test_revoke_user_select_values_are_strings():
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])
    view = _view(guild, guild.members, grants={111: {"control"}, 222: {"playlists"}})
    view.mode = "revoke_user"
    view.refresh()
    assert sorted(_option_values(view)) == ["111", "222"]


def test_grant_perm_select_values_are_perm_names():
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])
    view = _view(guild, guild.members)
    view.mode = "grant_perm"
    view.pending_user = 111
    view.refresh()
    assert set(_option_values(view)) == {"control", "playlists", "permissions"}


def test_empty_members_renders_placeholder_not_empty_select():
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])
    view = _view(guild, guild.members)
    view.mode = "grant_member"
    view.refresh()
    assert _selects(view) == []
    assert any(getattr(i, "disabled", False) for i in view.children)


def test_empty_grants_renders_placeholder_not_empty_select():
    guild = SimpleNamespace(get_member=lambda uid: None, members=[])
    view = _view(guild, guild.members, grants={})
    view.mode = "revoke_user"
    view.refresh()
    assert _selects(view) == []
    assert any(getattr(i, "disabled", False) for i in view.children)


def test_empty_label_falls_back_to_id():
    guild = SimpleNamespace(get_member=lambda uid: None, members=[_member(111, "")])
    view = _view(guild, guild.members)
    view.mode = "grant_member"
    view.refresh()
    labels = [opt.label for sel in _selects(view) for opt in sel.options]
    assert labels == ["111"]
