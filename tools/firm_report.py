#!/usr/bin/env python3
"""Short paper report from firm/TRADE_LEDGER.csv: weeks, wins, net, worst week, drawdown vs the backtest limits.
Local file only; no network; no orders.   python3 tools/firm_report.py"""
import csv
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "firm" / "TRADE_LEDGER.csv"
WORST_WEEK_PER_LOT = -10352   # backtest worst week (FIRM_RULES.md)

rows = list(csv.DictReader(open(LEDGER)))
done = [r for r in rows if r["net_rs"]]
open_ = [r for r in rows if not r["net_rs"]]
net = [float(r["net_rs"]) for r in done]
eq = pk = dd = 0.0
for v in net:
    eq += v; pk = max(pk, eq); dd = min(dd, eq - pk)
print("Paper condor: %d settled weeks, %d open" % (len(done), len(open_)))
if net:
    print("win %.0f%% | net Rs %+.0f | worst week Rs %+.0f | max drawdown Rs %+.0f"
          % (100 * sum(v > 0 for v in net) / len(net), sum(net), min(net), dd))
    for r in done:
        if float(r["net_rs"]) < WORST_WEEK_PER_LOT * int(r["lots"]):
            print("FIRE CHECK: week %s lost more than the backtest worst week" % r["week_expiry"])
for r in open_:
    print("open: %s %s lots, CE %s/%s PE %s/%s credit %s" % (r["week_expiry"], r["lots"], r["short_ce"], r["long_ce"],
                                                             r["short_pe"], r["long_pe"], r["credit_pts"]))
