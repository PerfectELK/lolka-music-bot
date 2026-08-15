"""Тесты чистых хелперов движка: ротация очереди, длительности, пагинация,
экранирование, friendly_error, подсчёт людей в голосовом канале."""

import asyncio
from types import SimpleNamespace

import pytest

from engine import (
    MusicEngine,
    _cycle_entries,
    _lazy_entry,
    _requeue_interrupted,
    _rewind_playlist,
    _rewind_queue,
    friendly_error,
    rotate_tracks,
)
from config import RESUME_GRACE_SEC
from ui_utils import esc, fmt_duration, paginate


def test_rotate_tracks():
    tracks = list(range(1, 5))
    assert rotate_tracks(tracks, 1) == [1, 2, 3, 4]
    assert rotate_tracks(tracks, 2) == [2, 3, 4, 1]
    assert rotate_tracks(tracks, 4) == [4, 1, 2, 3]
    assert rotate_tracks(tracks, 5) == [1, 2, 3, 4]  # вне диапазона — как есть
    assert rotate_tracks([], 3) == []
    assert rotate_tracks(tracks, 0) == [1, 2, 3, 4]
    # оригинал не мутируется
    assert tracks == [1, 2, 3, 4]


def test_fmt_duration():
    assert fmt_duration(0) == "0:00"
    assert fmt_duration(59) == "0:59"
    assert fmt_duration(60) == "1:00"
    assert fmt_duration(65) == "1:05"
    assert fmt_duration(3600) == "1:00:00"
    assert fmt_duration(3661) == "1:01:01"
    assert fmt_duration(None) == ""
    assert fmt_duration("abc") == ""
    assert fmt_duration(-5) == ""


def test_friendly_error_known_patterns():
    assert "18+" in friendly_error("ERROR: Sign in to confirm your age")
    assert "удалено" in friendly_error("Video unavailable")
    assert "удалено" in friendly_error("This video is private")
    assert "403" in friendly_error("HTTP Error 403: Forbidden")
    assert "скачать" in friendly_error("Unable to download video data")
    assert "ограничил" in friendly_error("ERROR: YouTube said: rate limit exceeded")
    assert "ограничил" in friendly_error("Too many requests")
    assert "не поддерживается" in friendly_error("Unsupported URL: https://x.com")
    assert "капчу" in friendly_error("Sign in to confirm you're not a bot")


def test_friendly_error_unknown_passthrough():
    assert friendly_error("что-то совсем незнакомое") == "что-то совсем незнакомое"


def test_paginate():
    items = list(range(1, 26))
    chunk, pages, _, _, start = paginate(items, 1)
    assert pages == 3 and start == 1 and chunk == list(range(1, 11))
    chunk, pages, _, _, start = paginate(items, 2)
    assert chunk == list(range(11, 21)) and start == 11
    chunk, pages, _, _, start = paginate(items, 3)
    assert chunk == list(range(21, 26)) and start == 21
    # вне диапазона
    assert paginate(items, 0) == (None, 3, False, False, 0)
    assert paginate(items, 4) == (None, 3, False, False, 0)
    # пустой список — одна пустая страница
    assert paginate([], 1) == ([], 1, False, False, 1)
    # нестандартный размер страницы
    chunk, pages, _, _, start = paginate(items, 2, page_size=10)
    assert chunk == list(range(11, 21))


def test_rewind_queue_restores_current():
    # A играет, очередь [C,D]: ⏮ → очередь [A,B,C,D] (текущий вернулся)
    hist = [{"title": "A"}, {"title": "B"}]
    queue = [{"title": "C"}, {"title": "D"}]
    h, q = _rewind_queue(hist, queue)
    assert [t["title"] for t in q] == ["A", "B", "C", "D"]
    assert [t["title"] for t in h] == ["A"]


