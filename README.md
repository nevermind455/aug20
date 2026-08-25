# BTC 5-minute Up/Down bot

This build starts in **paper mode by default**. Paper mode uses the live BTC
trade stream, the live Chainlink 60-second TWAP, current Polymarket market
identity, public CLOB rules, and the live public order book. It simulates FOK
execution and persists fills, fees, settlement, cash, and PnL without loading a
wallet or calling an authenticated endpoint.

## Install

Python 3.11 or newer is required. On Windows, `tzdata` is required so
`America/New_York` can be loaded.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env              # optional for paper; edit settings as needed
chmod 600 .env                    # required on POSIX when .env is used
```

Never put a real private key in a file you will share or commit. Prefer a
process supervisor or secret manager for live credentials; see `SECURITY.md`
for POSIX permissions, Windows ACLs, log handling, and release checks.

## Run safely

```bash
python run_feeds.py --paper --dash
```

The shorter equivalent is `python run_feeds.py --dash`; paper is the default.
The first paper launch uses `PAPER_START_BALANCE` (default 1000). Later launches
reuse the persisted paper account and ledger instead of resetting PnL.

Public feed diagnostics, with no wallet access or trading:

```bash
python run_feeds.py --health
```

Run the deterministic checks:

```bash
python tests_fixes.py
python tests_paper.py
python tests_accounting.py
python tests_feeds.py
python tests_dashboard.py
python run_terminal.py --selftest
```

## Live mode

Live mode is deliberately opt-in:

```bash
python run_feeds.py --live --dash
```

Do not use live mode until the credentialed canary checks listed in
`DEEP_AUDIT.md` pass for the actual wallet. The current audit verdict is
**live readiness: FAIL pending credentialed venue testing**. In particular,
the pinned V2 SDK currently has an open upstream type-3/POLY_1271 API-key
derivation issue, so this build rejects `POLY_SIGNATURE_TYPE=3` at startup.

See `PAPER_MODE.md`, `FEEDS.md`, `ACCOUNTING.md`, and `DEEP_AUDIT.md` for the
execution model, persistence files, safeguards, findings, and remaining risks.
