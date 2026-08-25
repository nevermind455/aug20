"""Round-clustered edge test for the paper ledger.

analyze_pnl.py answers "what happened". This answers "is any of it real".

Every table here clusters by condition_id: fills inside one 5-minute round
share a single settlement, so treating them as independent observations
manufactures significance. The unit of evidence is the round.

Run from the tree root:  python3 edge_test.py
"""
from __future__ import annotations

import collections
import csv
import json
import math
import random
import statistics as st

THETA = 0.07          # Polymarket crypto taker fee rate
ROUND_SECONDS = 300


# ---------------------------------------------------------------- loading

def load():
    """Join orders -> phase (from the trade log) -> settlement (from the ledger)."""
    orders = [json.loads(line) for line in open("paper_orders.jsonl")]
    filled = [o for o in orders if o["status"] == "FILLED"]

    rows = [r for r in csv.DictReader(open("paper_trade_log.csv"))
            if r["result"] == "paper_filled"]
    if len(rows) == len(filled):
        for order, row in zip(filled, rows):
            order["phase"] = row.get("phase", "?")
    else:
        # Log and ledger disagree on length; refuse to guess the alignment.
        for order in filled:
            order["phase"] = "?"
        print(f"! trade log has {len(rows)} filled rows vs {len(filled)} filled "
              f"orders - phase labels dropped\n")

    ledger = json.load(open("paper_ledger.json"))["positions"]
    payout = {tok: (pos["payout_per_share"] if pos.get("settled") else None)
              for tok, pos in ledger.items()}

    settled, unsettled = [], 0
    for order in filled:
        pps = payout.get(order["token_id"])
        if pps is None:
            unsettled += 1
            continue
        order["payout_per_share"] = pps
        order["stake"] = order["shares"] * order["average_price"]
        order["gross"] = order["shares"] * pps - order["stake"]
        order["net"] = order["gross"] - order["fee"]
        order["secs_left"] = ROUND_SECONDS - (order["wall"] % ROUND_SECONDS)
        settled.append(order)
    if unsettled:
        print(f"note: {unsettled} unsettled fill(s) excluded\n")
    return settled


# ---------------------------------------------------------------- statistics

def by_round(orders):
    """Collapse fills to one (stake, fee, gross, net) tuple per round."""
    agg = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for o in orders:
        a = agg[o["condition_id"]]
        a[0] += o["stake"]
        a[1] += o["fee"]
        a[2] += o["gross"]
        a[3] += o["net"]
    return list(agg.values())


def tstat(values):
    n = len(values)
    if n < 2:
        return float("nan"), float("nan")
    mean = st.mean(values)
    sd = st.stdev(values)
    if sd == 0:
        return mean, float("nan")
    return mean, mean / (sd / math.sqrt(n))


def bootstrap_ci(values, iters=20000, seed=0):
    """Resample ROUNDS, not fills. Percentile CI on the total."""
    if len(values) < 2:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(values)
    totals = []
    for _ in range(iters):
        totals.append(sum(values[rng.randrange(n)] for _ in range(n)))
    totals.sort()
    return totals[int(0.025 * iters)], totals[int(0.975 * iters)]


def rounds_for_significance(values):
    """How many rounds this effect size would need to reach |t| = 2."""
    if len(values) < 2:
        return None
    mean = st.mean(values)
    sd = st.stdev(values)
    if mean == 0 or sd == 0:
        return None
    return int((2 * sd / mean) ** 2)


# ---------------------------------------------------------------- reporting

def slice_table(title, orders, keyfunc, order_keys=None):
    groups = collections.defaultdict(list)
    for o in orders:
        groups[keyfunc(o)].append(o)

    keys = order_keys or sorted(groups)
    print("=" * 86)
    print(title)
    print("=" * 86)
    print(f"  {'bucket':<14}{'rnds':>5}{'stake':>9}{'gross':>9}{'net':>9}"
          f"{'net/$100':>10}{'t(net)':>8}{'verdict':>14}")
    for key in keys:
        rows = groups.get(key)
        if not rows:
            continue
        agg = by_round(rows)
        stake = sum(a[0] for a in agg)
        gross = sum(a[2] for a in agg)
        net = [a[3] for a in agg]
        _, t = tstat(net)
        verdict = "NOISE" if not (abs(t) >= 2) else ("EDGE" if t > 0 else "LOSS")
        if len(agg) < 30:
            verdict = "too few rounds"
        print(f"  {str(key):<14}{len(agg):>5}{stake:>9.0f}{gross:>+9.2f}"
              f"{sum(net):>+9.2f}{100 * sum(net) / stake:>+10.2f}"
              f"{t:>+8.2f}   {verdict:<14}")
    print()


