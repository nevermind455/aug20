"""Runtime configuration with fail-fast validation."""
import math
import os
import re
import stat
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    if _ENV_PATH.is_symlink() or not _ENV_PATH.is_file():
        raise PermissionError(".env must be a regular, non-symlink file")
    # POSIX makes private-file permissions directly inspectable.  Windows
    # operators must apply the ACL documented in SECURITY.md.
    if os.name != "nt" and stat.S_IMODE(_ENV_PATH.stat().st_mode) & 0o077:
        raise PermissionError(".env contains live credentials and must have mode 0600")
load_dotenv(_ENV_PATH, override=False, encoding="utf-8")


def _env_text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_float(name: str, default: str) -> float:
    raw = _env_text(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_int(name: str, default: str | None = None) -> int | None:
    raw = _env_text(name, default)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool | None = False) -> bool | None:
    raw = _env_text(name)
    if raw in (None, ""):
        return default
    value = raw.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")

SYMBOL = "BTCUSDT"
# $2.50 is not a round number by accident: the venue minimum is 5 shares, and
# at the top of the phase-1 band (0.50) that is exactly $2.50. Anything less
# and the engine sizes the order up at the most expensive price in the band.
# It is also the largest size whose bad-case drawdown fits the paper balance.
BET_SIZE = _env_float("BET_SIZE", "2.50")
# Phase 2 owns the final stretch only; phase 1 owns everything before it.
TRADE_LAST_SECONDS = _env_int("TRADE_LAST_SECONDS", "120")
if TRADE_LAST_SECONDS is None:
    raise ValueError("TRADE_LAST_SECONDS cannot be empty")
TRADE_INTERVAL_SECONDS = _env_float("TRADE_INTERVAL_SECONDS", "6")
MAX_BUY_PRICE = _env_float("MAX_BUY_PRICE", "0.90")
MIN_BUY_PRICE = _env_float("MIN_BUY_PRICE", "0.20")
BTC_STALE_AFTER = _env_float("BTC_STALE_AFTER", "3.0")
ORDERBOOK_MAX_AGE_SECONDS = _env_float("ORDERBOOK_MAX_AGE_SECONDS", "8.0")
MAX_ALLOWED_SPREAD = _env_float("MAX_ALLOWED_SPREAD", "0.25")
CLOCK_MAX_DRIFT_SECONDS = _env_float("CLOCK_MAX_DRIFT_SECONDS", "2.0")
PAPER_LATENCY_MS = _env_float("PAPER_LATENCY_MS", "150")
TWAP_STALE_AFTER = _env_float("TWAP_STALE_AFTER", "10.0")
# No order inside the final minute. Measured over 16 fills: 31.2% won against
# a 69.6% break-even, z = -3.29 - and it has a mechanism, not just a p-value.
# In the last minute the book goes one-sided (the winning leg keeps only bids,
# the losing leg only asks), so the fills still available are the ones the
# market is content to sell. That is adverse selection, and no signal fixes it.
# Trading T-120..T-60 was +1.4 per $100 over the same period; the damage was
# entirely in the tail.
MIN_SECONDS_TO_EXPIRY = _env_float("MIN_SECONDS_TO_EXPIRY", "60.0")
# ---- phase 1: price band, with direction authorized by fresh SIG PRICE -----
# The band controls whether the selected contract is affordable.  Binance
# opening-to-current direction controls which outcome may be submitted; a
# neutral, stale, or opposite signal now fails closed.
PHASE1_ENABLED = bool(_env_bool("PHASE1_ENABLED", True))
PHASE1_INTERVAL_SECONDS = _env_float("PHASE1_INTERVAL_SECONDS", "12")
# Bands are per sub-window: "start:end:low:high[:interval]", seconds REMAINING
# in the round, comma separated, listed from the open toward expiry. Each band
# caps its own orders, so a thin top level can never fill outside the band
# being measured - one global cap cannot bound several ranges. The optional
# fifth field sets that window's own cadence; without it the band uses
# PHASE1_INTERVAL_SECONDS.
#
# The final band covers the same T-120..T-60 interval as phase 2. Measured
# across 89 observations it won 68.5% needing 66.8% (+3.7 per $100, z=+0.66)
# - fair value.  The band and Phase 2 retain different entry conditions, but
# every order side is now authorized by the same fresh Binance SIG PRICE.
PHASE1_BANDS_RAW = _env_text(
    "PHASE1_BANDS",
    "300:240:0.35:0.45,240:180:0.30:0.40,180:120:0.40:0.50,120:60:0.55:0.75:8")


