"""Тесты нормализации громкости: gain_db, парсинг ebur128, gain_for_path."""

import json
import os
import time

import loudness
from loudness import gain_db, gain_for_path, measure_loudness


class FakeProc:
    def __init__(self, stderr, stdout=b"", returncode=0):
        self.stderr = stderr
        self.stdout = stdout
        self.returncode = returncode


def test_gain_db_quiet_track():
    # тихий трек: +6 дБ компенсации, но TP-защита снижает до +5
    # (TP после усиления должен остаться <= -1 dBTP: -6 + 5 = -1)
    assert gain_db(-20.0, -6.0) == 5.0


def test_gain_db_loud_track():
    assert gain_db(-8.0, -1.5) == -6.0


def test_gain_db_max_gain_cap():
    # I=-30 требует +16 дБ, но cap по NORMALIZE_MAX_GAIN_DB = +15
    assert gain_db(-30.0, -20.0) == 15.0
    # и вниз тоже: громкий трек не зажимается сильнее -15 дБ
    assert gain_db(5.0, -6.0) == -15.0


def test_gain_db_tp_protection():
    # TP=-0.2 близок к лимиту: даже +6 дБ компенсации нельзя — gain ограничен
    # NORMALIZE_TP_LIMIT_DB - tp = -0.8
    assert gain_db(-20.0, -0.2) == -0.8


def test_gain_db_unmeasurable():
    assert gain_db(float("-inf"), -6.0) is None
    assert gain_db(-50.0, -6.0) is None
    assert gain_db(float("nan"), -6.0) is None


def test_gain_db_zero_gain():
    # ровно на целевой громкости — нормализация не нужна
    assert gain_db(-14.0, -10.0) is None


def test_measure_loudness_parses_ebur128(monkeypatch):
    def fake_run(cmd, capture_output=True, timeout=60):
        return FakeProc(
            b"frame= 123 fps=0.0 q=-0.0 Lsize=N/A time=00:00:05.00 bitrate=N/A\n"
            b"[Parsed_ebur128_0 @ 0x...] I: -12.34 LUFS\n"
            b"[Parsed_ebur128_0 @ 0x...] True peak: -0.7 dBTP\n"
        )

    monkeypatch.setattr(loudness.subprocess, "run", fake_run)
    assert measure_loudness("ffmpeg", "x.opus") == {"i": -12.34, "tp": -0.7}


def test_measure_loudness_parses_summary_block(monkeypatch):
    # реальный формат сводки ffmpeg: значения в сводке, покадровые строки
    # содержат мгновенную громкость и НЕ должны попасть в результат
    out = (
        b"[Parsed_ebur128_0 @ 0x...] t: 0.0999773 TARGET:-23 LUFS M:-120.7 S:-120.7 I: -70.0 LUFS\n"
        b"[Parsed_ebur128_0 @ 0x...] t: 0.2999773 TARGET:-23 LUFS M:-36.2 S:-120.7 I: -36.2 LUFS\n"
        b"Summary:\n"
        b"\n"
        b"  Integrated loudness:\n"
        b"    I:         -36.2 LUFS\n"
        b"    Threshold: -46.2 LUFS\n"
        b"\n"
        b"  True peak:\n"
        b"    Peak:      -32.4 dBFS\n"
    )

    def fake_run(cmd, capture_output=True, timeout=60):
        return FakeProc(out)

    monkeypatch.setattr(loudness.subprocess, "run", fake_run)
    assert measure_loudness("ffmpeg", "x.opus") == {"i": -36.2, "tp": -32.4}


def test_measure_loudness_parses_summary_p_dbpt(monkeypatch):
    # формат ffmpeg <= 6: "P: -0.7 dBTP" в сводке
    out = (
        b"Summary:\n"
        b"  Integrated loudness:\n"
        b"    I:         -20.0 LUFS\n"
        b"  True peak:\n"
        b"    P:         -0.7 dBTP\n"
    )

    def fake_run(cmd, capture_output=True, timeout=60):
        return FakeProc(out)

    monkeypatch.setattr(loudness.subprocess, "run", fake_run)
    assert measure_loudness("ffmpeg", "x.opus") == {"i": -20.0, "tp": -0.7}


