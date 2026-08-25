# Terminal dashboard

Safe default:

```bash
python run_feeds.py --paper --dash
```

Equivalent dashboard entrypoint:

```bash
python run_terminal.py
```

Live mode requires the explicit `--live` flag. The banner is authoritative:
paper must display `MODE=PAPER (NO WALLET / NO SIGNATURE / NO LIVE ORDERS)`.

The dashboard observes the hardened runner. It shows the current condition and
round timestamps, UP and DOWN books, the official Chainlink 60-second TWAP
**Price To Beat** captured at the five-minute boundary, the fresh Chainlink
60-second TWAP **Running Price**, and the auxiliary Binance spot feed,
strategy legs, actual submission result, confirmed fills, shares, average
entry, fees, cash/balance, realized and mark-to-bid unrealized PnL, settlement
state, feed liveness, persistence errors, and reconciliation health. Missing or
stale data is displayed as absent; the renderer does not invent prices, fills,
outcomes, or PnL.

At every round change, the Price To Beat and all round-scoped signals are
cleared atomically before the new label is shown. If the exact boundary TWAP
was missed, Price To Beat remains `--` for that round. If the live TWAP becomes
stale or disconnected, Running Price becomes `--` instead of retaining the
last number.

Probe, terminal-size, render, and output errors are recorded in dashboard state
instead of being silently swallowed. Slow rendering automatically reduces its
refresh rate so the bot loop keeps priority.

Verification:

```bash
python tests_dashboard.py            # 48,946 passed
python run_terminal.py --selftest     # PASS at 11 terminal sizes
```
