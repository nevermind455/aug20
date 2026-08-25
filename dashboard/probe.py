"""Read-only instrumentation.

Every wrapper here calls the original function with the original arguments
and returns the original return value, unmodified. Telemetry is recorded in
a try/except so that a bug in the dashboard can never propagate into a
trading call. Nothing in this file changes a decision, a price, a size, an
order type, or the control flow of run_bot().

Attribute-binding note (this is why some patches target main_bot and some
target the source module):

    main_bot does `import orderbook`      -> patch orderbook.get_orderbook
    main_bot does `from polymarket_trade import place_trade`
                                          -> the name is already bound in
                                             main_bot, so patch main_bot.place_trade
"""
from __future__ import annotations

import io
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path

from .safety import exception_summary, terminal_text
from .state import TerminalState

_installed = False
_orig_stdout = None
_originals: dict[tuple[object, str], object] = {}
_sink = None
_lifecycle_lock = threading.RLock()


def _telemetry_failed(state: TerminalState, surface: str, exc: Exception) -> None:
    """Record a probe failure without letting dashboard code reach trading."""
    detail = f"{surface}: {exception_summary(exc)}"
    with state.lock():
        state.telemetry_error = detail
        state.event("DASH", detail, "warn")


# --------------------------------------------------------------- stdout ---
class EventSink(io.TextIOBase):
    """Swallows the bot's print() output and turns it into feed rows.

    Without this the bot's prints land in the middle of the frame and every
    later cursor write is off by one row — the failure mode looks like the
    dashboard corrupting itself, and it shows up exactly during an incident
    when the bot is printing most.
    """

    TAG = re.compile(r"\[(?P<tag>[A-Z]+)\]\s*(?P<msg>.*)$")
    LEVELS = {
        "error": "bad", "fail": "bad", "not placed": "bad", "warn": "warn",
        "could not": "warn", "reconnect": "warn", "skipping": "warn",
        "placed": "good", "connected": "good", "cancelled": "info",
    }
    MAX_LINE = 8192

    def __init__(self, state: TerminalState, mirror=None) -> None:
        super().__init__()
        self.state = state
        self.mirror = mirror          # optional tee to a file
        self._buf = ""
        self._truncated = False
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        if not isinstance(s, str):
            raise TypeError(f"write() argument must be str, not {type(s).__name__}")
        with self._lock:
            pieces = s.split("\n")
            for index, piece in enumerate(pieces):
                room = max(0, self.MAX_LINE - len(self._buf))
                self._buf += piece[:room]
                if len(piece) > room:
                    self._truncated = True
                if index < len(pieces) - 1:
                    suffix = " ...<truncated>" if self._truncated else ""
                    self._emit(self._buf + suffix)
                    self._buf = ""
                    self._truncated = False
        return len(s)

    def flush(self) -> None:
        if self.mirror:
            try:
                self.mirror.flush()
            except Exception as exc:
                _telemetry_failed(self.state, "log flush", exc)

    def finish(self) -> None:
        """Emit a final partial line and close the optional private mirror."""
        with self._lock:
            if self._buf:
                suffix = " ...<truncated>" if self._truncated else ""
                self._emit(self._buf + suffix)
                self._buf = ""
                self._truncated = False
            mirror, self.mirror = self.mirror, None
        if mirror is not None:
            try:
                mirror.flush()
                mirror.close()
            except Exception as exc:
                _telemetry_failed(self.state, "log close", exc)

    def _emit(self, line: str) -> None:
        raw = terminal_text(line.rstrip(), self.MAX_LINE)
        if not raw:
            return
        if self.mirror:
            try:
                self.mirror.write(raw + "\n")
            except Exception as exc:
                _telemetry_failed(self.state, "log mirror", exc)
        # strip the bot's own "[Aug 08 12:00:00 ET]" prefix, the panel has a clock
        body = re.sub(r"^\[[A-Za-z]{3} \d{2} \d{2}:\d{2}:\d{2} ET\]\s*", "", raw)
        m = self.TAG.match(body)
        tag, msg = (m.group("tag"), m.group("msg")) if m else ("LOG", body)
        low = msg.lower()
        level = "info"
        for needle, lv in self.LEVELS.items():
            if needle in low:
                level = lv
                break
        self.state.event(tag, msg, level)
        _parse_round_state(self.state, tag, msg)