def test_rewind_queue_toggle_keeps_order():
    # ⏭ после ⏮ должен дать тот же трек, а не перескочить (баг: текущий терялся)
    hist = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    queue = [{"title": "D"}]
    h, q = _rewind_queue(hist, queue)
    assert [t["title"] for t in q] == ["B", "C", "D"]
    assert [t["title"] for t in h] == ["A", "B"]


def test_rewind_queue_no_prev():
    assert _rewind_queue([], []) is None
    assert _rewind_queue([{"title": "A"}], []) is None


def test_rewind_queue_does_not_mutate_inputs():
    hist = [{"title": "A"}, {"title": "B"}]
    queue = [{"title": "C"}]
    _rewind_queue(hist, queue)
    assert [t["title"] for t in hist] == ["A", "B"]
    assert [t["title"] for t in queue] == ["C"]


def test_cycle_entries_playlist_returns_copy():
    items = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    nav = {"name": "X", "items": items, "index": 2}
    history = [{"title": "H"}]
    q = _cycle_entries(nav, history)
    assert [t["title"] for t in q] == ["A", "B", "C"]
    assert q == items
    # копия: мутация результата не меняет ни nav.items, ни history
    q.pop()
    assert [t["title"] for t in nav["items"]] == ["A", "B", "C"]
    assert [t["title"] for t in history] == ["H"]


def test_cycle_entries_no_nav_uses_history():
    nav = None
    history = [{"title": "H1"}, {"title": "H2"}]
    q = _cycle_entries(nav, history)
    assert [t["title"] for t in q] == ["H1", "H2"]
    assert q is not history
    # копия: мутация результата не трогает историю
    q.clear()
    assert [t["title"] for t in history] == ["H1", "H2"]


def test_cycle_entries_empty_items_fallback_to_history():
    # после !remove всех оставшихся треков nav.items пуст, но история непуста —
    # фолбэк на историю, иначе бот молча отключится
    nav = {"name": "X", "items": [], "index": 0}
    history = [{"title": "H1"}, {"title": "H2"}]
    q = _cycle_entries(nav, history)
    assert [t["title"] for t in q] == ["H1", "H2"]


def test_cycle_entries_both_empty_returns_none():
    assert _cycle_entries(None, None) is None
    assert _cycle_entries(None, []) is None
    assert _cycle_entries({"name": "X", "items": [], "index": 0}, None) is None
    assert _cycle_entries({"name": "X", "items": [], "index": 0}, []) is None


def test_rewind_playlist_walk_back_and_toggle():
    items = [
        {"title": "A"},
        {"title": "B"},
        {"title": "C"},
        {"title": "D"},
    ]
    # C играет (index=3, очередь [D]): ⏮ → играет B, потом снова C (toggle)
    q, index = _rewind_playlist(items, 3, [items[3]])
    assert [t["title"] for t in q] == ["B", "C", "D"]
    assert index == 1
    # B играет (index=2): ⏮ → играет A, порядок плейлиста полностью сохранён
    q2, index2 = _rewind_playlist(items, 2, [items[2], items[3]])
    assert [t["title"] for t in q2] == ["A", "B", "C", "D"]
    assert index2 == 0
    # первый трек играет — предыдущего нет
    assert _rewind_playlist(items, 1, items[1:]) is None
    assert _rewind_playlist(items, 0, items) is None
    # очередь == items[index:] (инвариант плейлиста)
    assert [t["title"] for t in q] == [t["title"] for t in items[index:]]


def test_requeue_interrupted_basic():
    state = {"title": "T1", "track_started_at": 100.0}
    entry = {"title": "T1", "duration": 300.0}
    hist = [{"title": "A"}, entry]
    queue = [{"title": "B"}]
    nav = {"name": "X", "items": [{"title": "A"}, entry, {"title": "B"}], "index": 2}
    res = _requeue_interrupted(state, hist, queue, nav, now=lambda: 142.5)
    assert res is not None
    requeued, seek = res
    assert requeued is entry  # объект тот же — история и очередь делят entry
    assert seek == 42.5
    assert queue[0] is entry
    assert [t["title"] for t in queue] == ["T1", "B"]
    assert [t["title"] for t in hist] == ["A"]
    assert nav["index"] == 1
    assert entry["resume_seek"] == 42.5
    # инвариант плейлиста: очередь == items[index:]
    assert [t["title"] for t in queue] == [t["title"] for t in nav["items"][nav["index"]:]]


