import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")


_clock_lock = threading.RLock()
_clock_checked_mono = None
_clock_result = None
# local_mid - CLOB_server.  CLOB-aligned time is local - offset.
_clock_offset = 0.0
_clock_measured = False
_clock_sample_wall = None
_clock_sample_mono = None


def clock_offset() -> float:
    with _clock_lock:
        return _clock_offset


def clock_measured() -> bool:
    with _clock_lock:
        return _clock_measured


def wall(ts: float | None = None) -> float:
    """CLOB-aligned Unix time after a successful ``check_clock``.

    Explicit timestamps are returned unchanged so tests and recorded samples
    stay literal.  Live call sites that need round identity should pass
    ``timer.wall()``, not ``time.time()``.
    """
    if ts is not None:
        return float(ts)
    with _clock_lock:
        offset = _clock_offset
    return time.time() - offset


def exchange_age_s(ts_ms: int | float) -> float:
    """Age of an exchange timestamp versus CLOB-aligned wall time."""
    return wall() - float(ts_ms) / 1000.0


def now_et(ts: float | None = None):
    return datetime.fromtimestamp(wall() if ts is None else ts, ET)


def seconds_left(ts: float | None = None):
    """Whole seconds remaining, sampled from the same Unix instant as a round id.

    At an exact boundary this intentionally returns 300, never 0.  Callers can
    pass one wall-clock sample to both this function and ``window_start`` so a
    boundary cannot split their view across two different rounds.
    """
    current = int(wall() if ts is None else ts)
    return 300 - (current % 300)


def window_start(ts: float | None = None, window: int = 300) -> int:
    current = int(wall() if ts is None else ts)
    return current - current % window


def current_round_window_et(ts: float | None = None):
    now = now_et(ts)
    start_min = (now.minute // 5) * 5
    end_min = start_min + 5
    start_h, end_h = now.hour, now.hour
    if end_min >= 60:
        end_min -= 60
        end_h = (end_h + 1) % 24

    def fmt(h, m):
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d}{suffix}"

    return f"{fmt(start_h, start_min)}-{fmt(end_h, end_min)} ET"


def check_clock(host: str, max_drift_s: float = 2.0, *, cache_s: float = 30.0,
                timeout: float = 4.0) -> tuple[bool, str, float | None]:
    """Compare the local wall clock with the CLOB server's public clock.

    A successful measurement stores an offset so ``wall()`` follows CLOB time.
    Network failure is fail-closed for live order entry.  The midpoint of the
    local request interval removes most request latency from the drift estimate.
    """
    global _clock_checked_mono, _clock_result, _clock_offset, _clock_measured
    global _clock_sample_wall, _clock_sample_mono
    now_mono = time.monotonic()
    with _clock_lock:
        jump = 0.0
        if _clock_sample_wall is not None and _clock_sample_mono is not None:
            jump = abs(
                (time.time() - _clock_sample_wall)
                - (now_mono - _clock_sample_mono)
            )
        if (_clock_checked_mono is not None and _clock_result is not None
                and now_mono - _clock_checked_mono <= cache_s
                and jump <= 0.5):
            return _clock_result
    offset = None
    try:
        before = time.time()
        response = requests.get(f"{host.rstrip('/')}/time", timeout=timeout)
        response.raise_for_status()
        after = time.time()
        raw = response.json()
        if isinstance(raw, dict):
            raw = raw.get("timestamp", raw.get("time"))
        server = float(raw)
        if server > 10_000_000_000:
            server /= 1000.0
        drift = ((before + after) / 2.0) - server
        if not -86_400 < drift < 86_400:
            raise ValueError("implausible server time")
        ok = abs(drift) <= float(max_drift_s)
        result = (ok, ("clock synchronized" if ok else
                       f"local clock drift {drift:+.3f}s exceeds {max_drift_s:.3f}s"), drift)
        offset = drift
    except Exception as exc:
        result = (False, f"CLOB clock check failed: {type(exc).__name__}", None)
    sampled_wall = time.time()
    sampled_mono = time.monotonic()
    with _clock_lock:
        _clock_checked_mono = now_mono
        _clock_result = result
        _clock_sample_wall = sampled_wall
        _clock_sample_mono = sampled_mono
        if offset is not None:
            _clock_offset = offset
            _clock_measured = True
    return result


def reset_clock_cache() -> None:
    global _clock_checked_mono, _clock_result, _clock_offset, _clock_measured
    global _clock_sample_wall, _clock_sample_mono
    with _clock_lock:
        _clock_checked_mono = None
        _clock_result = None
        _clock_offset = 0.0
        _clock_measured = False
        _clock_sample_wall = None
        _clock_sample_mono = None