_RE_START = re.compile(r"start_price=\$([0-9,]+\.?[0-9]*)")
_RE_CL_START = re.compile(
    r"Chainlink(?: 60s TWAP)? start_price=\$([0-9,]+\.?[0-9]*)")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_round_state(state: TerminalState, tag: str, msg: str) -> None:
    """Keep log-derived opening observations as a telemetry fallback."""
    try:
        if tag == "ROUND":
            m = _RE_CL_START.search(msg)
            if m:
                import timer

                price = _num(m.group(1))
                bot_round = timer.window_start(timer.wall())
                state.mark_strategy_round(bot_round)
                accepted = state.push_price_to_beat(
                    price, source="ROUND log line", round_key=bot_round)
                if accepted:
                    state.flash("NEW ROUND", f"PRICE TO BEAT ${price:,.2f}",
                                "info", ttl=1.8)
                return
            m = _RE_START.search(msg)
            if m:
                with state.lock():
                    state.start_price.set(_num(m.group(1)), source="ROUND log line")
    except Exception as exc:
        _telemetry_failed(state, "round parser", exc)


# -------------------------------------------------------------- wrappers ---
def _patch(obj, name: str, factory) -> None:
    key = (obj, name)
    if key in _originals:
        raise RuntimeError(f"probe target already patched: {getattr(obj, '__name__', obj)!s}.{name}")
    try:
        original = getattr(obj, name)
        wrapped = factory(original)
        _originals[key] = original
        setattr(obj, name, wrapped)
    except Exception:
        raise


def _restore_patches() -> list[str]:
    failures: list[str] = []
    for (obj, name), original in reversed(list(_originals.items())):
        try:
            setattr(obj, name, original)
        except Exception as exc:
            failures.append(
                f"probe restore {getattr(obj, '__name__', type(obj).__name__)}.{name}: "
                f"{exception_summary(exc)}"
            )
            continue
    _originals.clear()
    return failures


def _open_private_mirror(path_value: str):
    """Open an append-only, non-inheritable, regular log file.

    O_NOFOLLOW blocks a pre-planted symlink where the platform supports it;
    mode 0600 prevents other local users from reading live-wallet diagnostics.
    """
    path = Path(path_value).expanduser()
    if path.exists() and path.is_symlink():
        raise OSError("refusing symlink log mirror")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("log mirror must be a regular file")
        os.set_inheritable(fd, False)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    except Exception:
        os.close(fd)
        raise


def install(state: TerminalState, mirror_path: str | None = None):
    """Atomically install probes; roll back every side effect on failure."""
    global _installed, _orig_stdout, _sink
    with _lifecycle_lock:
        try:
            return _install_locked(state, mirror_path)
        except Exception as exc:
            failures = _restore_patches()
            if _sink is not None:
                if sys.stdout is _sink and _orig_stdout is not None:
                    sys.stdout = _orig_stdout
                _sink.finish()
            _sink = None
            _orig_stdout = None
            _installed = False
            state.event("DASH", f"probe install rolled back: {exception_summary(exc)}", "bad")
            for failure in failures:
                state.event("DASH", failure, "bad")
            raise


