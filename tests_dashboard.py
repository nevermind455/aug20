#!/usr/bin/env python3
"""Headless tests for the terminal layer.

Two jobs:
  1. Prove the dashboard renders correctly at any terminal size.
  2. Prove the dashboard did not change the bot.

Run:  python tests_dashboard.py
Needs no TTY, no network, no venue credentials.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import os
import pathlib
import subprocess
import sys
import types
import unicodedata

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


# --------------------------------------------------------------- stubbing ---
def stub_missing() -> None:
    """Stub venue SDKs so the suite runs on any machine. Never used to fake
    a value that reaches the screen — only to make imports resolve."""
    if "websockets" not in sys.modules:
        m = types.ModuleType("websockets")
        m.connect = lambda *a, **k: None
        sys.modules["websockets"] = m
    if "web3" not in sys.modules:
        m = types.ModuleType("web3")

        class _W3:
            def __init__(self, *a, **k): pass
            def is_connected(self): return False
            @staticmethod
            def HTTPProvider(*a, **k): return None
            @staticmethod
            def to_checksum_address(a): return a
        m.Web3 = _W3
        sys.modules["web3"] = m
    if "py_clob_client_v2" not in sys.modules:
        m = types.ModuleType("py_clob_client_v2")
        for n in ("AssetType", "BalanceAllowanceParams", "ClobClient",
                  "MarketOrderArgs", "OrderType", "PartialCreateOrderOptions", "Side"):
            setattr(m, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
        sys.modules["py_clob_client_v2"] = m


stub_missing()

from dashboard import TerminalState, build, snapshot  # noqa: E402
from dashboard.renderer import (ALT_OFF, ALT_ON, CLEAR, CURSOR_ON,  # noqa: E402
                                PlainRenderer, Renderer, render_row)
from dashboard.theme import UNICODE, Style  # noqa: E402
from dashboard.widgets import big_digits, hsplit, pad, table  # noqa: E402

SIZES = [(c, r) for c in (40, 56, 64, 72, 80, 84, 90, 100, 110, 120, 140, 160, 200, 240)
         for r in (10, 12, 14, 16, 18, 20, 24, 28, 30, 34, 40, 48, 60)]


# ------------------------------------------------------------ 1. geometry ---
def populated() -> TerminalState:
    """A state filled from values a real run would produce.

    These are inputs to the RENDERER under test, never displayed to a user
    as if they came from the venue.
    """
    st = TerminalState()
    st.set_round_context(1_754_780_700, "9:05PM-9:10PM ET", 47)
    st.bet_size, st.trade_window, st.max_buy_price = 2.0, 60, 0.99
    st.min_buy_price = 0.20
    base = 64_890.0
    for i in range(400):
        st.push_spot(base + (i % 37) - 18 + (i * 0.11))
    st.push_chainlink(64_894.0, 812.0)
    st.push_chainlink(64_894.0, 790.0)
    st.push_book("7213...UP",
                 [{"price": "0.47", "size": "310"}, {"price": "0.46", "size": "900"}],
                 [{"price": "0.52", "size": "180"}, {"price": "0.53", "size": "640"}], 121.0)
    st.start_price.set(64_894.0, source="ROUND log line")
    st.push_price_to_beat(64_894.0)
    st.sig_price.set("UP"); st.sig_book.set("DOWN"); st.sig_chainlink.set("UP")
    st.decision.set("UP")
    st.balance.set({"balance": 41.37, "allowance": 1e6})
    st.tokens.set({"slug": "btc-updown-5m-1754780700", "up_token_id": "72131"})
    st.cancel.set(True)
    st.loop_beat.set(47)
    st.record_order("UP", 2.0, True, None, 640.0)
    st.record_order("DOWN", 2.0, False, "not enough balance", 410.0)
    for i in range(30):
        st.event("BOT", f"line {i}", "info")
    st.trades = [{"time_et": "Aug 09 21:04:58 ET", "side": "UP", "amount": 2.0,
                  "price_side": "UP", "book_side": "DOWN", "chainlink_side": "UP",
                  "result": "ok"}] * 12
    return st


def test_geometry() -> None:
    for st, tag in ((TerminalState(), "empty"), (populated(), "full")):
        snap = snapshot(st, session_trades=st.trades)
        for cols, rows in SIZES:
            frame = build(snap, cols, rows, UNICODE)
            check(f"rowcount {tag} {cols}x{rows}",
                  len(frame) == rows, f"got {len(frame)}")
            bad = [(i, sum(len(t) for t, _ in row)) for i, row in enumerate(frame)
                   if sum(len(t) for t, _ in row) != cols]
            check(f"width {tag} {cols}x{rows}", not bad, f"rows {bad[:3]}")


def test_one_sided_books_render_at_every_size() -> None:
    """A temporarily empty side of the live book must not break the frame."""
    fixtures = (
        ("ask-only", [], [{"price": "0.52", "size": "180"}]),
        ("bid-only", [{"price": "0.47", "size": "310"}], []),
    )
    for tag, bids, asks in fixtures:
        st = populated()
        st.push_book("7213...UP", bids, asks, 121.0)
        snap = snapshot(st, session_trades=st.trades)
        for cols, rows in SIZES:
            try:
                frame = build(snap, cols, rows, UNICODE)
            except Exception as exc:
                check(f"one-sided render {tag} {cols}x{rows}", False,
                      f"{type(exc).__name__}: {exc}")
                continue
            bad = [(i, sum(len(t) for t, _ in row))
                   for i, row in enumerate(frame)
                   if sum(len(t) for t, _ in row) != cols]
            check(f"one-sided render {tag} {cols}x{rows}",
                  len(frame) == rows and not bad,
                  f"rows={len(frame)}, bad_widths={bad[:3]}")


def test_no_wide_or_control_chars() -> None:
    """A double-width or control character silently shifts every later column."""
    snap = snapshot(populated(), session_trades=[])
    for cols, rows in ((120, 40), (84, 24), (200, 60)):
        for row in build(snap, cols, rows, UNICODE):
            for text, _ in row:
                for ch in text:
                    check("no control char", ch == "\n" or ord(ch) >= 32 or ch == " ",
                          repr(ch))
                    check("single width",
                          unicodedata.east_asian_width(ch) not in ("W", "F"), repr(ch))


# ----------------------------------------------------- 1b. zero `#` in UI ---
HASH_SIZES = [(40, 12), (56, 10), (64, 20), (72, 16), (84, 24), (100, 30),
              (120, 40), (160, 48), (200, 34), (240, 60)]


def ui_states() -> list[tuple[str, TerminalState]]:
    """One fixture per state the dashboard has to survive."""
    import time as _time

    out: list[tuple[str, TerminalState]] = [("startup", TerminalState())]

    waiting = TerminalState()
    waiting.round_label = "9:05PM-9:10PM ET"
    waiting.seconds_left = 240
    out.append(("waiting for market", waiting))

    out.append(("active market", populated()))

    for side in ("UP", "DOWN"):
        st = populated()
        st.sig_price.set(side); st.sig_book.set(side); st.sig_chainlink.set(side)
        st.decision.set(side)
        out.append((f"{side.lower()} signal", st))

    filled = populated()
    filled.record_order("UP", 5.0, True, None, 210.0)
    filled.flash("FILLED", "UP $5.00 @ 0.52", "good")
    out.append(("order filled", filled))

    rejected = populated()
    rejected.record_order("UP", 5.0, False,
                          "cannot FOK buy UP: no asks on the live book", 180.0)
    rejected.flash("REJECTED", "no asks on the live book", "bad")
    out.append(("order rejected", rejected))

    base_acct = {"realized_pnl": 0.0, "unrealized_mark_to_bid": 0.0,
                 "pending_cost": 5.0, "win_rate": 0.5, "wins": 5, "losses": 5,
                 "open_positions": 1, "cash": 1000.0}
    for name, pnl, wins, losses in (("settlement", 4.9, 8, 5),
                                    ("positive pnl", 128.5, 10, 4),
                                    ("negative pnl", -18.75, 2, 9)):
        st = populated()
        st.accounting = dict(base_acct, total_pnl=pnl, realized_pnl=pnl,
                             wins=wins, losses=losses)
        st.balance.set({"balance": 1000.0 + pnl, "allowance": 1e6, "paper": True})
        out.append((name, st))

    # The endgame book, which the bot meets in the last minute of every round:
    # the winning token keeps only bids, the losing token only asks.
    bids = [{"price": "0.99", "size": "12783"}]
    asks = [{"price": "0.01", "size": "12796"}]
    for name, b, a in (("one-sided book: bids only", bids, []),
                       ("one-sided book: asks only", [], asks),
                       ("empty book", [], [])):
        st = populated()
        st.push_book("7213...UP", b, a, 118.0)
        st.push_down_book("4491...DOWN", a, b, 118.0)
        out.append((name, st))

    no_balance = populated()
    no_balance.balance.set({"balance": None, "allowance": None, "paper": True})
    out.append(("balance not yet read", no_balance))

    dead = populated()
    stale = _time.monotonic() - 900.0
    dead.spot_changed.at = dead.book.at = dead.chainlink.at = stale
    dead.loop_beat.at = stale
    dead.event("WS", "reconnecting after socket close", "warn")
    out.append(("reconnecting feeds", dead))

    return out


def test_no_hash_glyph_in_any_state() -> None:
    """`#` must never reach the screen: not as a bar, a candle or a numeral."""
    for name, st in ui_states():
        snap = snapshot(st, session_trades=st.trades)
        for cols, rows in HASH_SIZES:
            for i, row in enumerate(build(snap, cols, rows, UNICODE)):
                line = "".join(t for t, _ in row)
                check(f"no hash: {name} {cols}x{rows}", "#" not in line,
                      f"row {i}: {line.strip()[:70]!r}")


def test_build_never_raises() -> None:
    """A render exception kills the panel silently: the dashboard task is
    never awaited, so the screen just stops and the bot trades on unseen.
    The one-sided book that does it appears in the last minute of every
    round, which is why this has to hold for every state at every size."""
    for name, st in ui_states():
        snap = snapshot(st, session_trades=st.trades)
        for cols, rows in HASH_SIZES:
            try:
                build(snap, cols, rows, UNICODE)
                check(f"build survives {name} {cols}x{rows}", True)
            except Exception as exc:
                check(f"build survives {name} {cols}x{rows}", False,
                      f"{type(exc).__name__}: {exc}")


def test_no_hash_across_every_size() -> None:
    """The resize sweep: no width may bring a fallback renderer back."""
    snap = snapshot(populated(), session_trades=[])
    for cols, rows in SIZES:
        for row in build(snap, cols, rows, UNICODE):
            check(f"no hash on resize {cols}x{rows}",
                  "#" not in "".join(t for t, _ in row))


def test_no_hash_in_rendered_bytes() -> None:
    """Through the real renderer, escape codes and all."""
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    snap = snapshot(populated(), session_trades=[])
    for cols, rows in ((120, 40), (84, 24)):
        tty = Tty()
        r = Renderer(tty)
        r.cols, r.rows = cols, rows
        r.start()
        r.draw(build(snap, cols, rows, r.g))
        r.stop()
        out = tty.getvalue()
        check(f"renderer emits no hash {cols}x{rows}", "#" not in out,
              repr(out[:80]))
        check(f"renderer emits block glyphs {cols}x{rows}", "█" in out)


def test_hsplit_exact() -> None:
    for total in range(30, 260):
        parts = hsplit(total, [0.24, 0.30, 0.46], [28, 32, 30])
        check("hsplit sums", sum(parts) == total, f"{total} -> {parts}")
        check("hsplit positive", all(p >= 3 for p in parts), str(parts))


def test_table_and_pad() -> None:
    rows = table(["A", "B"], [4, 5], [[("x", Style()), ("y", Style())]], 30, 4)
    for r in rows:
        check("table width", sum(len(t) for t, _ in r) == 30)
    check("pad truncates", sum(len(t) for t, _ in pad([("abcdef", Style())], 3)) == 3)
    check("big digits fit", all(sum(len(t) for t, _ in r) == 20
                                for r in big_digits("41.37", 20, Style())))
    check("big digits fallback on non-numeric",
          "".join(t for t, _ in big_digits("--", 20, Style())[1]).strip() == "--")
    from dashboard.widgets import giant_digits
    giant = giant_digits("$41.37", 48, Style("green", bold=True), g=UNICODE)
    check("giant cash is five rows", len(giant) == 5, str(len(giant)))
    check("giant cash uses block glyphs",
          any("\u2588" in t for row in giant for t, _ in row))
    check("giant cash stays in width",
          all(sum(len(t) for t, _ in r) == 48 for r in giant))
    lines = ["".join(t for t, _ in row) for row in giant]
    # Weight: a terminal cell is twice as tall as it is wide, so a
    # single-column upright reads as a hairline against a full-width bar.
    check("giant cash uprights are two columns wide",
          all("██" in ln for ln in lines), str(lines))
    # The pattern tables spell "off" with spaces; a stray dot or hash in one
    # of them would print as itself.
    check("giant cash draws nothing but blocks and the currency mark",
          all(set(ln) <= {" ", "█", "$"} for ln in lines), str(lines))
    check("the currency mark sits on the centre line alone",
          "$" in lines[2] and not any("$" in ln for i, ln in enumerate(lines) if i != 2),
          str(lines))
    check("the decimal point sits on the baseline",
          lines[4].count("█") > lines[3].count("█"), str(lines[3:]))
    wide = giant_digits("$100000.00", 30, Style("green", bold=True), g=UNICODE)
    check("a figure too wide for the block font keeps the panel's height",
          len(wide) == 5 and all(sum(len(t) for t, _ in r) == 30 for r in wide),
          str(len(wide)))
    narrow = giant_digits("$41.37", 26, Style("green", bold=True), g=UNICODE)
    check("the narrow block font is still blocks only",
          all(set("".join(t for t, _ in row)) <= {" ", "█", "$"} for row in narrow),
          str(["".join(t for t, _ in row) for row in narrow]))


# ------------------------------------------------------------ 2. renderer ---
class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_frame_diff() -> None:
    st = populated()
    snap = snapshot(st, session_trades=st.trades)
    out = FakeTTY()
    r = Renderer(out)
    r.cols, r.rows = 120, 40
    r.size = lambda: (120, 40)
    r.start()
    out.truncate(0); out.seek(0)

    frame = build(snap, 120, 40, r.g)
    r.draw(frame)
    first = out.getvalue()
    check("first frame clears once", first.count(CLEAR) == 1, str(first.count(CLEAR)))

    out.truncate(0); out.seek(0)
    r.draw(frame)
    check("identical frame writes nothing", out.getvalue() == "", repr(out.getvalue()[:80]))

    out.truncate(0); out.seek(0)
    changed = [list(x) for x in frame]
    changed[7] = pad([("ZZZ", Style())], 120)
    r.draw(changed)
    body = out.getvalue()
    check("changed frame writes something", body != "")
    check("no full clear on update", CLEAR not in body)
    check("only the changed row is addressed", body.count("\x1b[8;1H") == 1, body[:60])
    check("one row rewritten", len([1 for i in range(1, 41)
                                    if f"\x1b[{i};1H" in body]) <= 2)

    out.truncate(0); out.seek(0)
    r._resized = True
    r.draw(frame)
    check("resize forces repaint", CLEAR in out.getvalue())

    out.truncate(0); out.seek(0)
    r.stop()
    tail = out.getvalue()
    check("cursor restored", CURSOR_ON in tail)
    check("alt screen exited", ALT_OFF in tail)


def test_context_manager_restores_on_exception() -> None:
    out = FakeTTY()
    r = Renderer(out)
    caught = False
    try:
        with r:
            raise RuntimeError("boom")
    except RuntimeError:
        caught = True
    check("test exception was observed", caught)
    check("restore after exception", CURSOR_ON in out.getvalue() and ALT_OFF in out.getvalue())


def test_non_tty_emits_no_escapes() -> None:
    out = io.StringIO()          # isatty() False
    r = PlainRenderer(out, every=0.0)
    st = populated()
    r.status(snapshot(st, session_trades=st.trades))
    body = out.getvalue()
    check("plain renderer emits no escapes", "\x1b" not in body, repr(body[:60]))
    check("plain renderer emits a status line", "[DASH]" in body)
    check("plain renderer names official round prices",
          "ptb=$64,894.00" in body and "running=$64,894.00" in body, body)
    check("make_renderer picks plain for a pipe",
          type(__import__("dashboard.renderer", fromlist=["make_renderer"])
               .make_renderer(io.StringIO())).__name__ == "PlainRenderer")


def test_selftest_survives_ascii_strict_stdout() -> None:
    """The direct preview write must initialize redirected legacy stdout."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "ascii:strict"
    env["TERM_PREVIEW"] = "40x12"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run_terminal.py"), "--selftest"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    detail = (proc.stderr or proc.stdout[-1000:]).decode("utf-8", "replace")
    check("selftest supports ascii-strict redirected stdout",
          proc.returncode == 0, detail)
    check("selftest completes its Unicode preview",
          b"selftest: PASS" in proc.stdout, detail)


