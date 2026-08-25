"""Polymarket CLOB V2 live execution with strict FOK acknowledgement rules."""
import math
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from urllib.parse import urlsplit

import config  # noqa: F401 — loads .env first
import orderbook

from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

_client = None
_live_disabled = False
_execution_lock = threading.Lock()
_state_lock = threading.RLock()
_order_observer = None
_journal_fault = None
_market_fee_by_token: dict[str, tuple[float, int]] = {}
_MAX_FEE_CACHE_TOKENS = 4096
_ambiguous_condition = None
_ambiguous_until = 0.0
last_order_error = None
last_order_status = None
last_order_receipt = None
PUSD_BASE_UNITS = Decimal("1000000")
CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_SECRET_RE = re.compile(r"0x[0-9a-fA-F]{64}")
FEE_PRECISION = Decimal("0.00001")


def _safe_error(exc) -> str:
    text = str(exc or "")
    for name in (
        "POLY_PRIVATE_KEY", "PRIVATE_KEY", "POLY_API_KEY", "CLOB_API_KEY",
        "POLY_SECRET", "CLOB_SECRET", "POLY_PASSPHRASE",
        "CLOB_PASSPHRASE", "CLOB_PASS_PHRASE",
    ):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "<redacted>")
    return HEX_SECRET_RE.sub("0x<redacted>", text)[:300]


def _pusd_amount(value) -> float | None:
    """Convert six-decimal base units; malformed values stay unknown."""
    try:
        out = Decimal(str(value)) / PUSD_BASE_UNITS
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not out.is_finite() or out < 0:
        return None
    return float(out)


def disable_live_execution() -> None:
    """Irreversible process-local paper-mode firewall."""
    global _live_disabled, _client
    with _execution_lock:
        _live_disabled = True
        _client = None


def live_execution_disabled() -> bool:
    return _live_disabled


def set_order_observer(callback) -> None:
    """Install the live accounting journal callback (never used in paper)."""
    global _order_observer
    with _state_lock:
        _order_observer = callback


def market_fee_rate(token_id) -> float | None:
    with _state_lock:
        params = _market_fee_by_token.get(str(token_id))
    return None if params is None else params[0]


def market_fee_parameters(token_id) -> dict | None:
    with _state_lock:
        params = _market_fee_by_token.get(str(token_id))
    return None if params is None else {"rate": params[0], "exponent": params[1]}


def reset_client():
    global _client
    with _execution_lock:
        _client = None


def _validate_live_host(host: str) -> str:
    value = str(host or "").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise RuntimeError("authenticated CLOB host must be a clean HTTPS origin")
    return value


