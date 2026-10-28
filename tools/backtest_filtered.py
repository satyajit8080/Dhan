#!/usr/bin/env python3
"""FILTERED paper variant on 1 year of SENSEX 1-min bars + real expired ATM option premiums. Local files only.

  python3 tools/backtest_filtered.py candles.csv HISTORY_DIR

FILTERED (docs/PHASE6_FILTERED_V72.md) = FIRST_TOUCH crossings that pass all of:
  dayTrend      CE only when the market is above its day open, PE only when below. The live variant uses the listed
                future vs its open; the 1-year file has no futures, so the SENSEX index vs its first bar is used
                (the live `spotVsOpen0915` diagnostic) -- an approximation.
  levelTouches  the selected level has < 3 touches (fresh)
  sessionRange  today's 1-min high - low up to the crossing >= 300 pts
  strike ITM    <= 150 pts: the rolling ATM strike always passes
Trade: ATM CE/PE at the crossing ('cross' entry of backtest_real_options.py), +6 / -11 premium, 10-min time stop;
stop first when a bar reaches both. Costs: slippage on stops (0 or 4 pts) and Rs 70 per trade, lot 20. No orders.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402


def filtered_signals(days, range_min=300.0, use_trend=True, use_fresh=True, use_range=True):
    sig = []
    for d in sorted(days):
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        o0 = bars[0]["open"]
        for e in b.day_events(bars):
            j = idx[e["time"]]; up = e["side"] == "CE"
            entry = max(e["trigger"], bars[j]["open"]) if up else min(e["trigger"], bars[j]["open"])
            hi = max(x["high"] for x in bars[:j]) if j else bars[0]["high"]
            lo = min(x["low"] for x in bars[:j]) if j else bars[0]["low"]
            ref = bars[j - 1]["close"] if j else o0
            if use_trend and not ((ref > o0) if up else (ref < o0)):
                continue
            if use_fresh and not e["fresh"]:
                continue
            if use_range and hi - lo < range_min:
                continue
            sig.append(dict(day=d, j=j, side=e["side"], ientry=entry, rule="FILTERED"))
    return sig


def report(name, days, opt, sig, half):
    for slip in (0, 4):
        h = [[], []]
        for s in sig:
            r = ro.trade(days, opt, s, "cross", slip)
            if r is not None:
                h[s["day"] >= half].append(r["pts"])
        a = h[0] + h[1]
        if not a:
            continue
        net = [sum(p * ro.LOT for p in x) - ro.CHARGES * len(x) for x in h]
        print("%-44s slip %d | n %4d (H1 %3d / H2 %3d) | win %4.1f%% | pts H1 %+7.1f / H2 %+7.1f | "
              "net 1 lot Rs H1 %+8.0f / H2 %+8.0f / total %+8.0f"
              % (name, slip, len(a), len(h[0]), len(h[1]), 100 * sum(p > 0 for p in a) / len(a), sum(h[0]), sum(h[1]),
                 net[0], net[1], net[0] + net[1]))


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1)
    ds = sorted(days); half = ds[len(ds) // 2]
    print("days %d, halves split at %s; break-even win rate for +6/-11 before costs: 64.7 %%" % (len(ds), half))
    report("FILTERED (trend + fresh + range >= 300)", days, opt, filtered_signals(days), half)
    report("FIRST_TOUCH (no filter)", days, opt, filtered_signals(days, use_trend=False, use_fresh=False, use_range=False), half)
    report("trend only", days, opt, filtered_signals(days, use_fresh=False, use_range=False), half)
    report("fresh only", days, opt, filtered_signals(days, use_trend=False, use_range=False), half)
    report("range >= 300 only", days, opt, filtered_signals(days, use_trend=False, use_fresh=False), half)
    for rm in (200.0, 400.0):
        report("FILTERED with range >= %d" % rm, days, opt, filtered_signals(days, range_min=rm), half)


if __name__ == "__main__":
    main()