def _parse_bands(raw: str):
    bands = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) not in (4, 5):
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} must be start:end:low:high[:interval]")
        try:
            start, end = int(parts[0]), int(parts[1])
            low, high = float(parts[2]), float(parts[3])
            interval = float(parts[4]) if len(parts) == 5 else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"PHASE1_BANDS entry {chunk!r} is not numeric") from exc
        if not 0 <= end < start <= 300:
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} needs 0 <= end < start <= 300")
        if not (math.isfinite(low) and math.isfinite(high) and 0 < low < high < 1):
            raise ValueError(f"PHASE1_BANDS entry {chunk!r} needs 0 < low < high < 1")
        if interval is not None and not (
                math.isfinite(interval) and 1 <= interval <= (start - end)):
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} needs a cadence of 1..{start - end}s")
        bands.append((start, end, low, high, interval))
    if not bands:
        raise ValueError("PHASE1_BANDS must list at least one window")
    bands.sort(key=lambda b: -b[0])
    for earlier, later in zip(bands, bands[1:]):
        if later[0] > earlier[1]:
            raise ValueError(
                f"PHASE1_BANDS windows overlap: {earlier[0]}:{earlier[1]} "
                f"and {later[0]}:{later[1]}")
    return tuple(bands)


PHASE1_BANDS = _parse_bands(PHASE1_BANDS_RAW)
PHASE1_START_SECONDS = PHASE1_BANDS[0][0]
PHASE1_END_SECONDS = PHASE1_BANDS[-1][1]
# Kept for callers that want the overall reach of phase 1 rather than a
# specific window's band.
PHASE1_MIN_PRICE = min(b[2] for b in PHASE1_BANDS)
PHASE1_MAX_PRICE = max(b[3] for b in PHASE1_BANDS)


def phase1_band(seconds_left: float):
    """The band governing this instant, or None outside every window.

    Returns (start, end, low, high, interval) with the cadence resolved, so
    callers never have to know whether a band set its own.
    """
    for start, end, low, high, interval in PHASE1_BANDS:
        if end < seconds_left <= start:
            return start, end, low, high, (
                PHASE1_INTERVAL_SECONDS if interval is None else interval)
    return None


# Phase 2 keeps book and Chainlink votes as diagnostics, while fresh Binance
# SIG PRICE is the sole order-side authority.  It remains off by default;
# enable it explicitly to run the experimental path.
PHASE2_ENABLED = bool(_env_bool("PHASE2_ENABLED", False))
# PAPER may deliberately follow a later, verified SIG PRICE reversal even
# after the first outcome token has filled.  LIVE keeps the complement-leg
# block unconditionally: two independent venue orders are not an atomic pair.
# Off by default so existing paper runs preserve their one-leg-per-round risk
# contract unless the experiment is selected explicitly.
PAPER_ALLOW_SIGNAL_FLIPS = bool(_env_bool("PAPER_ALLOW_SIGNAL_FLIPS", False))
if PAPER_ALLOW_SIGNAL_FLIPS and (PHASE1_ENABLED or not PHASE2_ENABLED):
    raise ValueError(
        "PAPER_ALLOW_SIGNAL_FLIPS requires PHASE1_ENABLED=0 and "
        "PHASE2_ENABLED=1 so band and signal cadences cannot overlap")

