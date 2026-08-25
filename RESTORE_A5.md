# A.5 provenance (archival)

The original snapshot used a $2 stake, the last 60 seconds of each five-minute
round, a 0.99 maximum buy price, and allowed repeated entries. This audit
preserves those strategy decisions. It does not preserve unsafe operational
assumptions from the old runner.

Retired instructions included direct live startup, global cancellation by
default, permissive market/token parsing, a spot-price oracle substitute, and
recording an order call as a fill. Do not restore those paths.

Current safe start:

```bash
python run_feeds.py --paper --dash
```

Live mode is an explicit opt-in and is currently rated FAIL until credentialed
network/canary validation is completed:

```bash
python run_feeds.py --live --dash
```

See `README.md` and `DEEP_AUDIT.md`. Never copy a private key into a document,
source file, log, or ZIP.
