#!/usr/bin/env python3
"""Short strangle / iron condor across all downloaded years (May 2023 - Oct 2026), paper research, no orders.

  python3 tools/backtest_strangle_multiyear.py PICKLE [PICKLE ...]

Each PICKLE is pickle.dump(backtest_wings.load(atm_dir, wings_dir)). Periods are merged; expiry days come from the
data (backtest_wings.expiries), so Friday / Tuesday / Thursday expiry eras are all handled. Results use today's lot
of 20 for every year so years compare. EVENTS: weeks holding a scheduled event day (election results, Union
Budget) can be skipped.
"""
import collections, pickle, statistics, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_wings as w                                   # noqa: E402

EVENTS = {"2024-02-01": "Interim Budget", "2024-06-04": "Election results", "2024-07-23": "Union Budget",
          "2025-02-01": "Union Budget", "2026-02-01": "Union Budget"}


def dist(n, hedge=None):
    def f(r):
        legs = [("C", r["K"] + n, 1), ("P", r["K"] - n, 1)]
        if hedge:
            legs += [("C", r["K"] + hedge, -1), ("P", r["K"] - hedge, -1)]
        return legs
    return f


def delta(t, hedge_extra=None):
    def f(r):
        legs = []
        for kind in "CP":
            K = None
            for k in range(1, 25):
                K = r["K"] + 100 * k if kind == "C" else r["K"] - 100 * k
                s = w.sig(r, K)
                d = w.Phi((r["F"] - K) / s) if kind == "C" else w.Phi((K - r["F"]) / s)
                if d <= t:
                    break
            legs.append((kind, K, 1))
            if hedge_extra:
                legs.append((kind, K + hedge_extra if kind == "C" else K - hedge_extra, -1))
        return legs
    return f


def go(D, name, legs, charges, slip, ef, skip_events=False, show=True):
    w.STRUCT["X"] = (legs, charges, slip)
    o = w.run(D, "X", ef)
    if skip_events:
        days = sorted(D)
        o = [x for x in o if not any(x["day"] <= e <= x["expiry"] for e in EVENTS)]
    n = [x["net"] for x in o]
    eq = pk = dd = 0.0
    for v in n:
        eq += v; pk = max(pk, eq); dd = min(dd, eq - pk)
    yr = collections.defaultdict(float)
    for x in o:
        yr[x["day"][:4]] += x["net"]
    if show:
        print("%-44s n %3d win %5.1f%% net %+9.0f worst %+8.0f maxDD %+8.0f t %5.2f | %s" % (
            name, len(n), 100 * sum(v > 0 for v in n) / len(n), sum(n), min(n), dd,
            statistics.mean(n) / statistics.stdev(n) * len(n) ** 0.5, " ".join("%s:%+.0f" % kv for kv in sorted(yr.items()))))
    return o


def main():
    D = {}
    for p in sys.argv[1:]:
        D.update(pickle.load(open(p, "rb")))
    w.EXP.clear(); w.EXP.update(w.expiries(D)); w.EXTRAP["mode"] = "linear"
    print("days %d (%s .. %s), expiry weeks %d; lot 20; strangle Rs 150 + 4 pts, condor Rs 250 + 6 pts" % (
        len(D), min(D), max(D), len(w.EXP)))
    for skip in (False, True):
        print("-- %s" % ("skip election/budget weeks" if skip else "every week"))
        for lbl, ef in (("2 days before expiry", w.prev_day(2)), ("1 day before expiry", w.prev_day(1))):
            for nm, legs, ch, sl in (("strangle +-1000", dist(1000), 150, 4), ("strangle +-1500", dist(1500), 150, 4),
                                     ("strangle delta 0.20", delta(0.20), 150, 4), ("strangle delta 0.15", delta(0.15), 150, 4),
                                     ("strangle delta 0.10", delta(0.10), 150, 4),
                                     ("condor +-1000 / +-2000", dist(1000, 2000), 250, 6),
                                     ("condor delta 0.10 + 1000 wings", delta(0.10, 1000), 250, 6)):
                go(D, "%s, %s" % (nm, lbl), legs, ch, sl, ef, skip)
    print("-- worst 8 weeks, strangle +-1000, 2 days before")
    o = go(D, "", dist(1000), 150, 4, w.prev_day(2), show=False)
    for x in sorted(o, key=lambda x: x["net"])[:8]:
        print("  %s -> %s net %+8.0f credit %5.0f" % (x["day"], x["expiry"], x["net"], x["credit"]))
    print("-- trades around the election result (4 Jun 2024, a Tuesday; expiry then on Friday)")
    for nm, legs, ch, sl in (("strangle +-1000", dist(1000), 150, 4), ("strangle delta 0.10", delta(0.10), 150, 4),
                             ("condor +-1000 / +-2000", dist(1000, 2000), 250, 6)):
        for lbl, ef in (("2 days before", w.prev_day(2)), ("1 day before", w.prev_day(1))):
            for x in go(D, "", legs, ch, sl, ef, show=False):
                if "2024-05-27" <= x["expiry"] <= "2024-06-14":
                    print("  %-24s %-14s %s -> %s net %+8.0f" % (nm, lbl, x["day"], x["expiry"], x["net"]))


if __name__ == "__main__":
    main()