def test_render_row_resets() -> None:
    line = render_row(pad([("hi", Style("green"))], 10))
    check("row ends reset", line.endswith("\x1b[0m"))
    check("alt on constant", ALT_ON.startswith("\x1b"))


# ------------------------------------------------- 3. data integrity ---
NUMERIC_BAN = ("0.00%", "$1,", "$2,", "12.3", "45.6")


def test_empty_state_invents_nothing() -> None:
    snap = snapshot(TerminalState(), session_trades=[])
    text = "\n".join("".join(t for t, _ in row) for row in build(snap, 160, 50, UNICODE))
    for key in ("PNL", "REALIZED", "EXPOSURE", "WINS", "SPOT",
                "PRICE TO BEAT", "RUNNING PRICE"):
        check(f"{key} present", key in text)
    check("shows the missing marker", "--" in text)
    check("no fabricated dollar pnl", "$-" not in text and "+$" not in text, "")
    for pat in NUMERIC_BAN:
        check(f"no demo number {pat}", pat not in text)
    check("absent modules labelled", "ABSENT" in text)


def test_round_panel_uses_official_chainlink_pair() -> None:
    """Binance is auxiliary; it must never be labelled as market strike."""
    st = populated()
    st.start_price.set(100.0, source="Binance boundary print")
    st.push_price_to_beat(200.0)
    st.push_chainlink(210.0, 25.0, observation_id=1_754_780_747_000)
    snap = snapshot(st, session_trades=[])
    lines = ["".join(text for text, _ in row)
             for row in build(snap, 160, 50, UNICODE)]
    ptb = next((line for line in lines if "PRICE TO BEAT" in line), "")
    running = next((line for line in lines if "RUNNING PRICE" in line), "")
    distance = next((line for line in lines if "DIST TO BEAT" in line), "")
    full = "\n".join(lines)
    check("Price To Beat renders Chainlink opening TWAP", "$200.00" in ptb, ptb)
    check("Running Price renders current Chainlink TWAP", "$210.00" in running,
          running)
    check("distance compares the two official TWAP values", "+10.00" in distance,
          distance)
    check("Binance opening print is never called strike",
          "STRIKE (start px)" not in full, full)