def main():
    orders = load()
    if not orders:
        print("no settled fills in the ledger")
        return

    agg = by_round(orders)
    stake = sum(a[0] for a in agg)
    fees = sum(a[1] for a in agg)
    gross = [a[2] for a in agg]
    net = [a[3] for a in agg]

    shares = sum(o["shares"] for o in orders)
    avg_px = sum(o["average_price"] * o["shares"] for o in orders) / shares
    won = sum(o["shares"] for o in orders if o["payout_per_share"] > 0) / shares
    need = avg_px + THETA * avg_px * (1 - avg_px)

    mean_net, t_net = tstat(net)
    mean_gross, t_gross = tstat(gross)
    lo, hi = bootstrap_ci(net)

    print("=" * 86)
    print("IS THERE AN EDGE   (round-clustered; the round is the unit, not the fill)")
    print("=" * 86)
    print(f"  rounds                    {len(agg):>10}")
    print(f"  fills                     {len(orders):>10}")
    print(f"  stake                     ${stake:>9,.2f}")
    print(f"  fees                      ${fees:>9,.2f}   "
          f"({100 * fees / stake:.2f}% of stake)")
    print()
    print(f"  gross PnL                 ${sum(gross):>+9.2f}   t = {t_gross:+.2f}")
    print(f"  net PnL                   ${sum(net):>+9.2f}   t = {t_net:+.2f}")
    print(f"  95% CI on net PnL         ${lo:>+9.2f} to ${hi:+.2f}")
    print(f"  median round              ${st.median(net):>+9.2f}")
    print(f"  sd per round              ${st.stdev(net):>9.2f}   "
          f"(mean stake ${stake / len(agg):.2f}/round)")
    print()
    print(f"  share-weighted avg price  {avg_px:>10.4f}   <- implied odds paid")
    print(f"  win rate                  {won:>10.4f}   <- what happened")
    print(f"  break-even after fees     {need:>10.4f}   <- p + theta*p*(1-p)")
    print(f"  edge vs price             {100 * (won - avg_px):>+9.2f}pp")
    print(f"  edge vs break-even        {100 * (won - need):>+9.2f}pp")
    print()
    need_n = rounds_for_significance(gross)
    if need_n:
        print(f"  rounds needed for |t|=2 on gross PnL at this effect size: {need_n:,}")
        print(f"  at 288 rounds/day that is {need_n / 288:,.0f} days of running")
    print()

    slice_table("BY PHASE", orders, lambda o: o["phase"])

    def price_bucket(o):
        p = o["average_price"]
        for lo_, hi_ in ((0, .30), (.30, .50), (.50, .70), (.70, .85), (.85, 1.0)):
            if lo_ <= p < hi_:
                return f"{lo_:.2f}-{hi_:.2f}"
        return "0.85-1.00"

    slice_table("BY ENTRY PRICE", orders, price_bucket)

    def time_bucket(o):
        s = o["secs_left"]
        for lo_, hi_ in ((240, 300), (180, 240), (120, 180), (60, 120), (0, 60)):
            if lo_ <= s < hi_:
                return f"{hi_}-{lo_}s"
        return "?"

    slice_table("BY SECONDS LEFT AT ENTRY", orders, time_bucket,
                order_keys=["300-240s", "240-180s", "180-120s", "120-60s", "60-0s"])

    print("=" * 86)
    print("HOW TO READ THIS")
    print("=" * 86)
    print("  A bucket is evidence only if |t| >= 2 AND it has enough rounds to")
    print("  produce that t honestly. Every bucket below 30 rounds is labelled")
    print("  'too few rounds' on purpose: with sd near the size of the stake, a")
    print("  handful of rounds can print any number you like. Tuning bands or")
    print("  price caps against a bucket marked NOISE is fitting the sample.")
    print()


if __name__ == "__main__":
    main()