def test_measure_loudness_garbage_output(monkeypatch):
    monkeypatch.setattr(
        loudness.subprocess, "run", lambda *a, **k: FakeProc(b"nothing useful")
    )
    assert measure_loudness("ffmpeg", "x.opus") is None


def test_measure_loudness_no_ffmpeg():
    assert measure_loudness(None, "x.opus") is None


def test_measure_loudness_launch_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(loudness.subprocess, "run", boom)
    assert measure_loudness("ffmpeg", "x.opus") is None


def test_gain_for_path_sidecar_hit(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    p = cache_dir / "dQw4w9WgXcQ.opus"
    p.write_bytes(b"x")
    (cache_dir / "dQw4w9WgXcQ.json").write_text(
        json.dumps({"title": "t", "loudness": {"i": -20.0, "tp": -6.0}}),
        encoding="utf-8",
    )

    def not_called(*a, **k):
        raise AssertionError("ffmpeg не должен вызываться при попадании в sidecar")

    monkeypatch.setattr(loudness, "measure_loudness", not_called)
    assert gain_for_path("ffmpeg", p, cache_dir) == 5.0


def test_gain_for_path_sidecar_without_loudness_measures(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    p = cache_dir / "dQw4w9WgXcQ.opus"
    p.write_bytes(b"x")
    (cache_dir / "dQw4w9WgXcQ.json").write_text(
        json.dumps({"title": "Старый трек", "page_url": "https://youtube.com/watch?v=dQw4w9WgXcQ"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        loudness, "measure_loudness", lambda exe, path: {"i": -20.0, "tp": -6.0}
    )
    assert gain_for_path("ffmpeg", p, cache_dir) == 5.0
    # измерение дописано в sidecar, остальные поля сохранены
    meta = json.loads((cache_dir / "dQw4w9WgXcQ.json").read_text(encoding="utf-8"))
    assert meta["loudness"] == {"i": -20.0, "tp": -6.0}
    assert meta["title"] == "Старый трек"


def test_gain_for_path_upload_memoized(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    p = uploads / "u1.mp3"
    p.write_bytes(b"x")
    calls = []

    def fake_measure(exe, path):
        calls.append(path)
        return {"i": -20.0, "tp": -6.0}

    monkeypatch.setattr(loudness, "measure_loudness", fake_measure)
    assert gain_for_path("ffmpeg", p, uploads) == 5.0
    assert gain_for_path("ffmpeg", p, uploads) == 5.0
    assert len(calls) == 1  # мемо: повторного измерения нет


def test_gain_for_path_upload_remeasure_on_mtime_change(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    p = uploads / "u1.mp3"
    p.write_bytes(b"x")
    calls = []

    def fake_measure(exe, path):
        calls.append(path)
        return {"i": -20.0, "tp": -6.0}

    monkeypatch.setattr(loudness, "measure_loudness", fake_measure)
    assert gain_for_path("ffmpeg", p, uploads) == 5.0
    # файл заменён (например, перезалили) — mtime изменился, мерим заново
    p.write_bytes(b"yy")
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert gain_for_path("ffmpeg", p, uploads) == 5.0
    assert len(calls) == 2


def test_gain_for_path_disabled(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    p = cache_dir / "v1.opus"
    p.write_bytes(b"x")
    (cache_dir / "v1.json").write_text(
        json.dumps({"loudness": {"i": -20.0, "tp": -6.0}}), encoding="utf-8"
    )
    monkeypatch.setattr(loudness, "NORMALIZE_ENABLED", False)
    assert gain_for_path("ffmpeg", p, cache_dir) is None


def test_gain_for_path_measure_failure(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    p = uploads / "u1.mp3"
    p.write_bytes(b"x")
    monkeypatch.setattr(loudness, "measure_loudness", lambda exe, path: None)
    assert gain_for_path("ffmpeg", p, uploads) is None