def test_round_rollover_clears_old_price_and_signal_state() -> None:
    st = TerminalState()
    check("first round context is a transition",
          st.set_round_context(300, "ROUND A", 200))
    st.start_price.set(100.0)
    st.push_price_to_beat(200.0)
    st.sig_price.set("UP")
    st.sig_book.set("DOWN")
    st.sig_chainlink.set("UP")
    st.decision.set("UP")
    st.decision_forced = True

    check("same round does not erase observations",
          not st.set_round_context(300, "ROUND A", 199)
          and st.start_chainlink.value == 200.0
          and st.decision.value == "UP")
    check("new round is detected",
          st.set_round_context(600, "ROUND B", 300))
    snap = snapshot(st, session_trades=[])
    check("new label and key commit with reset",
          snap["round_key"] == 600 and snap["round_label"] == "ROUND B"
          and snap["seconds_left"] == 300, repr(snap["round_key"]))
    check("old opening values cannot cross a boundary",
          snap["start_price"] is None and snap["start_chainlink"] is None)
    check("old signals and decision cannot cross a boundary",
          all(snap[key] is None for key in
              ("sig_price", "sig_book", "sig_chainlink", "decision"))
          and not snap["decision_forced"])


def test_running_price_clears_when_chainlink_is_unavailable() -> None:
    st = TerminalState()
    st.push_chainlink(210.0, 20.0, observation_id=1_000)
    calls = st.chainlink.count
    st.push_chainlink(210.0, 30.0, observation_id=1_000)
    check("dashboard polling does not duplicate an RTDS observation",
          st.chainlink.count == calls and st.chainlink_repeat == 0)
    st.push_chainlink(210.0, 5.0, observation_id=2_000)
    check("equal values from distinct RTDS observations count once each",
          st.chainlink.count == calls + 1 and st.chainlink_repeat == 1)
    st.push_chainlink(211.0, 4.0, observation_id=2_000)
    check("a corrected value at the same timestamp is not deduplicated",
          st.chainlink.value == 211.0 and st.chainlink.count == calls + 2
          and st.chainlink_repeat == 0)
    st.push_chainlink(None, 500.0, observation_id=2_000)
    snap = snapshot(st, session_trades=[])
    check("missing or stale Chainlink hides the old numeric value",
          snap["chainlink"] is None and snap["chainlink_age"] is None
          and snap["chainlink_repeat"] == 0)


