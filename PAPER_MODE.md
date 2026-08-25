# Real paper mode

Run the unchanged strategy and timing loop against live BTC, Chainlink 60-second
TWAP, Polymarket market discovery, and the public CLOB order book:

```bash
python run_feeds.py --paper --dash
```

Choose the starting cash on the first launch:

```bash
python run_feeds.py --paper --paper-balance 1000 --dash
```

`PAPER_START_BALANCE=1000` in `.env` does the same thing. Once
`paper_account.json` exists, its starting balance wins so restarting the bot
cannot silently reset the account.

## Safety boundary

Paper mode does not need a private key. It does not derive API credentials,
connect the private user WebSocket, reconcile a wallet, construct/sign orders,
cancel venue orders, or call an order endpoint. It also installs a fail-closed
guard on the live CLOB client for the lifetime of the paper process.

The terminal must show both of these banners:

```text
MODE=PAPER (NO WALLET / NO SIGNATURE / NO LIVE ORDERS)
LIVE ORDERS DISABLED
```

If it says `MODE=LIVE`, stop with `Ctrl+C`.

## Fill and PnL model

Each paper BUY re-reads the selected outcome's public order book at submission
time. It walks asks from best to worst and applies the same worst-price cap as
the live FOK. The entire dollar amount must be available or the order is
rejected; partial fills are never invented. Venue minimum size, current public
V2 fee rate/exponent, depth, slippage, and fees at each consumed level are
recorded.

Filled positions remain pending until the public CLOB winner flags and Gamma's
explicit oracle resolution independently agree on the exact final payouts. A
merely closed market or a near-1.0 price never settles paper cash. PnL is:

```text
shares × resolved payout − filled notional − taker fees
```

Open positions are marked to the live bid and are labelled unrealized. They are
not counted as settled profit.

Files are separate from live trading:

- `paper_ledger.json` — persistent fills, positions, fees, and settlement
- `paper_account.json` — starting cash identity
- `paper_orders.jsonl` — full fill/rejection audit with price levels
- `paper_trade_log.csv` — strategy attempt log

The account and ledger share a random durable ledger ID. They can be moved
together to a new directory without resetting cash or PnL; the diagnostic
absolute path is refreshed automatically. A missing ledger or an ID mismatch
stops startup instead of silently opening an empty bankroll. Legacy path-based
files migrate once only after their creation metadata and, when fills exist,
audit IDs prove that they are a pair.

On restart, confirmed outcome-token inventory is restored by condition before
either strategy phase can buy. This preserves the rule that the bot never buys
the complementary outcome merely because its process-local state was reset.

Paper execution applies `PAPER_LATENCY_MS` plus the venue's public
`seconds_delay` value, then re-fetches the public book. If the venue exposes a
delay flag but no duration, the paper order fails closed. It still cannot
reproduce private matcher queueing or book movement during a real submission.
The public book also
aggregates makers at the same price, so sub-cent fee rounding can differ from
the venue's individual fills. Therefore it is a realistic live-data
simulation, not a guarantee of identical live fills.

## Other commands

Public feed health only, with no wallet authentication:

```bash
python run_feeds.py --health
```

Live trading requires the explicit flag and wallet credentials:

```bash
python run_feeds.py --live --dash
```