# Give SIG BOOK and SIG CHAINLINK their own orders instead of leaving them as
# diagnostics. Each non-neutral signal trades its own side, so a round where
# they disagree buys BOTH legs on purpose. Measured on 1,957 logged decisions
# that is 26.1% of them, and a simultaneous pair costs the overround: at the
# 1.0100 sum observed live, ~-$0.22 per $5 pair whichever way BTC settles.
# The complement guard exists to prevent exactly that, so this switch stands
# it down for signal-driven legs and cannot be combined with a lock that would
# refuse them. Off by default; PAPER only.
# How early the NEXT round's books are discovered and pre-subscribed. The
# websocket needs time to subscribe and receive a first snapshot before the
# boundary, or the opening seconds of the new round trade against an empty
# book. Raising this costs one extra gamma-api call per round, no more.
ROUND_PREPARE_LEAD_SECONDS = _env_float("ROUND_PREPARE_LEAD_SECONDS", "30")
if not 5.0 <= ROUND_PREPARE_LEAD_SECONDS <= 280.0:
    raise ValueError("ROUND_PREPARE_LEAD_SECONDS must be between 5 and 280")
# Rotation poll interval away from a boundary. Near one the loop polls every
# second regardless: the opening print may only be latched in the first 5s of
# a round, so a rotation that lands 6s late costs the entire round. Mid-round
# there is nothing to gain and gamma-api rate-limits, hence the slower default.
ROUND_POLL_SECONDS = _env_float("ROUND_POLL_SECONDS", "5")
if not 0.5 <= ROUND_POLL_SECONDS <= 30.0:
    raise ValueError("ROUND_POLL_SECONDS must be between 0.5 and 30")

# Phase 2 normally refuses to trade a round unless all four boundary inputs are
# present: both Binance values and both Chainlink values. But SIG PRICE alone
# owns the order side, and SIG CHAINLINK is either a diagnostic or - under
# PHASE2_MULTI_SIGNAL - a leg of its own that can simply abstain. Requiring its
# inputs to trade cancels rounds that SIG PRICE could have handled: one missed
# one-second TWAP observation kills five minutes of trading. With this on, only
# missing BINANCE inputs cancel the round; Chainlink abstains like SIG BOOK
# already does on a one-sided book. Off by default so the stricter original
# contract is what you get unless the looser one is chosen deliberately.
PHASE2_PARTIAL_SIGNALS = bool(_env_bool("PHASE2_PARTIAL_SIGNALS", False))

PHASE2_MULTI_SIGNAL = bool(_env_bool("PHASE2_MULTI_SIGNAL", False))
if PHASE2_MULTI_SIGNAL and not PHASE2_ENABLED:
    raise ValueError("PHASE2_MULTI_SIGNAL requires PHASE2_ENABLED=1")
if PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS:
    raise ValueError(
        "PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS both relax the "
        "complement guard by different rules; enable exactly one")

# The order path refuses any submission outside the round's execution
# interval. That interval has to reach back to the earliest second ANY enabled
# phase can trade: sizing it from TRADE_LAST_SECONDS alone silently refuses
# every phase-1 order as "outside the current round execution interval",
# because phase 1 runs entirely before phase 2's window opens.
EXECUTION_WINDOW_SECONDS = max(
    TRADE_LAST_SECONDS,
    PHASE1_START_SECONDS if PHASE1_ENABLED else 0,
)


# Polymarket takes shares * theta * p * (1-p) from a taker. For a fixed
# notional that is at most notional * theta, which is all a cap needs.
TAKER_FEE_RATE = 0.07
VENUE_MIN_SHARES = 5.0

# ---- paired-leg profit lock ------------------------------------------------
# Holding both legs of one market redeems for exactly $1.00 per matched pair at
# settlement, whichever way BTC goes. Buying the complement is therefore worth
# doing only when both entry prices plus both fees come to less than $1.00;
# above that the pair is a guaranteed loss, which is what the complement guard
# normally exists to prevent. Off by default: it deliberately relaxes that
# guard, so it has to be switched on knowingly.
PAIR_LOCK_ENABLED = bool(_env_bool("PAIR_LOCK_ENABLED", False))
# Headroom held back from $1.00. A quote is not a fill: the book can move a
# tick between the check and the FOK, the venue can change theta, and the
# broker rounds up to VENUE_MIN_SHARES. Without a margin, a pair measured at
# exactly break-even settles as a small loss.
PAIR_LOCK_MIN_EDGE = _env_float("PAIR_LOCK_MIN_EDGE", "0.02")
if not math.isfinite(PAIR_LOCK_MIN_EDGE) or not 0.0 <= PAIR_LOCK_MIN_EDGE < 1.0:
    raise ValueError("PAIR_LOCK_MIN_EDGE must be in [0, 1)")