def test_requeue_interrupted_no_track_started():
    # обрыв между треками: track_started_at нет (или уже снят) — ничего не делаем
    state = {"title": "T1"}
    hist = [{"title": "T1"}]
    queue = [{"title": "B"}]
    nav = {"name": "X", "items": [{"title": "T1"}, {"title": "B"}], "index": 1}
    assert _requeue_interrupted(state, hist, queue, nav, now=lambda: 100.0) is None
    assert [t["title"] for t in hist] == ["T1"]
    assert [t["title"] for t in queue] == ["B"]
    assert nav["index"] == 1


def test_requeue_interrupted_empty_history():
    state = {"title": "T1", "track_started_at": 100.0}
    assert _requeue_interrupted(state, [], [], None, now=lambda: 150.0) is None


def test_requeue_interrupted_near_end_no_resume():
    # до конца осталось меньше RESUME_GRACE_SEC — возобновлять нечего
    state = {"title": "T1", "track_started_at": 100.0}
    entry = {"title": "T1", "duration": 50.0}
    hist = [entry]
    queue = []
    assert _requeue_interrupted(state, hist, queue, None, now=lambda: 147.0) is None
    assert hist == [entry]
    assert queue == []
    # ровно на границе тоже не возобновляем (elapsed >= duration - grace)
    assert _requeue_interrupted({"title": "T1", "track_started_at": 100.0},
                                hist, queue, None, now=lambda: 145.0) is None


def test_requeue_interrupted_unknown_duration_resumes():
    # duration неизвестен — резюм всегда (seek в конец безвреден)
    state = {"title": "T1", "track_started_at": 100.0}
    entry = {"title": "T1", "duration": None}
    hist = [entry]
    queue = []
    res = _requeue_interrupted(state, hist, queue, None, now=lambda: 200.0)
    assert res is not None
    assert queue == [entry]
    assert entry["resume_seek"] == 100.0


def test_requeue_interrupted_title_mismatch_noop():
    # страховка: history[-1] не совпадает с играющим треком
    state = {"title": "Другой", "track_started_at": 100.0}
    entry = {"title": "T1"}
    hist = [entry]
    queue = [{"title": "B"}]
    assert _requeue_interrupted(state, hist, queue, None, now=lambda: 150.0) is None
    assert hist == [entry]
    assert queue == [{"title": "B"}]


def test_requeue_interrupted_clears_track_ended_quick():
    # обрыв < TRACK_MIN_RETRY_SEC: ретрай из play_next заменён резюмом —
    # флаг снимается, двойного ре-кью не будет
    state = {"title": "T1", "track_started_at": 100.0, "track_ended_quick": True}
    entry = {"title": "T1", "duration": 300.0}
    hist = [entry]
    queue = []
    assert _requeue_interrupted(state, hist, queue, None, now=lambda: 105.0) is not None
    assert "track_ended_quick" not in state


def test_requeue_interrupted_index_floor():
    # индекс не уходит в минус (первый трек плейлиста): инвариант сохраняется
    state = {"title": "T1", "track_started_at": 100.0}
    entry = {"title": "T1", "duration": 300.0}
    items = [entry]
    hist = [entry]
    queue = []
    nav = {"name": "X", "items": items, "index": 0}
    assert _requeue_interrupted(state, hist, queue, nav, now=lambda: 130.0) is not None
    assert nav["index"] == 0
    assert [t["title"] for t in queue] == [t["title"] for t in items[nav["index"]:]]


