#!/usr/bin/env python3
"""When do the SENSEX breakout-buying losses happen? Per-trade results of FIRST_TOUCH (every crossing) and FILTERED,
split by weekday, expiry proximity, time of day, gap, day range and day type. Real expired ATM premiums, +6/-11,
10-min stop, entry at the crossing, net = pts x 20 - Rs 70. Local files only; no orders.

  python3 tools/loss_days_breakdown.py INDEX_CSV OPTION_HISTORY_DIR
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_filtered import filtered_signals              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402


def all_signals(days):
    sig = []
    for d in sorted(days):
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        for e in b.day_events(bars):
            j = idx[e["time"]]; up = e["side"] == "CE"
            entry = max(e["trigger"], bars[j]["open"]) if up else min(e["trigger"], bars[j]["open"])
            sig.append(dict(day=d, j=j, side=e["side"], ientry=entry, rule="FT"))
    return sig


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1)
    ds = sorted(days); exp = expiry_days(opt)
    dte = {}
    for d in ds:
        e = next((x for x in sorted(exp) if x >= d), None)
        dte[d] = None if e is None else sum(1 for x in ds if d <= x < e)
    info = {}
    for i, d in enumerate(ds):
        bars = days[d]; o, c = bars[0]["open"], bars[-1]["close"]
        hi = max(x["high"] for x in bars); lo = min(x["low"] for x in bars)
        prev = days[ds[i - 1]][-1]["close"] if i else o
        info[d] = dict(wd=bars[0]["t"].strftime("%a"), gap=100 * (o - prev) / prev, rng=hi - lo,
                       trend=abs(c - o) / (hi - lo) if hi > lo else 0)
    for name, sig in (("FIRST_TOUCH", all_signals(days)), ("FILTERED", filtered_signals(days))):
        rows = []
        for s in sig:
            r = ro.trade(days, opt, s, "cross", 0)
            if r is not None:
                rows.append((s, r["pts"] * ro.LOT - ro.CHARGES))
        day_net = defaultdict(float)
        for s, v in rows:
            day_net[s["day"]] += v
        print("\n==== %s: %d trades, %d days traded, net Rs %+.0f; losing days %d / %d"
              % (name, len(rows), len(day_net), sum(v for _, v in rows), sum(v < 0 for v in day_net.values()), len(day_net)))

        def group(title, key):
            g = defaultdict(list)
            for s, v in rows:
                g[key(s)].append(v)
            print("  by %s:" % title)
            for k in sorted(g, key=str):
                v = g[k]
                print("    %-22s trades %4d  win %4.1f%%  net Rs %+9.0f  per trade %+6.0f"
                      % (k, len(v), 100 * sum(x > 0 for x in v) / len(v), sum(v), sum(v) / len(v)))
        group("weekday", lambda s: info[s["day"]]["wd"])
        group("days to expiry", lambda s: {0: "0 expiry day", 1: "1 day before"}.get(dte[s["day"]], "2+ days" if dte[s["day"]] is not None else "?"))
        group("time of day", lambda s: (lambda hm: "09:15-10:00" if hm < "10:00" else "10:00-11:30" if hm < "11:30" else
                                         "11:30-13:30" if hm < "13:30" else "13:30-15:30")(days[s["day"]][s["j"]]["t"].strftime("%H:%M")))
        group("opening gap", lambda s: (lambda g: "gap > +0.5%" if g > 0.5 else "gap < -0.5%" if g < -0.5 else "flat open")(info[s["day"]]["gap"]))
        group("day range", lambda s: (lambda r: "< 400 pts" if r < 400 else "400-700" if r < 700 else "700-1000" if r < 1000 else "> 1000")(info[s["day"]]["rng"]))
        group("day type", lambda s: (lambda t: "trend day (close near end)" if t >= 0.6 else "choppy (close mid-range)" if t < 0.3 else "mixed")(info[s["day"]]["trend"]))
        group("side", lambda s: s["side"])
        worst = sorted(day_net.items(), key=lambda kv: kv[1])[:8]
        print("  worst days: " + ", ".join("%s %s %+.0f" % (d, info[d]["wd"], v) for d, v in worst))


if __name__ == "__main__":
    main()
