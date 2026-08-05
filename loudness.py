"""Измерение громкости (EBU R128) и расчёт компенсирующего gain.

ReplayGain-подход: трек измеряется один раз фильтром ebur128 (интегрированная
громкость I и true peak TP), при воспроизведении громкость компенсируется
фильтром -af volume=<gain>dB. В кеше хранятся сырые измерения ({"i", "tp"}),
а не готовый gain — смена целевой громкости не требует повторного измерения.

Модуль не импортирует engine (как now_playing.py) — его используют и
track_cache, и engine, без круговых зависимостей.
"""

import json
import logging
import math
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

from config import (
    NORMALIZE_ENABLED,
    NORMALIZE_MAX_GAIN_DB,
    NORMALIZE_TARGET_LUFS,
    NORMALIZE_TP_LIMIT_DB,
)

_log = logging.getLogger("music_bot")

_RE_I = re.compile(r"I:\s+([-\d.]+)\s+LUFS")
# True peak в сводке ebur128: "P: -0.7 dBTP" (ffmpeg <= 6) или
# "Peak: -32.4 dBFS" (ffmpeg 9+), значение на той же строке или следующей.
_RE_TP = re.compile(r"True peak:\s*(?:P|Peak)?\s*:?\s*([-\d.]+)\s*dB(?:TP|FS)")

# Сессионный кеш ленивых измерений (uploads и старый кеш без sidecar-измерения):
# ключ (str(path), mtime), значение {"i", "tp"}. Блокировка защищает только
# чтение/запись dict — измерение (ffmpeg ~1-3 с) выполняется вне блокировки,
# поэтому одновременный старт треков в разных глидах не сериализуется.
# Дублирующее измерение одного файла при гонке допустимо: результат
# идемпотентен, оба потока пишут одно значение в мемо и sidecar.
_memo: dict[tuple[str, float], dict] = {}
_memo_lock = threading.Lock()


def measure_loudness(ffmpeg_exe: Optional[str], path) -> Optional[dict]:
    """Измерить громкость трека фильтром ebur128: {"i": LUFS, "tp": dBTP}.

    None — ffmpeg недоступен/не запустился, таймаут, либо ebur128 не выдал
    данных (трек короче ~3 с или цифровая тишина → строки I в выводе нет,
    а короткие отрывки дают "-inf", который под регулярки не попадает).
    """
    if not ffmpeg_exe:
        return None
    cmd = [
        ffmpeg_exe,
        "-nostats",
        "-i", str(path),
        "-af", "ebur128=peak=true",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("не удалось измерить громкость %s: %s", path, exc)
        return None
    text = proc.stderr.decode("utf-8", errors="replace")
    text += proc.stdout.decode("utf-8", errors="replace")
    # Покадровые строки ebur128 тоже содержат "I: <мгновенная громкость>" —
    # берём только сводку, иначе первой попадётся громкость первого кадра.
    idx = text.rfind("Summary:")
    if idx != -1:
        text = text[idx:]
    m_i = _RE_I.search(text)
    m_tp = _RE_TP.search(text)
    if not m_i or not m_tp:
        _log.info("громкость %s не измерена (ebur128 не выдал данные)", path)
        return None
    try:
        return {"i": float(m_i.group(1)), "tp": float(m_tp.group(1))}
    except ValueError:
        return None


def gain_db(i: float, tp: float) -> Optional[float]:
    """Компенсирующий gain в дБ из сырых измерений I (LUFS) и TP (dBTP).

    None — трек не нормализуется: громкость неизмерима (не число или
    I < -40 LUFS — почти тишина, усиление только раскачает шум) либо
    компенсация не нужна (gain = 0).
    """
    if not math.isfinite(i) or i < -40.0:
        return None
    g = NORMALIZE_TARGET_LUFS - i
    g = max(g, -NORMALIZE_MAX_GAIN_DB)
    g = min(g, NORMALIZE_MAX_GAIN_DB)
    if math.isfinite(tp):
        # защита от клиппинга: после усиления TP не должен превысить лимит
        g = min(g, NORMALIZE_TP_LIMIT_DB - tp)
    g = round(g, 2)
    if g == 0.0:
        return None
    return g


def gain_for_path(ffmpeg_exe: Optional[str], path, cache_dir) -> Optional[float]:
    """Gain для файла — единая точка входа из engine.

    Кеш-файл (родитель == cache_dir, суффикс .opus): измерение читается из
    sidecar (постоянное хранилище), при отсутствии — измеряется лениво и
    дописывается в sidecar с сохранением остальных полей. Локальные файлы
    (uploads) — ленивое измерение с сессионным мемо по (путь, mtime): файл
    заменили — перемерится по новому mtime. NORMALIZE_ENABLED=False или
    падение измерения → None, трек играет без нормализации.
    """
    if not NORMALIZE_ENABLED:
        return None
    path = Path(path)
    if path.parent == Path(cache_dir) and path.suffix == ".opus":
        meas = _sidecar_measurement(path)
        if meas is None:
            meas = _measure_lazy(ffmpeg_exe, path)
            if meas is not None:
                _write_sidecar(path, meas)
    else:
        meas = _measure_lazy(ffmpeg_exe, path)
    if meas is None:
        return None
    return gain_db(meas["i"], meas["tp"])


def _measure_lazy(ffmpeg_exe: Optional[str], path: Path) -> Optional[dict]:
    """Измерить с мемо: один вызов измерения на (путь, mtime) на сессию.

    Double-checked locking: под блокировкой — только чтение/запись мемо,
    само измерение — вне блокировки. Гонки двух проигрываний одного трека
    теперь может дать два измерения (редко), но между глидами блокировки нет.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (str(path), mtime)
    with _memo_lock:
        meas = _memo.get(key)
        if meas is not None:
            return meas
    meas = measure_loudness(ffmpeg_exe, path)
    if meas is not None:
        with _memo_lock:
            _memo[key] = meas
    return meas


def _sidecar_measurement(path: Path) -> Optional[dict]:
    try:
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except Exception:
        return None
    meas = meta.get("loudness")
    if isinstance(meas, dict) and "i" in meas and "tp" in meas:
        return meas
    return None


def _write_sidecar(path: Path, meas: dict) -> None:
    """Дописать измерение в sidecar (старый кеш), сохранив остальные поля."""
    meta_path = path.with_suffix(".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    meta["loudness"] = meas
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _log.warning("не сохранил измерение громкости %s: %s", path, exc)