def test_requeue_interrupted_stale_elapsed_noop():
    # elapsed <= 0 (часы назад?) — не мутируем ничего
    state = {"title": "T1", "track_started_at": 100.0}
    hist = [{"title": "T1"}]
    queue = [{"title": "B"}]
    assert _requeue_interrupted(state, hist, queue, None, now=lambda: 50.0) is None
    assert [t["title"] for t in hist] == ["T1"]
    assert [t["title"] for t in queue] == ["B"]


def test_esc():
    assert esc("Обычный title") == "Обычный title"
    assert "@everyone" not in esc("@everyone")
    assert "@here" not in esc("@here")
    assert "<@123456789012345678>" not in esc("<@123456789012345678>")
    assert "**" not in esc("**bold**")
    # ссылочный синтаксис ломается (escape_markdown с ignore_links=False)
    assert "\\[" in esc("[link](https://evil.example)")


class _FakeMember:
    def __init__(self, uid, bot=False):
        self.id = uid
        self.bot = bot


class _FakeGuild:
    def __init__(self, members):
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeChannel:
    def __init__(self, guild, voice_states):
        self.guild = guild
        self.voice_states = voice_states


def _make_engine():
    engine = MusicEngine.__new__(MusicEngine)
    engine.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    return engine


def test_channel_humans_counts_only_humans():
    engine = _make_engine()
    guild = _FakeGuild(
        [
            _FakeMember(1),                  # человек, в кеше
            _FakeMember(2, bot=True),        # бот, в кеше
            _FakeMember(999, bot=True),      # сам бот
        ]
    )
    # в канале: человек 1, бот 2, сам бот, неизвестный 3 (не в кеше — человек)
    channel = _FakeChannel(guild, {1: None, 2: None, 999: None, 3: None})
    assert engine._channel_humans(channel) == 2


def test_channel_humans_empty():
    engine = _make_engine()
    guild = _FakeGuild([_FakeMember(999, bot=True)])
    channel = _FakeChannel(guild, {999: None})
    assert engine._channel_humans(channel) == 0


def test_channel_humans_bot_without_cache_entry_is_human():
    # бот в канале, но его нет в кеше участников — считаем человеком
    # (безопасное направление: не выходим из канала, где кто-то есть)
    engine = _make_engine()
    guild = _FakeGuild([])
    channel = _FakeChannel(guild, {123: None})
    assert engine._channel_humans(channel) == 1


def _engine_with_nav(guild_id=1, name="X", items=None, index=0):
    engine = MusicEngine(SimpleNamespace(), SimpleNamespace(), None)
    # префетч не должен уходить в реальную сеть (yt-dlp)
    engine._schedule_prefetch = lambda g: None
    if items is not None:
        engine.pl_nav[guild_id] = {"name": name, "items": items, "index": index}
    return engine


def test_on_playlist_track_added_no_nav():
    engine = _engine_with_nav(items=None)
    assert engine.on_playlist_track_added(1, "X", "https://youtube.com/watch?v=abc", "T") is False
    assert engine.queues == {}


def test_on_playlist_track_added_other_playlist():
    engine = _engine_with_nav(items=[])
    assert engine.on_playlist_track_added(1, "Y", "https://youtube.com/watch?v=abc", "T") is False
    assert engine.queues == {}


def test_on_playlist_track_added_extends_queue_and_items():
    engine = _engine_with_nav(items=[])
    ok = engine.on_playlist_track_added(
        1, "X", "https://youtube.com/watch?v=abc123", "Title", 42
    )
    assert ok is True
    entry = engine.queues[1][-1]
    nav = engine.pl_nav[1]
    assert nav["index"] == 0
    assert nav["items"][-1] is entry
    assert entry["page_url"] == "https://youtube.com/watch?v=abc123"
    assert entry["url"] is None
    assert entry["resolved"] is False
    assert entry["duration"] == 42
    assert entry["title"] == "Title"


