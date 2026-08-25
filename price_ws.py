import asyncio
import json
import math
import threading
import time

import websockets

import timer

latest_price = None
latest_price_mono = None
latest_price_ts_ms = None
latest_trade_id = None
_first_logged = False
_lock = threading.RLock()


def publish_price(price, observed_mono=None, exchange_ts_ms=None, trade_id=None):
    """Atomically publish one valid Binance trade print."""
    global latest_price, latest_price_mono, latest_price_ts_ms, latest_trade_id
    p = float(price)
    ts = int(exchange_ts_ms) if exchange_ts_ms is not None else None
    sequence = int(trade_id) if trade_id is not None else None
    observed = time.monotonic() if observed_mono is None else float(observed_mono)
    if not math.isfinite(p) or p <= 0:
        raise ValueError("invalid BTC price")
    if not math.isfinite(observed):
        raise ValueError("invalid observation timestamp")
    if ts is not None and ts <= 0:
        raise ValueError("invalid exchange timestamp")
    if sequence is not None and sequence < 0:
        raise ValueError("invalid trade sequence id")
    with _lock:
        if (sequence is not None and latest_trade_id is not None
                and sequence <= latest_trade_id):
            return False
        if (ts is not None and latest_price_ts_ms is not None
                and ts < latest_price_ts_ms):
            return False
        latest_price = p
        latest_price_mono = observed
        latest_price_ts_ms = ts
        if sequence is not None:
            latest_trade_id = sequence
    return True


def latest_snapshot():
    with _lock:
        return latest_price, latest_price_mono, latest_price_ts_ms


def fresh_snapshot(max_age_s=3.0, *, future_tolerance_s=2.0):
    """Return ``(price, exchange_timestamp_ms)`` only for a fresh real print."""
    try:
        max_age = float(max_age_s)
        future_tolerance = float(future_tolerance_s)
    except (TypeError, ValueError):
        return None, None
    if (not math.isfinite(max_age) or max_age <= 0
            or not math.isfinite(future_tolerance) or future_tolerance < 0):
        return None, None
    price, mono, ts_ms = latest_snapshot()
    if price is None or mono is None or ts_ms is None:
        return None, None
    local_age = time.monotonic() - mono
    exchange_age = timer.exchange_age_s(ts_ms)
    # Local receipt age is staleness. Exchange age only rejects impossible
    # stamps: CLOB wall and Binance T can differ by a few seconds without the
    # print being old on the wire.
    exchange_limit = max(max_age + 15.0, 30.0)
    if (not math.isfinite(local_age) or not math.isfinite(exchange_age)
            or local_age < 0 or local_age > max_age
            or exchange_age > exchange_limit
            or exchange_age < -future_tolerance):
        return None, None
    return price, ts_ms


def clear_if_observation(expected_mono) -> bool:
    """Atomically blank a stale display value without erasing a newer print."""
    global latest_price, latest_price_mono, latest_price_ts_ms
    with _lock:
        if latest_price_mono != expected_mono:
            return False
        latest_price = None
        latest_price_mono = None
        latest_price_ts_ms = None
        return True


async def stream_price():
    global _first_logged
    urls = ("wss://stream.binance.com:9443/ws/btcusdt@trade",
            "wss://data-stream.binance.vision/ws/btcusdt@trade")
    url_i = 0
    dry_attempts = 0
    delay = 0.25
    while True:
        started = time.monotonic()
        got_valid = False
        url = urls[url_i]
        try:
            async with websockets.connect(
                    url, ping_interval=15, ping_timeout=10,
                    open_timeout=10, close_timeout=2) as ws:
                print("[PRICE] Binance WebSocket connected. Streaming BTC/USDT...")
                _first_logged = False
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    payload = json.loads(msg)
                    accepted = publish_price(
                        payload["p"], exchange_ts_ms=payload.get("T") or payload.get("E"),
                        trade_id=payload.get("t"))
                    if not accepted:
                        continue
                    got_valid = True
                    price, _, _ = latest_snapshot()
                    if not _first_logged:
                        print(f"[PRICE] First price received: ${price:,.2f}")
                        _first_logged = True
        except Exception as exc:
            healthy = got_valid and time.monotonic() - started >= 30.0
            if healthy:
                delay = 0.25
            if got_valid:
                dry_attempts = 0
            else:
                dry_attempts += 1
                if dry_attempts >= 2:
                    url_i = (url_i + 1) % len(urls)
                    dry_attempts = 0
            print(f"[PRICE] WebSocket error: {type(exc).__name__}: {str(exc)[:120]}. "
                  f"Reconnecting in {delay:.2f}s...")
            await asyncio.sleep(delay)
            delay = min(8.0, delay * 2)
