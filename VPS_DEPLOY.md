# Running on a Linux VPS

Written after a day of chasing failures that were all the same root cause: a
slow, intermittently-hijacked home connection and a hardware clock drifting
1.3 s/hour. A VPS fixes both. Nothing in this file changes strategy — it only
moves the bot somewhere its inputs are reliable.

## Why move

Measured on the home machine, all of which a VPS removes:

| Symptom | Root cause |
| --- | --- |
| `SSLError` / `ConnectTimeout` on every venue call | ISP DNS-hijacks `polymarket.com` to a block server and RSTs on SNI |
| `public order book is stale` | clock drifted −3.8 s to −8.9 s; book age is measured against it |
| `cannot read live market rules` | 8.6 s response against an 8.0 s timeout |
| Settlement 8.6 min late | CLOB `closed` flag lags; needs a Polygon RPC that answers |
| 18% of rounds skipped | boundary latch missed across reconnects and restarts |

## Prerequisites

- **Python 3.11+** (`README.md` states this; 3.13 is what has been tested).
- A region where Polymarket's API and a Polygon RPC are both reachable.
  Verify before paying for anything — see "Verify the host first".
- No GPU, no large disk. This is a few HTTP calls and two websockets.

## Verify the host first

Run this on the candidate VPS before deploying. Every line must pass; these are
the exact surfaces that failed at home.

```bash
python3 - <<'PY'
import socket, ssl, time, urllib.request, json
def check(name, host, port=443):
    try:
        ips = socket.gethostbyname_ex(host)[2]
        t0 = time.time()
        with socket.create_connection((ips[0], port), timeout=8) as s:
            with ssl.create_default_context().wrap_socket(s, server_hostname=host):
                print(f"  OK   {name:<26} {ips[0]:<16} {(time.time()-t0)*1000:.0f}ms")
    except Exception as e:
        print(f"  FAIL {name:<26} {type(e).__name__}: {str(e)[:50]}")
for n, h in [("Polymarket CLOB","clob.polymarket.com"),
             ("Polymarket Gamma","gamma-api.polymarket.com"),
             ("Binance WS","stream.binance.com", ),
             ("Polygon RPC","polygon-bor-rpc.publicnode.com")]:
    check(n, h if isinstance(h,str) else h[0], 9443 if "binance" in str(h) else 443)
PY
```

Two hosts that are **hijacked on the home network** and must be re-tested, not
assumed: `polygon-rpc.com` and `rpc.ankr.com` both resolved to `152.236.9.75`
and answered HTTP 401. Working alternatives: `polygon-bor-rpc.publicnode.com`,
`1rpc.io/matic`, `polygon.drpc.org`.

## Install

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip chrony
git clone <your repo> /opt/btcbot && cd /opt/btcbot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` includes `web3`, which only the on-chain settlement path
uses. Without it `SETTLE_ONCHAIN` fails closed and settlement falls back to the
CLOB/Gamma APIs — slower, but correct.

## Clock

Do not skip this. Book staleness, order cutoffs and round identity are all
measured against the system clock, and LIVE mode refuses to start outside
`CLOCK_MAX_DRIFT_SECONDS` (2.0 s).

```bash
sudo systemctl enable --now chrony
chronyc tracking          # "Leap status : Normal", offset well under 1s
chronyc sources -v
```

Most VPS hosts run a local NTP server that is far better than anything
reachable from a home connection. Verify against the venue's own clock too:

```bash
python3 -c "
import requests, time, statistics as st
s=[]
for _ in range(5):
    b=time.time(); r=requests.get('https://clob.polymarket.com/time',timeout=6); a=time.time()
    v=r.json(); v=v.get('timestamp',v.get('time')) if isinstance(v,dict) else v
    v=float(v); v = v/1000 if v>1e10 else v
    s.append((b+a)/2-v); time.sleep(0.3)
print('median drift %+.3fs (limit 2.000s)'%st.median(s))"
```

## Permissions

On POSIX, `config.py` refuses to start unless `.env` is not group/other
readable — it holds live credentials:

```bash
chmod 600 .env
```

Without this you get `PermissionError: .env contains live credentials and must
have mode 0600` at import, before anything else runs.

## systemd unit

`/etc/systemd/system/btcbot.service`:

```ini
[Unit]
Description=BTC 5-min Polymarket bot (paper)
After=network-online.target chrony.service
Wants=network-online.target

[Service]
Type=simple
User=btcbot
WorkingDirectory=/opt/btcbot
ExecStart=/opt/btcbot/.venv/bin/python run_feeds.py --paper
Restart=always
RestartSec=10
StandardOutput=append:/var/log/btcbot/bot.log
StandardError=append:/var/log/btcbot/bot.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/log/btcbot && sudo chown btcbot /var/log/btcbot
sudo systemctl daemon-reload && sudo systemctl enable --now btcbot
journalctl -u btcbot -f
```

Notes that matter:

- **Run without `--dash`.** The dashboard needs a TTY. Use the plain log form
  under systemd and read `journalctl`, or run `--dash` by hand inside `tmux`
  when you want to watch it.
- The Enter kill-switch is inert under systemd: stdin is `/dev/null`, `input()`
  raises `EOFError`, and the thread exits cleanly. Use `systemctl stop btcbot`.
- **`Restart=always` costs a round each time.** The Chainlink strike service
  discards the window its connection was established during, and the Binance
  boundary print can only be latched in the first 5 s of a round. A restart
  mid-round means that round does not trade. Measured: this, not feed failure,
  caused most of the 18% skipped rounds.
- The process lock uses `fcntl.flock` on POSIX (`msvcrt` only on Windows), so
  two units cannot share one ledger.

## Before switching to live

Live mode is a different code path from what has been tested here. These are
gated on `mode == "PAPER"` and **will not run live**:

- `PHASE2_MULTI_SIGNAL` — the per-signal legs
- `PAIR_LOCK_ENABLED` — the basis provider is only wired in the paper branch
- `PAPER_ALLOW_SIGNAL_FLIPS`

Live runs the plain single-leg SIG PRICE path. It also requires the private
fill stream (`USER_WS`) to be LIVE and subscribed, and refuses to start if the
clock is outside 2.0 s. Credentials (`POLY_PRIVATE_KEY`, `POLY_FUNDER`,
`POLY_SIGNATURE_TYPE`) go in `.env` by hand — see `SECURITY.md`.