def pair_lock_permits(entry_price, entry_fee_per_share,
                      ask) -> tuple[bool, float]:
    """Would buying the complement at ``ask`` lock a profit on the pair?

    Returns ``(permitted, locked_per_pair)``. The fee already paid on the held
    leg is counted deliberately: this answers "is the finished round position
    profitable", not "is this marginal order cheap". Sunk-cost reasoning would
    let the bot complete a pair that still loses money overall, and the whole
    point of the lock is that the outcome stops depending on BTC.
    """
    try:
        p1 = float(entry_price)
        f1 = float(entry_fee_per_share)
        p2 = float(ask)
    except (TypeError, ValueError):
        return False, 0.0
    if not all(math.isfinite(v) for v in (p1, f1, p2)):
        return False, 0.0
    if not 0.0 < p1 < 1.0 or not 0.0 < p2 < 1.0 or f1 < 0.0:
        return False, 0.0
    f2 = TAKER_FEE_RATE * p2 * (1.0 - p2)
    locked = 1.0 - (p1 + f1 + p2 + f2)
    return locked >= PAIR_LOCK_MIN_EDGE, locked


def entry_cost_ceiling(cap_price: float) -> float:
    """The most one entry can take out of the account at this price cap.

    BUGFIX: main_bot used to charge MAX_ROUND_EXPOSURE exactly BET_SIZE per
    entry. The broker sizes UP to the venue's 5-share minimum, so any fill
    above BET_SIZE/5 costs more than BET_SIZE and the fee is on top. Measured
    on a real paper run the tracker was 22% low overall and 76% low on one
    round, which made the cap nominal rather than real. A limit has to use an
    upper bound, so this returns one.
    """
    notional = max(float(BET_SIZE), VENUE_MIN_SHARES * float(cap_price))
    return notional * (1.0 + TAKER_FEE_RATE)


def _round_entry_budget() -> float:
    """Worst-case CASH for one round, counting only the phases switched on.

    Deriving it from the phases themselves means parking phase 2 lowers the
    cap automatically, instead of leaving a ceiling sized for a path that no
    longer runs. Each band is budgeted at its own ceiling price, so a band
    priced above BET_SIZE/5 gets the room it will actually need.
    """
    budget = 0.0
    if PHASE1_ENABLED:
        # Each band may set its own cadence, so budget them individually.
        for start, end, _lo, hi, interval in PHASE1_BANDS:
            gap = PHASE1_INTERVAL_SECONDS if interval is None else interval
            entries = math.ceil((start - end) / max(gap, 1))
            budget += entries * entry_cost_ceiling(hi)
    if PHASE2_ENABLED:
        # Phase 2 stops at MIN_SECONDS_TO_EXPIRY, so its budget is the window
        # it can actually reach, not the whole tail of the round.
        entries = math.ceil(
            max(0.0, TRADE_LAST_SECONDS - MIN_SECONDS_TO_EXPIRY)
            / max(TRADE_INTERVAL_SECONDS, 1))
        budget += entries * entry_cost_ceiling(MAX_BUY_PRICE)
    return budget if budget > 0 else entry_cost_ceiling(MAX_BUY_PRICE)


MAX_ROUND_EXPOSURE = _env_float("MAX_ROUND_EXPOSURE", str(_round_entry_budget()))