def _normalize_private_key(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().strip('"').strip("'").replace(" ", "").replace("\n", "")
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", key):
        raise RuntimeError("live private key must be exactly 32 bytes of hexadecimal")
    return key


def _get_client() -> ClobClient:
    global _client
    if _live_disabled:
        raise RuntimeError("live CLOB client is disabled by paper mode")
    if _client is not None:
        return _client

    key = _normalize_private_key(os.environ.get("POLY_PRIVATE_KEY") or
                                 os.environ.get("PRIVATE_KEY"))
    if not key:
        raise RuntimeError("live trading requires POLY_PRIVATE_KEY or PRIVATE_KEY")
    funder = config.POLY_FUNDER
    if funder and not re.fullmatch(r"0x[0-9a-fA-F]{40}", str(funder)):
        raise RuntimeError("POLY_FUNDER is not a valid EVM address")

    client_kwargs = {
        "host": _validate_live_host(config.CLOB_HOST),
        "chain_id": config.CHAIN_ID,
        "key": key,
        "use_server_time": True,
    }
    # Preserve the SDK's EOA defaults instead of overriding them with None.
    if config.POLY_SIGNATURE_TYPE is not None:
        client_kwargs["signature_type"] = config.POLY_SIGNATURE_TYPE
    if funder:
        client_kwargs["funder"] = funder
    client = ClobClient(**client_kwargs)
    try:
        creds = client.create_or_derive_api_key()
        if creds is None:
            raise RuntimeError("credential derivation returned no credentials")
        client.set_api_creds(creds)
    except Exception as exc:
        raise RuntimeError(f"could not derive L2 API credentials: {_safe_error(exc)}") from None

    _client = client
    print("[LIVE] CLOB V2 client initialized (wallet identifiers redacted).")
    return _client


def _balance_from_response(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    balance = _pusd_amount(raw.get("balance"))
    allowance_raw = raw.get("allowance")
    if allowance_raw is not None:
        allowance = _pusd_amount(allowance_raw)
    elif isinstance(raw.get("allowances"), dict):
        vals = [_pusd_amount(v) for v in raw["allowances"].values()]
        # A maximum hides a zero approval on one required exchange.  The
        # conservative minimum is the only safe aggregate.
        allowance = min(vals) if vals and all(v is not None for v in vals) else None
    else:
        allowance = None
    if balance is None or allowance is None:
        return None
    return {"balance": balance, "allowance": allowance}


def _read_balance(client) -> dict | None:
    raw = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return _balance_from_response(raw)


def get_balance_allowance() -> dict | None:
    global last_order_error
    if not _execution_lock.acquire(timeout=10.0):
        last_order_error = "balance read timed out behind another live API action"
        return None
    try:
        result = _read_balance(_get_client())
        if result is None:
            raise RuntimeError("malformed balance/allowance response")
        return result
    except Exception as exc:
        last_order_error = _safe_error(exc)
        print(f"[LIVE] Balance/allowance check failed: {last_order_error}")
        return None
    finally:
        _execution_lock.release()


def cancel_all_open_orders() -> bool:
    global last_order_error
    if not _execution_lock.acquire(blocking=False):
        last_order_error = "cannot cancel while another live API action is in flight"
        return False
    try:
        return _cancel_all_open_orders()
    finally:
        _execution_lock.release()


def _cancel_all_open_orders() -> bool:
    global last_order_error
    try:
        resp = _get_client().cancel_all()
        if not isinstance(resp, dict):
            raise RuntimeError("malformed cancel-all response")
        canceled = resp.get("canceled", resp.get("cancelled"))
        not_canceled = resp.get("not_canceled", resp.get("notCancelled"))
        if not isinstance(canceled, list) or not isinstance(not_canceled, dict):
            raise RuntimeError("cancel-all response is missing result fields")
        if not_canceled:
            raise RuntimeError(f"venue could not cancel {len(not_canceled)} order(s)")
        last_order_error = None
        print(f"[LIVE] Cancelled {len(canceled)} open order(s).")
        return True
    except Exception as exc:
        last_order_error = _safe_error(exc)
        print(f"[LIVE] Cancel all error: {last_order_error}")
        return False


def _is_no_match(err: str) -> bool:
    e = (err or "").lower()
    return ("no match" in e or "couldn't be fully filled" in e
            or "fully filled or killed" in e or "status unmatched" in e
            or "fok_order_not_filled" in e)


_FAILED_ORDER_STATUSES = {
    "failed", "rejected", "cancelled", "canceled", "unmatched", "error",
}


def _accepted_order_response(resp) -> tuple[str | None, str | None, str | None]:
    """Accept only a documented FOK ``matched`` acknowledgement.

    This is placement evidence, not final position evidence.  The ledger still
    waits for the user channel/REST trade to reach terminal ``CONFIRMED``.
    """
    if not isinstance(resp, dict):
        return None, None, "unaccepted non-object order response"
    oid = resp.get("orderID") or resp.get("order_id")
    status = str(resp.get("status") or "").strip()
    status_l = status.lower()
    error = resp.get("errorMsg") or resp.get("error")
    if resp.get("success") is False or resp.get("ok") is False:
        return None, status or None, str(error or resp.get("message") or "venue rejected order")
    if error not in (None, ""):
        return None, status or None, str(error)
    if status_l in _FAILED_ORDER_STATUSES:
        return None, status or None, f"FOK order status {status or 'missing'}"
    if status_l != "matched":
        return None, status or None, f"FOK response is not matched (status={status or 'missing'})"
    if resp.get("success") is not True and resp.get("ok") is not True:
        return None, status, "FOK response has no explicit success flag"
    if not oid:
        return None, status, "matched FOK response has no order id"
    if not _valid_response_id(oid):
        return None, status, "matched FOK response has an invalid order id"
    trades = resp.get("tradeIDs") or resp.get("trade_ids") or []
    transactions = resp.get("transactionsHashes") or resp.get("transaction_hashes") or []
    if not isinstance(trades, list) or not isinstance(transactions, list):
        return None, status, "matched FOK response has malformed trade evidence"
    if not trades and not transactions:
        return None, status, "matched FOK response has no trade ids or transaction hashes"
    if (any(not _valid_response_id(value) for value in trades + transactions)
            or len(set(map(str, trades))) != len(trades)
            or len(set(map(str, transactions))) != len(transactions)):
        return None, status, "matched FOK response has invalid trade evidence"
    if not _valid_execution_amounts(resp):
        return None, status, "matched FOK response omitted valid execution amounts"
    return str(oid), status, None


def _accepted_pending_response(resp) -> tuple[str | None, str | None, str | None]:
    """Recognize a successful placement that is not yet validated as matched.

    ``delayed`` is an official successful order status. It must never be
    counted as a fill, but its order ID must be journaled so a later CONFIRMED
    user-stream trade can enter accounting. Other explicit-success responses
    with an order ID are retained as unverified rather than mislabeled failed.
    """
    if not isinstance(resp, dict):
        return None, None, None
    if resp.get("success") is not True and resp.get("ok") is not True:
        return None, None, None
    error = resp.get("errorMsg") or resp.get("error")
    if error not in (None, ""):
        return None, None, None
    oid = resp.get("orderID") or resp.get("order_id")
    status = str(resp.get("status") or "").strip()
    if (not _valid_response_id(oid) or not _valid_execution_amounts(resp)
            or status.lower() in _FAILED_ORDER_STATUSES):
        return None, status or None, None
    return str(oid), status or None, (
        "DELAYED_PENDING_OUTCOME" if status.lower() == "delayed" else
        "UNEXPECTED_LIVE_PENDING" if status.lower() == "live" else
        "MATCHED_UNVERIFIED_PENDING_CONFIRMATION" if status.lower() == "matched" else
        "ACCEPTED_UNVERIFIED_PENDING"
    )


def _definitive_rejection(resp) -> bool:
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("status") or "").strip().lower()
    return (resp.get("success") is False or resp.get("ok") is False
            or status in _FAILED_ORDER_STATUSES)


def _valid_response_id(value) -> bool:
    text = str(value or "")
    return (0 < len(text) <= 256 and text.isprintable()
            and not any(char.isspace() for char in text))


def _valid_execution_amounts(resp: dict) -> bool:
    try:
        making = Decimal(str(resp["makingAmount"]))
        taking = Decimal(str(resp["takingAmount"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False
    return (making.is_finite() and taking.is_finite()
            and making > 0 and taking > 0
            and making == making.to_integral_value()
            and taking == taking.to_integral_value())


def _build_receipt(resp: dict, oid: str, status: str | None, *, condition_id,
                   token_id, window_end, amount, estimated_fee,
                   fee_rate, fee_exponent, validation: str) -> dict:
    return {
        "order_id": oid,
        "status": status,
        "validation": validation,
        "trade_ids": list(resp.get("tradeIDs") or resp.get("trade_ids") or []),
        "transaction_hashes": list(
            resp.get("transactionsHashes") or resp.get("transaction_hashes") or []),
        "making_amount_base_units": str(resp.get("makingAmount")),
        "taking_amount_base_units": str(resp.get("takingAmount")),
        "condition_id": str(condition_id),
        "token_id": token_id,
        "window_end": window_end,
        "requested_notional": amount,
        "estimated_fee": float(estimated_fee),
        "fee_rate": float(fee_rate),
        "fee_exponent": int(fee_exponent),
    }


def _validate_market_mapping(info, condition_id, up_token_id, down_token_id):
    if not isinstance(info, dict):
        raise RuntimeError("CLOB market-info response is not an object")
    returned = str(info.get("condition_id") or info.get("conditionId") or "")
    # The current CLOB V2 response is keyed by condition in the URL but does
    # not echo it.  If a future response does echo it, verify it.
    if returned and returned != str(condition_id):
        raise RuntimeError("CLOB market-info condition id mismatch")
    rows = info.get("t")
    if not isinstance(rows, list) or len(rows) != 2:
        raise RuntimeError("CLOB market is not binary")
    labelled = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("malformed CLOB token mapping")
        labelled[str(row.get("o") or "").strip().lower()] = str(row.get("t") or "")
    if (set(labelled) != {"up", "down"} or not all(_valid_token(v) for v in labelled.values())
            or labelled["up"] == labelled["down"]):
        raise RuntimeError("CLOB outcomes are not exactly UP and DOWN")
    if (labelled["up"] != str(up_token_id)
            or labelled["down"] != str(down_token_id)):
        raise RuntimeError("Gamma and CLOB disagree on UP/DOWN token mapping")
    try:
        minimum = Decimal(str(info.get("mos")))
        tick = Decimal(str(info.get("mts")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("CLOB market has invalid minimum-size/tick rules") from exc
    if (not minimum.is_finite() or minimum <= 0 or not tick.is_finite()
            or tick <= 0 or tick >= 1):
        raise RuntimeError("CLOB market has invalid minimum-size/tick rules")
    fd = info.get("fd")
    if not isinstance(fd, dict):
        raise RuntimeError("CLOB market omitted fee details")
    try:
        fee_rate = Decimal(str(fd.get("r")))
        exponent_raw = fd.get("e")
        if isinstance(exponent_raw, bool):
            raise ValueError
        exponent_decimal = Decimal(str(exponent_raw))
        if (not exponent_decimal.is_finite()
                or exponent_decimal != exponent_decimal.to_integral_value()):
            raise ValueError
        fee_exponent = int(exponent_decimal)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("CLOB market has invalid fee details") from exc
    if (not fee_rate.is_finite() or not 0 < fee_rate <= 1
            or not 1 <= fee_exponent <= 8):
        raise RuntimeError("CLOB market has invalid fee details")
    if fd.get("to") is not True:
        raise RuntimeError("CLOB market fee details are not explicitly taker-only")
    neg_risk = info.get("nr", False)
    taker_delay = info.get("itode", False)
    if not isinstance(neg_risk, bool) or not isinstance(taker_delay, bool):
        raise RuntimeError("CLOB market has invalid boolean execution rules")
    if taker_delay and config.ASSUMED_MATCH_DELAY_SECONDS <= 0:
        # This endpoint exposes only that a delay is enabled, not its duration.
        # Submitting near a hard expiry cutoff without the duration would guess.
        # Set ASSUMED_MATCH_DELAY_SECONDS to trade anyway, outside a window of
        # that many seconds from the round end; the caller enforces it, because
        # only the caller knows how much of the round is left.
        raise RuntimeError("CLOB market has an undisclosed matching delay")
    fee_params = (float(fee_rate), fee_exponent)
    with _state_lock:
        _market_fee_by_token[labelled["up"]] = fee_params
        _market_fee_by_token[labelled["down"]] = fee_params
        while len(_market_fee_by_token) > _MAX_FEE_CACHE_TOKENS:
            _market_fee_by_token.pop(next(iter(_market_fee_by_token)))
    return {
        "taker_delay": bool(taker_delay),
        "minimum": minimum,
        "tick": tick,
        "neg_risk": neg_risk,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "taker_delay": taker_delay,
    }


def _valid_token(token) -> bool:
    text = str(token or "")
    return text.isdigit() and int(text) > 0


def _round_limit(max_price, tick: Decimal) -> Decimal:
    cap = min(Decimal(str(max_price)), Decimal(1) - tick)
    rounded = (cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if rounded <= 0 or rounded >= 1:
        raise RuntimeError("MAX_BUY_PRICE cannot be represented at the venue tick size")
    return rounded


def _round_floor(min_price, tick: Decimal) -> Decimal:
    floor = max(Decimal(str(min_price)), tick)
    rounded = (floor / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    if rounded <= 0 or rounded >= 1:
        raise RuntimeError("MIN_BUY_PRICE cannot be represented at the venue tick size")
    return rounded


def _quote_fok(asks, amount: Decimal, limit: Decimal,
               fee_rate: Decimal, fee_exponent: int) -> tuple[Decimal, Decimal]:
    """Return (shares, fee) for the same best-to-worst FOK walk as paper."""
    remaining = amount
    shares = Decimal(0)
    fee = Decimal(0)
    for level in asks:
        price = Decimal(str(level["price"]))
        available_shares = Decimal(str(level["size"]))
        if price > limit:
            break
        spend = min(remaining, price * available_shares)
        if spend <= 0:
            continue
        level_shares = spend / price
        shares += level_shares
        fee += level_shares * fee_rate * (price * (Decimal(1) - price)) ** fee_exponent
        remaining -= spend
        if remaining <= Decimal("0.0000005"):
            remaining = Decimal(0)
            break
    if remaining > 0:
        raise RuntimeError(
            f"FOK preflight has only ${amount - remaining:.6f} executable liquidity")
    return shares, fee.quantize(FEE_PRECISION, rounding=ROUND_HALF_UP)


def _validate_round_end(window_end) -> float:
    try:
        end = float(window_end)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("missing current round end timestamp") from exc
    now = time.time()
    if (int(end) % 300 != 0
            or not end - config.EXECUTION_WINDOW_SECONDS <= now
            or not now < end - config.MIN_SECONDS_TO_EXPIRY):
        raise RuntimeError("order is outside the current round execution interval")
    return end


def _mark_ambiguous(condition_id: str, window_end: float) -> None:
    global _ambiguous_condition, _ambiguous_until
    _ambiguous_condition = str(condition_id)
    _ambiguous_until = float(window_end)


def _journal_receipt(receipt: dict) -> bool:
    with _state_lock:
        callback = _order_observer
    if callback is None:
        return False
    try:
        return callback(receipt) is not False
    except Exception as exc:
        print(f"[LIVE] CRITICAL: matched-order accounting journal failed: {type(exc).__name__}")
        return False


def _pre_submit_guard_error(pre_submit_guard) -> str | None:
    """Run an optional last-moment execution guard and fail closed.

    The callback is intentionally argument-free: callers bind the exact side,
    round, and observation they authorized in a closure.  Only the literal
    boolean ``True`` permits submission; truthy objects or callback failures
    are unsafe at an irreversible order boundary.
    """
    if pre_submit_guard is None:
        return None
    if not callable(pre_submit_guard):
        return "pre-submit guard is not callable"
    try:
        allowed = pre_submit_guard()
    except Exception as exc:
        return f"pre-submit guard failed closed: {type(exc).__name__}: {_safe_error(exc)}"
    if allowed is not True:
        return "pre-submit guard rejected order"
    return None


def place_trade(side: str, amount: float, up_token_id: str | None = None,
                down_token_id: str | None = None,
                condition_id: str | None = None,
                window_end: float | None = None,
                max_price: float | None = None, *, pre_submit_guard=None) -> bool:
    """Serialize live submissions so two callers cannot duplicate an entry.

    `max_price` caps this order alone, and may only tighten MAX_BUY_PRICE.
    `pre_submit_guard`, when supplied, must return literal ``True`` immediately
    before each signing/POST boundary.  It may reject, but never changes side.
    """
    global last_order_error
    if not _execution_lock.acquire(blocking=False):
        last_order_error = "another live order is already in flight"
        return False
    try:
        return _place_trade(side, amount, up_token_id, down_token_id,
                            condition_id, window_end, max_price,
                            pre_submit_guard=pre_submit_guard)
    finally:
        _execution_lock.release()


def _place_trade(side: str, amount: float, up_token_id: str | None = None,
                 down_token_id: str | None = None,
                 condition_id: str | None = None,
                 window_end: float | None = None,
                 max_price: float | None = None, *, pre_submit_guard=None) -> bool:
    global last_order_error, last_order_status, last_order_receipt, _journal_fault
    last_order_error = None
    last_order_status = None
    last_order_receipt = None
    if _live_disabled:
        last_order_error = "live execution is disabled by paper mode"
        return False
    if _journal_fault:
        last_order_error = _journal_fault
        return False
    with _state_lock:
        observer_missing = _order_observer is None
    if observer_missing:
        last_order_error = (
            "durable live order observer is not installed; refusing submission")
        return False
    side = str(side or "").upper()
    if side not in ("UP", "DOWN"):
        last_order_error = f"invalid side {side}"
        return False
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        last_order_error = "invalid order amount"
        return False
    if not math.isfinite(amount) or amount <= 0:
        last_order_error = "order amount must be finite and positive"
        return False
    if (not _valid_token(up_token_id) or not _valid_token(down_token_id)
            or str(up_token_id) == str(down_token_id)):
        last_order_error = "missing or invalid UP/DOWN token ids"
        return False
    if not condition_id or not CONDITION_RE.fullmatch(str(condition_id)):
        last_order_error = "missing or invalid current condition id"
        return False
    token_id = str(up_token_id if side == "UP" else down_token_id)

    try:
        end = _validate_round_end(window_end)
        if (_ambiguous_condition == str(condition_id)
                and time.time() < _ambiguous_until):
            raise RuntimeError(
                "prior submission has an ambiguous result; this round is blocked to prevent duplicates")
        client = _get_client()
        rules = _validate_market_mapping(
            client.get_clob_market_info(str(condition_id)), condition_id,
            up_token_id, down_token_id)
        if rules.get("taker_delay"):
            # The venue will not state the delay, so treat it as at most the
            # assumed value and refuse inside that window - an order sent later
            # than this could match after the round has already resolved, which
            # is the whole reason the flag is dangerous. Guessing LOW is the
            # unsafe direction: if the real delay exceeds the assumption, an
            # order accepted just outside the window still lands too late.
            assumed = float(config.ASSUMED_MATCH_DELAY_SECONDS)
            remaining = end - time.time()
            if remaining <= assumed:
                raise RuntimeError(
                    f"undisclosed matching delay: {remaining:.1f}s left is "
                    f"inside the assumed {assumed:.1f}s delay window")
        if config.TICK_SIZE is not None and Decimal(str(config.TICK_SIZE)) != rules["tick"]:
            raise RuntimeError("configured TICK_SIZE disagrees with the current market")
        if config.NEG_RISK is not None and bool(config.NEG_RISK) != rules["neg_risk"]:
            raise RuntimeError("configured NEG_RISK disagrees with the current market")
        ceiling = config.MAX_BUY_PRICE
        if max_price is not None:
            # Tighten only: an order cap can never raise the account ceiling.
            ceiling = min(ceiling, float(max_price))
        limit = _round_limit(ceiling, rules["tick"])
        floor = _round_floor(config.MIN_BUY_PRICE, rules["tick"])
        if floor > limit:
            raise RuntimeError(
                "the effective price floor is above the order's price cap")
        _bids, asks = orderbook.validate_buy_liquidity(
            token_id, amount, float(limit), config.MAX_ALLOWED_SPREAD,
            min_price=float(floor))
        shares, estimated_fee = _quote_fok(
            asks, Decimal(str(amount)), limit,
            rules["fee_rate"], rules["fee_exponent"])
        if shares < rules["minimum"]:
            raise RuntimeError(
                f"executable size {shares:.6f} is below venue minimum {rules['minimum']}")
        funds = _read_balance(client)
        if funds is None:
            raise RuntimeError("malformed balance/allowance response")
        total_cost = float(Decimal(str(amount)) + estimated_fee)
        if funds["balance"] + 1e-9 < total_cost:
            raise RuntimeError(
                f"insufficient pUSD balance (${funds['balance']:.6f} < ${total_cost:.6f} incl fee)")
        if funds["allowance"] + 1e-9 < total_cost:
            raise RuntimeError(
                f"insufficient pUSD allowance (${funds['allowance']:.6f} < ${total_cost:.6f} incl fee)")
        _validate_round_end(end)
    except Exception as exc:
        last_order_error = _safe_error(exc)
        print(f"[LIVE] Order preflight failed: {last_order_error}")
        return False

    # Retry only an explicit FOK no-fill.  Every retry rebuilds and re-signs;
    # transport timeouts remain ambiguous and are never retried.
    for attempt in range(3):
        guard_error = _pre_submit_guard_error(pre_submit_guard)
        if guard_error is not None:
            last_order_error = guard_error
            print(f"[LIVE] Order blocked before signing: {last_order_error}")
            return False
        try:
            _validate_round_end(end)
            options = PartialCreateOrderOptions(
                tick_size=str(rules["tick"]), neg_risk=rules["neg_risk"])
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=Side.BUY,
                price=float(limit),
                order_type=OrderType.FOK,
                user_usdc_balance=funds["balance"],
            )
            signed = client.create_market_order(mo, options=options)
            _validate_round_end(end)
        except Exception as exc:
            last_order_error = _safe_error(exc)
            print(f"[LIVE] Order signing failed: {last_order_error}")
            return False
        guard_error = _pre_submit_guard_error(pre_submit_guard)
        if guard_error is not None:
            last_order_error = guard_error
            print(f"[LIVE] Order blocked before submission: {last_order_error}")
            return False
        try:
            resp = client.post_order(signed, OrderType.FOK)
        except Exception as exc:
            err = _safe_error(exc)
            if _is_no_match(err) and attempt < 2:
                time.sleep(0.5)
                continue
            if not _is_no_match(err):
                _mark_ambiguous(condition_id, end)
                err = f"ambiguous submission; round blocked: {err}"
            last_order_error = err
            print(f"[LIVE] Place order error: {last_order_error}")
            return False

        oid, status, err = _accepted_order_response(resp)
        if err is not None:
            err = _safe_error(err)
            if _is_no_match(err) and attempt < 2:
                time.sleep(0.5)
                continue
            pending_oid, pending_status, pending_kind = _accepted_pending_response(resp)
            if pending_oid and pending_kind:
                last_order_status = pending_kind
                last_order_receipt = _build_receipt(
                    resp, pending_oid, pending_status,
                    condition_id=condition_id, token_id=token_id,
                    window_end=end, amount=amount, estimated_fee=estimated_fee,
                    fee_rate=rules["fee_rate"],
                    fee_exponent=rules["fee_exponent"],
                    validation=pending_kind,
                )
                journaled = _journal_receipt(last_order_receipt)
                last_order_receipt["accounting_journaled"] = journaled
                if not journaled:
                    _mark_ambiguous(condition_id, end)
                    _journal_fault = (
                        "CRITICAL: accepted order could not be durably journaled; "
                        "all further live submissions are disabled")
                    last_order_error = _journal_fault
                    last_order_status = f"{pending_kind}_JOURNAL_FAULT"
                    print(f"[LIVE] {_journal_fault}")
                    # The order was accepted. Returning False could make the
                    # caller retry an already-live action; return True while
                    # the process-wide fault blocks every later submission.
                    return True
                # A documented delayed placement has a known order ID and is
                # followed through the user channel. Any other incomplete
                # success blocks another submission for this condition because
                # its terminal outcome is not yet known.
                if pending_kind != "DELAYED_PENDING_OUTCOME":
                    _mark_ambiguous(condition_id, end)
                last_order_error = None
                print(
                    f"[LIVE] FOK accepted: {side} ${amount:.2f} "
                    f"orderID={pending_oid} status={pending_status or 'missing'} | "
                    "no fill recorded; waiting for CONFIRMED trade status"
                )
                return True
            # A response received after POST that is not an explicit no-fill
            # may describe an accepted order incompletely. Block the round.
            if not _is_no_match(err) and not _definitive_rejection(resp):
                _mark_ambiguous(condition_id, end)
                err = f"ambiguous order response; round blocked: {err}"
            last_order_error = err
            print(f"[LIVE] Place order error: {last_order_error}")
            return False

        last_order_status = "MATCHED_PENDING_CONFIRMATION"
        last_order_receipt = _build_receipt(
            resp, oid, status, condition_id=condition_id, token_id=token_id,
            window_end=end, amount=amount, estimated_fee=estimated_fee,
            fee_rate=rules["fee_rate"], fee_exponent=rules["fee_exponent"],
            validation="MATCHED_WITH_TRADE_EVIDENCE",
        )
        journaled = _journal_receipt(last_order_receipt)
        last_order_receipt["accounting_journaled"] = journaled
        if not journaled:
            _mark_ambiguous(condition_id, end)
            _journal_fault = (
                "CRITICAL: matched order could not be durably journaled; "
                "all further live submissions are disabled")
            last_order_error = _journal_fault
            last_order_status = "MATCHED_PENDING_CONFIRMATION_JOURNAL_FAULT"
            print(f"[LIVE] {_journal_fault}")
            # POST succeeded, so this remains a submitted action.  The fault
            # gate above prevents a duplicate or any subsequent live order.
            return True
        print(
            f"[LIVE] FOK matched: {side} ${amount:.2f} orderID={oid} | "
            "position waits for CONFIRMED trade status"
        )
        return True

    last_order_error = "FOK no-fill after 3 freshly signed attempts"
    return False
