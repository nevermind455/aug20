# aug13 → aug18: bug pass + measurement tools

All six suites green: **49,556 assertions, 0 failures.** pyflakes clean.
Every fix has a test that was confirmed to FAIL on the original tree
(`tests_bugfix.py` scores 40/0 here and 13/14 against the aug13 zip).

---

## Bugs fixed

### 1. `signal_journal.py resolve` could never resolve anything — CRITICAL

It called `market_discovery.get_btc_5m_tokens(window)` for a **past** window.
That function returns `None` for any `window < current` by design — the guard
that stops the order path touching an already-resolving market. Verified with
a stub that would have answered: **zero Gamma requests issued**, empty winners
file, exit code 0, printing `resolved 0 rounds, N still pending`. A
success-shaped failure that reads as "wait longer" forever.

The bug was double. Even past the window gate, `_parse_event` requires
`not closed` and `active`, and `_tradeable` requires `acceptingOrders` and
`enableOrderBook`. A settled round fails all four — the live parser rejects
exactly the rounds a resolver needs.

**Fix:** new `journal_resolve.py`. Fetches the slug directly with a local
parser that keeps every *identity* check — slug match, market start/end
timestamps matching the window to the second, outcomes exactly up/down, token
and condition regexes, refuse if more than one candidate — and drops only the
tradeability flags. It cannot place an order, so there is nothing for those
flags to protect. Atomic temp-and-replace write. `signal_journal.py resolve`
now delegates to it, so both entry points work.

The H6 guard itself is untouched and `tests_bugfix.py` asserts it still
refuses past windows.

### 2. `signal_journal.analyze()` understated its error bar by 12×

`se = 50 / sqrt(n)` used `n = len(rows)` — **samples**, not rounds. At a 2s
cadence there are ~150 samples per 5-minute round, all settling on the same
outcome. 100 rounds reported ±0.41 points against a round-clustered truth of
±5.0. Its advice — "for a 3-point edge you need about 1,111 samples" — meant
about seven rounds. The real answer is 1,111 rounds.

**Fix:** the standard error is computed from the round count, the
sample-size line is stated in ROUNDS, and the report now prints how much
too confident the sample-count version would have been.

### 3. `round_exposure` under-counted real cash by 22%

`main_bot` charged `MAX_ROUND_EXPOSURE` exactly `BET_SIZE` per entry, but
`paper_trade.size_to_venue_minimum` sizes **up** to the 5-share venue
minimum, and the fee lands on top. Measured on the aug17 paper run: real
$1,049.52 against $857.50 counted; worst single round real $34.68 against
$22.50 (+54%), another at +76%. The cap was nominal, not real.

**Fix:** `config.entry_cost_ceiling(cap_price)` returns
`max(BET_SIZE, 5 × cap_price) × (1 + θ)` — an upper bound, which is what a
limit needs. Both `main_bot` sites gate and charge that same number, and
`_round_entry_budget()` reserves each band at its own ceiling price.

Default `MAX_ROUND_EXPOSURE` moves 57.50 → 72.23, which admits exactly the
same 23 entries as before. **No behaviour change at defaults** — the worst
round in the aug17 run spent $34.68, well under either figure. What changed
is that the number now means dollars. Set it explicitly and it will bind
correctly, which it would not have before.

### 4. Binance strike taken from a mid-round sample

`record()` did `bn_strike.setdefault(window, spot)` unconditionally, so
starting the recorder mid-round stored a mid-round price as that round's
strike and every Binance-signal call for the round compared against the wrong
reference. One contaminated round per restart — and the aug17 exit log shows
eight restarts in a day.

**Fix:** a sample may only claim to be a round's strike within
`BOUNDARY_GRACE` (5s) of the open; otherwise the field stays blank and
`analyze` skips the round rather than scoring it against a fiction. The dict
is also pruned past an hour instead of growing 288 entries a day forever.

### 5. Phase-1 fills mislabelled as trading against every signal

`analyze_pnl.py`'s votes table scored phase-1 rows as
`0/3 backed the side taken`. Phase 1 writes empty signal columns because it
asks the book for a **price** and never consults a signal — so that reads as
"traded against all three signals" when nothing was consulted. It is the
largest bucket in the table, at 206 fills.

**Fix:** blank columns get their own label; the vote count is now out of the
signals that actually spoke (`1/2` when one abstains). Real disagreement
still reads `0/3`.

### Lint

`accounting.fees` (paper_trade), `dashboard.make_renderer` (run_terminal),
`statistics`/`time`/`defaultdict` (signal_journal) removed; the `field` loop
variable in `feeds/poly_user.py` renamed so it stops shadowing the
`dataclasses` import.

---

## New tools

| file | what it does |
|---|---|
| `journal_resolve.py` | the working resolver (bug 1) |
| `tests_bugfix.py` | 40 assertions; 14 of them fail on the aug13 tree |
| `edge_test.py` | round-clustered significance on the paper ledger — t, bootstrap CI resampling rounds not fills, "rounds needed for \|t\|=2" |
| `band_backtest.py` | tests a `PHASE1_BANDS` schedule: deterministic mechanics, fee arithmetic, censored replay, uncensored journal backtest |
| `band_backtest_selftest.py` | 7 cases proving the backtester can say yes *and* no |

### Order to run

```
python3 signal_journal.py record      # leave running — samples every round
python3 journal_resolve.py            # later; fills in the winners
python3 band_backtest.py --bands "..."
```

---

## Two things the tests do not fix

**The clock.** 65% of aug17 paper fills logged a *negative* `book_age_ms`;
`book_timestamp − order_wall` ran +1.07s median, so the venue clock reads
about a second ahead of the machine. `paper_trade.py:682` tolerates
`book_age_s < -5.0` before rejecting, and `timer.check_clock` only runs in
LIVE mode — the paper run never validated the clock its own freshness guard
depends on. Left alone deliberately: tightening it changes which fills happen
and needs its own measurement first.

**The result.** 88 settled rounds, net −$18.75 at t = −0.16, gross +$11.57 at
t = +0.10, inside the zero-edge band for all 88 rounds. Median round −$2.60;
five rounds carry +$131. None of the fixes above change that — they make the
instruments trustworthy enough to answer it.