CANCEL_OPEN_BEFORE_TRADE = bool(_env_bool("CANCEL_OPEN_BEFORE_TRADE", False))
ALLOW_GLOBAL_CANCEL_ALL = bool(_env_bool("ALLOW_GLOBAL_CANCEL_ALL", False))
ALLOW_CUSTOM_CLOB_HOST = bool(_env_bool("ALLOW_CUSTOM_CLOB_HOST", False))
CLOB_HOST = _env_text("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = 137
_VALID_TICKS = ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
TICK_SIZE = _env_text("TICK_SIZE") or None
if TICK_SIZE is not None and TICK_SIZE not in _VALID_TICKS:
    raise ValueError(f"TICK_SIZE must be one of {_VALID_TICKS}, got {TICK_SIZE!r}")
NEG_RISK = _env_bool("NEG_RISK", None)
UP_TOKEN_ID = _env_text("UP_TOKEN_ID") or None
DOWN_TOKEN_ID = _env_text("DOWN_TOKEN_ID") or None
ORDERBOOK_TOKEN_ID = _env_text("ORDERBOOK_TOKEN_ID") or None
POLY_FUNDER = _env_text("POLY_FUNDER") or None
POLY_SIGNATURE_TYPE = _env_int("POLY_SIGNATURE_TYPE")


def _finite_positive(name, value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")


_finite_positive("BET_SIZE", BET_SIZE)
if Decimal(str(BET_SIZE)).quantize(Decimal("0.01")) != Decimal(str(BET_SIZE)):
    raise ValueError("BET_SIZE must be expressed in whole pUSD cents")
if not 1 <= TRADE_LAST_SECONDS <= 300:
    raise ValueError("TRADE_LAST_SECONDS must be between 1 and 300")
if (not math.isfinite(TRADE_INTERVAL_SECONDS)
        or not 1 <= TRADE_INTERVAL_SECONDS <= TRADE_LAST_SECONDS):
    raise ValueError("TRADE_INTERVAL_SECONDS must be in [1, TRADE_LAST_SECONDS]")
if not math.isfinite(MAX_BUY_PRICE) or not 0 < MAX_BUY_PRICE < 1:
    raise ValueError("MAX_BUY_PRICE must be strictly between 0 and 1")
if not math.isfinite(MIN_BUY_PRICE) or not 0 < MIN_BUY_PRICE < 1:
    raise ValueError("MIN_BUY_PRICE must be strictly between 0 and 1")
if not MIN_BUY_PRICE < MAX_BUY_PRICE:
    raise ValueError("MIN_BUY_PRICE must be below MAX_BUY_PRICE")
# Window and band shapes are validated in _parse_bands; what is left is the
# cadence fitting the tightest window, and the stake clearing the venue
# minimum at the most expensive price any band can reach.
# Bands carrying their own cadence are validated in _parse_bands; the default
# only has to fit the narrowest window that relies on it.
_default_users = [b for b in PHASE1_BANDS if b[4] is None]
_narrowest = min((start - end for start, end, _l, _h, _i in _default_users),
                 default=300)
if (not math.isfinite(PHASE1_INTERVAL_SECONDS)
        or not 1 <= PHASE1_INTERVAL_SECONDS <= _narrowest):
    raise ValueError(
        f"PHASE1_INTERVAL_SECONDS must fit the narrowest band window ({_narrowest}s)")
# The venue minimum is 5 shares, so a band whose prices exceed BET_SIZE/5
# forces the engine to size the order up. That is legitimate - it is what the
# venue requires - but it must never be silent, because the stake then varies
# with price and a per-$100 comparison across bands stops being like-for-like.
PHASE1_STAKE_NOTES = tuple(
    (start, end, low, high, round(5 * low, 2), round(5 * high, 2))
    for start, end, low, high, _i in PHASE1_BANDS
    if 5 * high > BET_SIZE + 1e-9
)
if PHASE1_ENABLED and BET_SIZE < 5 * PHASE1_MIN_PRICE - 1e-9:
    raise ValueError(
        f"BET_SIZE {BET_SIZE} cannot buy the 5-share venue minimum anywhere in "
        f"the cheapest band ({PHASE1_MIN_PRICE}); raise it to "
        f"{5 * PHASE1_MIN_PRICE:.2f}")
_finite_positive("BTC_STALE_AFTER", BTC_STALE_AFTER)
_finite_positive("ORDERBOOK_MAX_AGE_SECONDS", ORDERBOOK_MAX_AGE_SECONDS)
if not math.isfinite(MAX_ALLOWED_SPREAD) or not 0 < MAX_ALLOWED_SPREAD <= 1:
    raise ValueError("MAX_ALLOWED_SPREAD must be in (0, 1]")
_finite_positive("CLOCK_MAX_DRIFT_SECONDS", CLOCK_MAX_DRIFT_SECONDS)
if not math.isfinite(PAPER_LATENCY_MS) or PAPER_LATENCY_MS < 0:
    raise ValueError("PAPER_LATENCY_MS must be finite and non-negative")
_finite_positive("TWAP_STALE_AFTER", TWAP_STALE_AFTER)
if (not math.isfinite(MIN_SECONDS_TO_EXPIRY)
        or not 0 <= MIN_SECONDS_TO_EXPIRY < TRADE_LAST_SECONDS):
    raise ValueError("MIN_SECONDS_TO_EXPIRY must be non-negative and below TRADE_LAST_SECONDS")
if not math.isfinite(MAX_ROUND_EXPOSURE) or MAX_ROUND_EXPOSURE < BET_SIZE:
    raise ValueError("MAX_ROUND_EXPOSURE must be finite and at least BET_SIZE")
if POLY_SIGNATURE_TYPE not in (None, 0, 1, 2, 3):
    raise ValueError("POLY_SIGNATURE_TYPE must be 0, 1, 2, or 3")
# py-clob-client-v2's L1 API-key derivation does not currently bind a type-3
# (POLY_1271 deposit-wallet) key to the funder. Upstream issue #70 remains
# open; allowing it here produces orders that the CLOB rejects as the wrong
# signer. Fail before any credential or order request instead.
if POLY_SIGNATURE_TYPE == 3:
    raise ValueError(
        "POLY_SIGNATURE_TYPE=3 is blocked: py-clob-client-v2 cannot currently "
        "derive a funder-bound POLY_1271 API key (upstream issue #70)"
    )
if POLY_SIGNATURE_TYPE in (1, 2, 3) and not POLY_FUNDER:
    raise ValueError("proxy signature types 1/2/3 require POLY_FUNDER")
if POLY_FUNDER and POLY_SIGNATURE_TYPE is None:
    raise ValueError("POLY_FUNDER requires an explicit POLY_SIGNATURE_TYPE")
if POLY_FUNDER and not re.fullmatch(r"0x[0-9A-Fa-f]{40}", POLY_FUNDER):
    raise ValueError("POLY_FUNDER must be a 20-byte 0x-prefixed Ethereum address")
for _name, _token in (
        ("UP_TOKEN_ID", UP_TOKEN_ID),
        ("DOWN_TOKEN_ID", DOWN_TOKEN_ID),
        ("ORDERBOOK_TOKEN_ID", ORDERBOOK_TOKEN_ID)):
    if _token is not None and (not re.fullmatch(r"[0-9]{1,78}", _token)
                               or int(_token) <= 0):
        raise ValueError(f"{_name} must be a positive decimal uint256 token id")
if CANCEL_OPEN_BEFORE_TRADE and not ALLOW_GLOBAL_CANCEL_ALL:
    raise ValueError(
        "CANCEL_OPEN_BEFORE_TRADE uses the wallet-wide cancel-all endpoint; "
        "set ALLOW_GLOBAL_CANCEL_ALL=1 only for a dedicated bot wallet"
    )
try:
    _clob_url = urlsplit(CLOB_HOST)
    _clob_port = _clob_url.port
except (TypeError, ValueError) as exc:
    raise ValueError("CLOB_HOST must be a valid HTTPS origin") from exc
if (_clob_url.scheme.lower() != "https" or not _clob_url.hostname
        or _clob_url.username is not None or _clob_url.password is not None
        or _clob_url.query or _clob_url.fragment
        or _clob_url.path not in ("", "/")):
    raise ValueError(
        "CLOB_HOST must be an HTTPS origin without credentials, path, query, or fragment"
    )
_official_clob = (
    _clob_url.hostname.lower() == "clob.polymarket.com"
    and _clob_port in (None, 443)
)
if not _official_clob and not ALLOW_CUSTOM_CLOB_HOST:
    raise ValueError(
        "custom CLOB_HOST is blocked because it receives authenticated requests; "
        "set ALLOW_CUSTOM_CLOB_HOST=1 only for an endpoint you control"
    )
# Store an origin without a trailing slash so every SDK endpoint is joined in a
# single, predictable way.
CLOB_HOST = f"https://{_clob_url.hostname}"
if _clob_port not in (None, 443):
    CLOB_HOST += f":{_clob_port}"