def test_on_playlist_track_added_local_file(tmp_path, monkeypatch):
    monkeypatch.setattr("upload_util.UPLOADS_DIR", tmp_path)
    f = tmp_path / "track.mp3"
    f.write_bytes(b"x")
    engine = _engine_with_nav(items=[])
    ok = engine.on_playlist_track_added(1, "X", "local:track.mp3", "Local")
    assert ok is True
    entry = engine.queues[1][-1]
    assert entry["resolved"] is True
    assert entry["page_url"] is None
    assert entry["url"] == str(f)
    assert entry["title"] == "Local"


def test_on_playlist_track_added_local_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("upload_util.UPLOADS_DIR", tmp_path)
    engine = _engine_with_nav(items=[])
    assert engine.on_playlist_track_added(1, "X", "local:nope.mp3", "T") is False
    assert engine.queues == {}


def test_on_playlist_track_added_keeps_invariant():
    guild_id = 1
    items = [_lazy_entry(f"https://youtube.com/watch?v={i}0000000000", f"T{i}") for i in range(5)]
    engine = _engine_with_nav(items=items, index=2)
    engine.queues[guild_id] = list(items[2:])
    ok = engine.on_playlist_track_added(
        guild_id, "X", "https://youtube.com/watch?v=zzzzzzzzzzz", "New"
    )
    assert ok is True
    nav = engine.pl_nav[guild_id]
    assert nav["index"] == 2
    assert engine.queues[guild_id] == nav["items"][nav["index"]:]
    assert len(engine.queues[guild_id]) == 4
    assert len(nav["items"]) == 6


def test_on_playlist_track_added_guild_isolation():
    engine = _engine_with_nav(items=[])
    assert engine.on_playlist_track_added(
        1, "X", "https://youtube.com/watch?v=aaa00000000", "T1"
    ) is True
    assert engine.queues.get(2) is None
    assert engine.pl_nav.get(2) is None
    assert engine.on_playlist_track_added(
        2, "X", "https://youtube.com/watch?v=bbb00000000", "T2"
    ) is False
    assert engine.queues.get(2) is None
    assert [t["title"] for t in engine.queues[1]] == ["T1"]


def test_queues_history_loop_independent():
    engine = _engine_with_nav(items=None)
    engine.queues[1] = [_lazy_entry("https://youtube.com/watch?v=aaa00000000", "A1")]
    engine.queues[2] = [_lazy_entry("https://youtube.com/watch?v=bbb00000000", "B1")]
    engine.history[1] = [{"title": "H1"}]
    engine.history[2] = [{"title": "H2"}]
    engine.loop_on[1] = False
    engine.loop_on[2] = True
    engine.pl_nav[1] = {"name": "X", "items": list(engine.queues[1]), "index": 0}
    engine.play_state[1] = {"channel": None, "now_message": None}
    engine.clear_guild(1)
    assert engine.queues == {2: engine.queues[2]}
    assert engine.history == {2: [{"title": "H2"}]}
    assert engine.pl_nav == {}
    assert engine.play_state == {}
    assert engine.loop_on[2] is True
    assert engine.queues[2][0]["title"] == "B1"


def test_prefetch_per_guild(monkeypatch):
    engine = _engine_with_nav(items=None)
    engine._schedule_prefetch = MusicEngine._schedule_prefetch.__get__(engine)
    engine.queues[1] = [_lazy_entry("https://youtube.com/watch?v=aaa00000000", "A1")]
    engine.queues[2] = [_lazy_entry("https://youtube.com/watch?v=bbb00000000", "B1")]

    class _FakeTask:
        def __init__(self, coro=None):
            self._done = False
            self._coro = coro

        def done(self):
            return self._done

        def add_done_callback(self, cb):
            self._cb = cb

    active1 = _FakeTask()
    engine._prefetch[1] = active1

    created = []

    def fake_create_task(coro):
        t = _FakeTask(coro)
        created.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    # активный префетч глиды 1 не блокирует планирование глиды 2
    engine._schedule_prefetch(1)
    engine._schedule_prefetch(2)

    assert engine._prefetch[1] is active1
    assert len(created) == 1
    assert engine._prefetch[2] is created[0]
    assert engine._prefetch[2].done() is False
    created[0]._coro.close()