def test_late_old_round_strategy_probe_cannot_repopulate_reset_state() -> None:
    """An awaited old validation may finish after the wall-clock boundary."""
    os.environ.setdefault("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    import main_bot
    import orderbook
    import strategy
    from dashboard import probe

    st = TerminalState()
    st.set_round_context(300, "ROUND A", 1)
    st.mark_strategy_round(300)
    st.push_price_to_beat(200.0, round_key=300)
    real_stdout = probe.install(st)
    try:
        st.set_round_context(600, "ROUND B", 300)
        # These are the old coroutine's late final-validation callbacks.
        main_bot.price_signal(300, 100.0, 101.0)
        main_bot.chainlink_signal(300, 200.0, 201.0)
        orderbook.liquidity_signal(
            [{"price": 0.4, "size": 10.0}],
            [{"price": 0.6, "size": 20.0}],
        )
        strategy.final_decision("UP", "DOWN", "UP")
        snap = snapshot(st, session_trades=[])
        check("late old Price To Beat stays rejected",
              snap["start_chainlink"] is None)
        check("late old signals and decision stay rejected",
              all(snap[key] is None for key in
                  ("sig_price", "sig_book", "sig_chainlink", "decision")))
    finally:
        probe.uninstall()
        sys.stdout = real_stdout


def test_explicit_round_keyed_signal_probes_never_swap_sources() -> None:
    os.environ.setdefault("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    import main_bot
    from dashboard import probe

    st = TerminalState()
    st.set_round_context(300, "ROUND A", 200)
    st.mark_strategy_round(300)
    real_stdout = probe.install(st)
    try:
        main_bot.price_signal(300, 100.0, 101.0)
        snap = snapshot(st, session_trades=[])
        check("a solitary phase-1 call updates only SIG PRICE",
              snap["sig_price"] == "UP" and snap["sig_chainlink"] is None,
              str((snap["sig_price"], snap["sig_chainlink"])))

        main_bot.chainlink_signal(300, 200.0, 199.0)
        snap = snapshot(st, session_trades=[])
        check("the next explicit Chainlink call cannot steal the price slot",
              snap["sig_price"] == "UP" and snap["sig_chainlink"] == "DOWN",
              str((snap["sig_price"], snap["sig_chainlink"])))

        main_bot.price_signal(300, 100.0, 100.0)
        snap = snapshot(st, session_trades=[])
        check("a neutral current price clears SIG PRICE visibly",
              snap["sig_price"] is None and snap["sig_chainlink"] == "DOWN",
              str((snap["sig_price"], snap["sig_chainlink"])))

        main_bot.price_signal(0, 100.0, 101.0)
        check("an old round cannot overwrite the neutral current-round signal",
              snapshot(st, session_trades=[])["sig_price"] is None)
    finally:
        probe.uninstall()
        sys.stdout = real_stdout


def test_absent_are_absent_not_disconnected() -> None:
    h = TerminalState().feed_health()
    for k in ("POLY WS", "USER WS", "DATABASE", "RECONCILE", "SETTLEMENT"):
        check(f"{k} is ABSENT", h[k] == "ABSENT", h[k])
    check("binance starts WAIT not OK", h["BINANCE WS"] == "WAIT", h["BINANCE WS"])


def test_staleness_marks_rather_than_hides() -> None:
    st = populated()
    st.spot_changed.at -= 30.0          # simulate a silent feed
    snap = snapshot(st, session_trades=[])
    check("stale feed is marked", snap["spot_status"] in ("STALE", "DISCONNECTED"),
          snap["spot_status"])
    check("last value retained", snap["spot"] is not None)


def test_overlay_preserves_geometry() -> None:
    st = populated()
    st.flash("+$4.52", "ORDER FILLED", "good")
    snap = snapshot(st, session_trades=st.trades)
    for cols, rows in ((120, 40), (84, 24), (200, 60)):
        frame = build(snap, cols, rows, UNICODE)
        check("overlay keeps rowcount", len(frame) == rows)
        for row in frame:
            check("overlay keeps width", sum(len(t) for t, _ in row) == cols)
        check("overlay text present",
              "+$4.52" in "".join("".join(t for t, _ in r) for r in frame))


# ----------------------------------------- 4. REGRESSION: bot unchanged ---
TRADING_FILES = ["main_bot.py", "strategy.py", "polymarket_trade.py", "orderbook.py",
                 "chainlink.py", "market_discovery.py", "price_ws.py", "timer.py",
                 "config.py"]

BASELINE_SHA = {  # approved trading-file baseline; intentional changes require review
    # Re-approved 2026-08-25: see the matching note in tests_feeds.py for
    # what changed - http pooling, per-signal legs, and two ordering fixes.
    # Re-approved 2026-08-17: a fourth phase-1 band (T-120..T-60, 0.55-0.75,
    # 8s cadence) replaces phase 2's signal path, which is now off by default.
    # Bands may carry their own cadence as an optional 5th field, and a band
    # whose prices exceed BET_SIZE/5 announces its venue-minimum sizing at
    # startup rather than inflating the stake silently.
    # Re-approved 2026-08-17: PAPER no longer substitutes a mid-round price
    # when it misses the round's opening observation. It skipped the round in
    # LIVE and silently measured a different question in PAPER - 4.9% of
    # phase-2 fills, one of them $58 the wrong side of the true strike.
    # Re-approved 2026-08-17: phase 2 no longer trades the final minute.
    # MIN_SECONDS_TO_EXPIRY 1 -> 60 after 16 fills there won 31.2% against a
    # 69.6% break-even (z = -3.29) - the one-sided endgame book only offers
    # the side the market wants to sell. T-120..T-60 stays open.
    # Re-approved 2026-08-17: the strike now reads Chainlink's 60-second TWAP
    # (crypto_prices_twap_sixty), which is the stream the market's own
    # resolution text names. The 30-second stream it used before is a
    # different series and disagrees by about a dollar at any instant.
    # Re-approved 2026-08-17 for per-window phase-1 bands: PHASE1_BANDS drives
    # selection, and each band's ceiling now travels with the order as a price
    # cap (paper and live), so a thin best level can no longer walk the book
    # and fill outside the range being measured.
    # Re-approved 2026-08-17 for the two-phase entry plan: phase 1 buys a
    # price band (T-300..T-120, no signal call), phase 2 is the unchanged
    # signal path, parked behind PHASE2_ENABLED for the measurement period.
    # BET_SIZE defaults to the 5-share venue minimum at the band top.
    # Re-approved 2026-08-16 after the venue-contract tightening landed:
    # fees only simulated when the venue marks them taker-charged (`fd.to`),
    # a market declaring an undisclosed matching delay is refused before
    # signing, a matched FOK must report integral execution amounts, a fill
    # only counts once it carries token and market, resolution must supply
    # both outcomes, and a socket book with no exchange timestamp falls back
    # to REST instead of being stamped with receipt time.
    # main_bot.py / timer.py / market_discovery.py were re-approved on
    # 2026-08-15 after PAPER clock-offset handling. Prior main_bot digest:
    # 87f6b7ed2a79...  Anything else here changing is still an unreviewed
    # edit to the trading path.
    # price_ws.py re-approved 2026-08-17: receipt age is staleness; exchange
    # age only rejects impossible stamps so a 3s CLOB/Binance clock offset
    # cannot blank a just-received print. Prior digest: 56a3272be9a1...
    # main_bot.py / orderbook.py re-approved on 2026-08-15 for the
    # unbuyable-side gate: liquidity_signal abstains on a one-sided book and
    # the loop preflights the chosen token before submitting. Prior digests:
    # main_bot 7081505ef23e..., orderbook d3625fe3247b...
    # main_bot.py re-approved on 2026-08-15 for the per-round trade log: the
    # round-rollover block also clears session_trades, the display list behind
    # RECENT TRADES. No decision, sizing or submission path changed. Prior
    # main_bot digest: 8a49f0d7f51c...
    "chainlink.py": "c20ac69ee93bb06df32552d3cd802ae3b45137dbfd0151ddd19a46e9c29a671d",
    # Re-approved 2026-08-25: the PAPER-only signal-flip experiment requires
    # Phase 1 parked and Phase 2 enabled, preventing overlapping cadences.
    "config.py": "9e5f506a7b9fecfa0855cb82ba1b82b9e2a3dd3e4f8880805fd5170dec0d7365",
    # Re-approved 2026-08-25: restart restores durable held-token legs before
    # both phase paths can buy the complementary outcome, and LIVE rechecks a
    # sent, heartbeat-proven private fill subscription before each submission.
    # Re-approved 2026-08-25: both phases are gated by round-keyed fresh
    # Binance SIG PRICE, with the same permit rechecked at executor commit.
    # Re-approved 2026-08-25: PAPER may acquire the complementary outcome only
    # after a fresh, round-local SIG PRICE epoch; LIVE and ambiguous restarts
    # remain blocked, and executor commit still rechecks the selected side.
    "main_bot.py": "08dbbec08c0d024e5a955de6167884bb8834bd9cbe0e52faf132c32c43869272",
    # Re-approved 2026-08-25: discovery now rejects any market whose venue
    # config is not explicitly BTC / 5m / enabled 60-second TWAP.
    "market_discovery.py": "b20c6c01d666aab8744b656449f6ed52c27feb8f72f70df417cf755e6a7dd149",
    "orderbook.py": "98ad9877e1032504010ba2b61e1237e70e76e377400c098bde75aa9a556031dc",
    "polymarket_trade.py": "94346efb01c64e26dd9020ea513eb20691059e701271cd1b661e0a79d4c68ecb",
    "price_ws.py": "0dc5e08fede52b8ec20d60cca83c6811baa811832d711f4c8236cf6128b628c7",
    "strategy.py": "95d46436999c5d5cdc24742b0fa4f40842017fe5aa89dcd691f72e4d76b81d91",
    "timer.py": "55cdaf1655b210c83791730b094e79d5c0b00a7e79b6a6ca2bd2950df21e3124",
}


def test_trading_file_baselines() -> None:
    for name in TRADING_FILES:
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        check(f"approved baseline {name}", digest == BASELINE_SHA[name],
              f"{digest[:12]} != {BASELINE_SHA[name][:12]}")


SIDES = (None, "UP", "DOWN")
PRICES = (None, 0.0, 64_000.0, 64_894.0, 64_894.01, 1e9, -5.0)


def _truth_tables(strategy) -> tuple[dict, dict]:
    a = {(s, c): strategy.decide(s, c) for s in PRICES for c in PRICES}
    b = {t: strategy.final_decision(*t) for t in itertools.product(SIDES, SIDES, SIDES)}
    return a, b


def test_decisions_identical_after_probe() -> None:
    os.environ.setdefault("POLY_PRIVATE_KEY", "0x" + "1" * 64)
    import main_bot
    import strategy
    from dashboard import probe

    before = _truth_tables(strategy)

    # spy on the real order path BEFORE probing, so we can prove the wrapper
    # passes arguments through untouched and returns the callee's own value
    seen: list[tuple] = []
    sentinel = object()
    main_bot.place_trade = lambda *a, **k: (seen.append((a, k)), sentinel)[1]
    main_bot.cancel_all_open_orders = lambda *a, **k: "CANCEL-RET"
    main_bot.get_balance_allowance = lambda *a, **k: {"balance": 1.0, "allowance": 2.0}

    st = TerminalState()
    real_stdout = probe.install(st)
    try:
        after = _truth_tables(strategy)
        check("decide() truth table unchanged", before[0] == after[0])
        check("final_decision() truth table unchanged", before[1] == after[1])

        ret = main_bot.place_trade("UP", 2.0, "UPID", "DOWNID")
        check("place_trade return passed through", ret is sentinel)
        check("place_trade args passed through",
              seen == [(("UP", 2.0, "UPID", "DOWNID"), {})], str(seen))
        check("cancel return passed through",
              main_bot.cancel_all_open_orders() == "CANCEL-RET")

        import orderbook
        bids = [{"price": "0.4", "size": "10"}]
        asks = [{"price": "0.6", "size": "20"}]
        check("liquidity_signal unchanged", orderbook.liquidity_signal(bids, asks) == "DOWN")

        # the sink must not print to the terminal
        print("[BOT] a captured line")
        check("stdout captured, not printed", isinstance(sys.stdout, probe.EventSink))
        check("captured line reached the feed",
              any("a captured line" in e.text for e in st.events))
    finally:
        probe.uninstall()
        sys.stdout = real_stdout

    check("uninstall restores strategy", _truth_tables(strategy) == before)


def test_probe_survives_telemetry_failure() -> None:
    """A bug in the dashboard must never reach a trading call."""
    import main_bot
    from dashboard import probe

    class Exploding(TerminalState):
        def record_order(self, *a, **k):
            raise RuntimeError("dashboard bug")

    calls: list = []
    main_bot.place_trade = lambda *a, **k: (calls.append(a), True)[1]
    st = Exploding()
    real = probe.install(st)
    try:
        check("order still succeeds when telemetry raises",
              main_bot.place_trade("UP", 2.0, "A", "B") is True)
        check("order still reached the venue path", len(calls) == 1)
    finally:
        probe.uninstall()
        sys.stdout = real


def test_order_response_requires_acceptance_evidence() -> None:
    import polymarket_trade as trade

    originals = {
        "client": trade._client,
        "Side": trade.Side,
        "OrderType": trade.OrderType,
        "MarketOrderArgs": trade.MarketOrderArgs,
        "PartialCreateOrderOptions": trade.PartialCreateOrderOptions,
        "AssetType": trade.AssetType,
        "BalanceAllowanceParams": trade.BalanceAllowanceParams,
        "sleep": trade.time.sleep,
        "book": trade.orderbook.validate_buy_liquidity,
        "observer": trade._order_observer,
        "journal_fault": trade._journal_fault,
        "trade_window": trade.config.TRADE_LAST_SECONDS,
        "min_expiry": trade.config.MIN_SECONDS_TO_EXPIRY,
        "ambiguous_condition": trade._ambiguous_condition,
        "ambiguous_until": trade._ambiguous_until,
    }
    up, down = "101", "202"
    condition = "0x" + "a" * 64
    window_end = (int(trade.time.time()) // 300 + 1) * 300

    class FakeClient:
        def __init__(self, response):
            self.response = response
        def get_clob_market_info(self, _condition):
            return {
                "t": [{"o": "Up", "t": up}, {"o": "Down", "t": down}],
                "mos": "1", "mts": "0.01", "nr": False,
                # `to` is the venue's own flag for "this curve is charged to
                # the taker"; without it the live path will not price a fill.
                # `itode` false because this endpoint never discloses the
                # delay's duration - a market that declares one is refused
                # outright, which the check below pins down separately.
                "fd": {"r": "0.07", "e": 1, "to": True}, "itode": False,
            }
        def get_balance_allowance(self, *_a, **_kw):
            return {"balance": "100000000",
                    "allowances": {"exchange": "100000000",
                                   "neg_risk": "100000000"}}
        def create_market_order(self, *_a, **_kw):
            return "signed"
        def post_order(self, *_a, **_kw):
            return self.response

    trade.Side = types.SimpleNamespace(BUY="BUY")
    trade.OrderType = types.SimpleNamespace(FOK="FOK")
    trade.MarketOrderArgs = lambda **kw: kw
    trade.PartialCreateOrderOptions = lambda **kw: kw
    trade.AssetType = types.SimpleNamespace(COLLATERAL="COLLATERAL")
    trade.BalanceAllowanceParams = lambda **kw: kw
    trade.orderbook.validate_buy_liquidity = lambda *_a, **_kw: (
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.50", "size": "100"}])
    trade.config.TRADE_LAST_SECONDS = 300
    trade.config.MIN_SECONDS_TO_EXPIRY = 0
    trade.set_order_observer(lambda _receipt: True)
    try:
        # A market that declares a matching delay without its duration cannot
        # be timed against the expiry cutoff, so it never reaches signing.
        class DelayedClient(FakeClient):
            def get_clob_market_info(self, condition):
                info = FakeClient.get_clob_market_info(self, condition)
                return {**info, "itode": True}

        trade._client = DelayedClient(
            {"success": True, "orderID": "accepted", "status": "matched",
             "tradeIDs": ["trade-0"]})
        trade._ambiguous_condition = None
        check("an undisclosed matching delay is refused before signing",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is False
              and "matching delay" in (trade.last_order_error or ""),
              str(trade.last_order_error))

        for response in (
            None,
            {},
            "venue rejected",
            {"errorMsg": "venue rejected"},
            {"success": False, "orderID": "ghost", "errorMsg": "venue rejected"},
            {"ok": False, "order_id": "ghost", "message": "venue rejected"},
            {"success": True},
            {"orderID": "ghost", "status": "failed"},
        ):
            trade._ambiguous_condition = None
            trade._ambiguous_until = 0
            trade._client = FakeClient(response)
            check(f"malformed/rejected response {response!r} is not success",
                  trade.place_trade("UP", 2.0, up, down, condition, window_end) is False,
                  str(trade.last_order_error))

        # A matched order must report what it actually executed, in base
        # units, or the receipt would be recording a guess.
        amounts = {"makingAmount": "2000000", "takingAmount": "4000000"}
        trade._client = FakeClient(
            {"success": True, "orderID": "accepted", "status": "matched",
             "tradeIDs": ["trade-1"], **amounts})
        trade._ambiguous_condition = None
        check("explicit venue acceptance succeeds",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is True)

        trade._client = FakeClient({"ok": True, "order_id": "accepted-v2",
                                    "status": "matched", "trade_ids": ["trade-2"],
                                    **amounts})
        check("current ok/order_id response succeeds",
              trade.place_trade("DOWN", 2.0, up, down, condition, window_end) is True)

        trade._client = FakeClient(
            {"success": True, "orderID": "no-amounts", "status": "matched",
             "tradeIDs": ["trade-x"]})
        trade._ambiguous_condition = None
        check("a matched response without execution amounts is not accepted",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is False
              and "execution amounts" in (trade.last_order_error or ""),
              str(trade.last_order_error))

        class RetryClient:
            def __init__(self):
                self.created = []
                self.posted = []
                self.responses = [
                    {"success": False, "errorMsg": "no match"},
                    {"success": False,
                     "errorMsg": "order couldn't be fully filled"},
                    {"success": True, "orderID": "fresh-third",
                     "status": "matched", "tradeIDs": ["trade-3"],
                     "makingAmount": "2000000", "takingAmount": "4000000"},
                ]
            def get_clob_market_info(self, _condition):
                return FakeClient(None).get_clob_market_info(_condition)
            def get_balance_allowance(self, *_a, **_kw):
                return FakeClient(None).get_balance_allowance()
            def create_market_order(self, *_a, **_kw):
                signed = f"signed-{len(self.created) + 1}"
                self.created.append(signed)
                return signed
            def post_order(self, signed, *_a, **_kw):
                self.posted.append(signed)
                return self.responses.pop(0)

        retry = RetryClient()
        trade.time.sleep = lambda *_a, **_kw: None
        trade._client = retry
        trade._ambiguous_condition = None
        check("explicit no-fill retries eventually succeed",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is True)
        check("every FOK retry is rebuilt and re-signed",
              retry.created == ["signed-1", "signed-2", "signed-3"] and
              retry.posted == retry.created, repr((retry.created, retry.posted)))

        class TimeoutClient:
            def __init__(self):
                self.created = 0
                self.posted = 0
            def get_clob_market_info(self, _condition):
                return FakeClient(None).get_clob_market_info(_condition)
            def get_balance_allowance(self, *_a, **_kw):
                return FakeClient(None).get_balance_allowance()
            def create_market_order(self, *_a, **_kw):
                self.created += 1
                return f"ambiguous-{self.created}"
            def post_order(self, *_a, **_kw):
                self.posted += 1
                raise TimeoutError("transport timeout")

        timeout = TimeoutClient()
        trade._client = timeout
        trade._ambiguous_condition = None
        check("ambiguous transport failure is not blindly retried",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is False and
              timeout.created == timeout.posted == 1,
              repr((timeout.created, timeout.posted)))

        trade._journal_fault = None
        trade._ambiguous_condition = None
        trade.set_order_observer(lambda _receipt: False)
        trade._client = FakeClient(
            {"success": True, "orderID": "unjournaled", "status": "matched",
             "tradeIDs": ["trade-unjournaled"],
             "makingAmount": "2000000", "takingAmount": "4000000"})
        check("an accepted order remains submitted when its journal fails",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is True)
        check("journal failure is explicit on the receipt and process state",
              trade.last_order_receipt["accounting_journaled"] is False and
              "CRITICAL" in (trade.last_order_error or "") and trade._journal_fault)
        check("journal failure disables every later live submission",
              trade.place_trade("UP", 2.0, up, down, condition, window_end) is False)
    finally:
        trade._client = originals["client"]
        trade.Side = originals["Side"]
        trade.OrderType = originals["OrderType"]
        trade.MarketOrderArgs = originals["MarketOrderArgs"]
        trade.PartialCreateOrderOptions = originals["PartialCreateOrderOptions"]
        trade.AssetType = originals["AssetType"]
        trade.BalanceAllowanceParams = originals["BalanceAllowanceParams"]
        trade.time.sleep = originals["sleep"]
        trade.orderbook.validate_buy_liquidity = originals["book"]
        trade.set_order_observer(originals["observer"])
        trade._journal_fault = originals["journal_fault"]
        trade.config.TRADE_LAST_SECONDS = originals["trade_window"]
        trade.config.MIN_SECONDS_TO_EXPIRY = originals["min_expiry"]
        trade._ambiguous_condition = originals["ambiguous_condition"]
        trade._ambiguous_until = originals["ambiguous_until"]


def test_collateral_balance_uses_pusd_units() -> None:
    import polymarket_trade as trade

    original = trade._client
    original_asset_type = trade.AssetType
    original_params = trade.BalanceAllowanceParams

    class BalanceClient:
        def get_balance_allowance(self, *_a, **_kw):
            return {
                "balance": "123450000",
                "allowances": {"exchange": "9000000", "neg_risk": "7500000"},
            }

    trade._client = BalanceClient()
    trade.AssetType = types.SimpleNamespace(COLLATERAL="COLLATERAL")
    trade.BalanceAllowanceParams = lambda **kw: kw
    try:
        result = trade.get_balance_allowance()
        check("pUSD balance converts six-decimal base units",
              result["balance"] == 123.45, str(result))
        check("pUSD allowance converts six-decimal base units",
              result["allowance"] == 7.5, str(result))
    finally:
        trade._client = original
        trade.AssetType = original_asset_type
        trade.BalanceAllowanceParams = original_params


# -------------------------------------------------------------------- main ---
def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception as exc:  # a crashing test is a failing test
            global FAIL
            FAIL += 1
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES[:25]:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