def _install_locked(state: TerminalState, mirror_path: str | None = None):
    """Install probes. Returns the saved real stdout for the renderer."""
    global _installed, _orig_stdout, _sink
    if _installed:
        if _sink is not None and _sink.state is not state:
            raise RuntimeError("dashboard probes are already attached to another state")
        return _orig_stdout

    import config
    import main_bot
    import market_discovery
    import orderbook
    import polymarket_trade
    import strategy

    _orig_stdout = sys.stdout

    with state.lock():
        state.bet_size = config.BET_SIZE
        state.trade_window = config.TRADE_LAST_SECONDS
        state.max_buy_price = config.MAX_BUY_PRICE
        state.min_buy_price = config.MIN_BUY_PRICE
        state.mode = str(getattr(main_bot, "execution_mode", "LIVE") or "LIVE").upper()

    # ---- orderbook -------------------------------------------------------
    def wrap_book(orig):
        def inner(token_id, *a, **kw):
            t0 = time.monotonic()
            out = orig(token_id, *a, **kw)
            try:
                bids, asks = out
                state.push_book(token_id, bids, asks, (time.monotonic() - t0) * 1000.0)
            except Exception as exc:
                _telemetry_failed(state, "book probe", exc)
            return out
        return inner
    _patch(orderbook, "get_orderbook", wrap_book)

    def wrap_liq(orig):
        def inner(bids, asks):
            out = orig(bids, asks)
            try:
                with state.lock():
                    if (state.round_key is not None
                            and state.strategy_round_key == state.round_key):
                        state.sig_book.set(out)
            except Exception as exc:
                _telemetry_failed(state, "liquidity probe", exc)
            return out
        return inner
    _patch(orderbook, "liquidity_signal", wrap_liq)

    # ---- strategy --------------------------------------------------------
    # Price and Chainlink signals are explicitly tagged with their bot round.
    # Inferring the source from alternating decide() calls breaks as soon as a
    # phase abstains or performs an extra pre-submit validation.
    def wrap_price_signal(orig):
        def inner(round_key, start, current):
            out = orig(round_key, start, current)
            try:
                with state.lock():
                    if state.round_key is not None and round_key == state.round_key:
                        if start is not None:
                            state.start_price.set(
                                start, source="main_bot.price_signal arg")
                        state.sig_price.set(out)
            except Exception as exc:
                _telemetry_failed(state, "price signal probe", exc)
            return out
        return inner
    _patch(main_bot, "price_signal", wrap_price_signal)

    def wrap_chainlink_signal(orig):
        def inner(round_key, start, current):
            out = orig(round_key, start, current)
            try:
                with state.lock():
                    if state.round_key is not None and round_key == state.round_key:
                        if start is not None:
                            state.push_price_to_beat(
                                start, source="main_bot.chainlink_signal arg",
                                round_key=round_key)
                        state.sig_chainlink.set(out)
            except Exception as exc:
                _telemetry_failed(state, "Chainlink signal probe", exc)
            return out
        return inner
    _patch(main_bot, "chainlink_signal", wrap_chainlink_signal)

    def wrap_final(orig):
        def inner(price_side, book_side, chainlink_side):
            out = orig(price_side, book_side, chainlink_side)
            try:
                with state.lock():
                    if (state.round_key is not None
                            and state.strategy_round_key == state.round_key):
                        state.decision.set(out)
                        state.decision_forced = False
            except Exception as exc:
                _telemetry_failed(state, "decision probe", exc)
            return out
        return inner
    _patch(strategy, "final_decision", wrap_final)

    # ---- market discovery ------------------------------------------------
    def wrap_tokens(orig):
        def inner(*a, **kw):
            t0 = time.monotonic()
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.tokens.set(out, latency_ms=(time.monotonic() - t0) * 1000.0)
                    # H6: a previous-round market is older than the live window.
                    # Prewarming the *next* slug is expected and is not a fallback.
                    got = out.get("window_start") if isinstance(out, dict) else None
                    want = market_discovery._current_5m_window_start_unix()
                    try:
                        state.token_fallback = got is not None and int(got) < int(want)
                    except (TypeError, ValueError, OverflowError) as exc:
                        state.token_fallback = False
                        _telemetry_failed(state, "market window", exc)
                    if state.token_fallback:
                        state.event("MARKET", f"PREV-WINDOW FALLBACK {out.get('slug')}", "bad")
            except Exception as exc:
                _telemetry_failed(state, "market probe", exc)
            return out
        return inner
    _patch(market_discovery, "get_tokens_for_current_round", wrap_tokens)

    # ---- loop heartbeat --------------------------------------------------
    # run_bot calls seconds_left() once per iteration, so wrapping it gives a
    # true liveness signal and the round clock exactly as the BOT sees it —
    # not a second clock computed by the renderer.
    def wrap_secs(orig):
        def inner(*a, **kw):
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.loop_beat.set(out)
                    state.seconds_left = out
            except Exception as exc:
                _telemetry_failed(state, "clock probe", exc)
            return out
        return inner
    _patch(main_bot, "seconds_left", wrap_secs)

    # ---- execution (bound into main_bot at import time) -------------------
    def wrap_place(orig):
        def inner(side, amount, up_id=None, down_id=None, *args, **kwargs):
            t0 = time.monotonic()
            ok = orig(side, amount, up_id, down_id, *args, **kwargs)
            try:
                ms = (time.monotonic() - t0) * 1000.0
                # main_bot's own `last_order_error` copy is bound at import and
                # stays None (C3). Read the live module attribute instead.
                err = getattr(polymarket_trade, "last_order_error", None)
                with state.lock():
                    paper = state.mode == "PAPER"
                state.record_order(side, amount, bool(ok), err, ms,
                                   count_stake=paper)
                if ok:
                    suffix = "PAPER FILLED" if paper else "SENT FOK"
                    clean_amount = TerminalState._finite(amount, nonnegative=True)
                    amount_text = f"${clean_amount:.2f}" if clean_amount is not None else "$--"
                    state.flash(f"ENTRY {side}", f"{amount_text} {suffix}", "good", 2.4)
                else:
                    state.flash("ORDER REJECTED", terminal_text(err or "unknown", 44), "bad", 3.0)
            except Exception as exc:
                _telemetry_failed(state, "order probe", exc)
            return ok
        return inner
    _patch(main_bot, "place_trade", wrap_place)

    def wrap_cancel(orig):
        def inner(*a, **kw):
            t0 = time.monotonic()
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.cancel.set(bool(out), latency_ms=(time.monotonic() - t0) * 1000.0)
            except Exception as exc:
                _telemetry_failed(state, "cancel probe", exc)
            return out
        return inner
    _patch(main_bot, "cancel_all_open_orders", wrap_cancel)

    def wrap_balance(orig):
        def inner(*a, **kw):
            out = orig(*a, **kw)
            try:
                with state.lock():
                    state.balance.set(out)
            except Exception as exc:
                _telemetry_failed(state, "balance probe", exc)
            return out
        return inner
    _patch(main_bot, "get_balance_allowance", wrap_balance)

    # ---- honest gap register --------------------------------------------
    gaps = {
        "REDEMPTION": (
            "official outcomes settle the local ledger, but this build does not "
            "submit an on-chain redeem transaction"
        ),
    }
    for key, why in gaps.items():
        state.note_absent(key, why)

    mirror = _open_private_mirror(mirror_path) if mirror_path else None
    _sink = EventSink(state, mirror)
    sys.stdout = _sink
    _installed = True
    return _orig_stdout


def uninstall() -> None:
    """Restore every patched attribute and stdout. Used by the tests."""
    global _installed, _orig_stdout, _sink
    with _lifecycle_lock:
        state = _sink.state if _sink is not None else None
        failures = _restore_patches()
        if _sink is not None:
            if sys.stdout is _sink and _orig_stdout is not None:
                sys.stdout = _orig_stdout
            _sink.finish()
        _sink = None
        _orig_stdout = None
        _installed = False
        if state is not None:
            for failure in failures:
                state.event("DASH", failure, "bad")
