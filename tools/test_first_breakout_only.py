#!/usr/bin/env python3
"""One trade a day: only the FIRST breakout of the day, then nothing. 1 year, real ATM premiums, 10-min time stop,
net = pts x 20 - Rs 70. Local files only; no orders.

  python3 tools/test_first_breakout_only.py INDEX_CSV OPTION_HISTORY_DIR

First crossing of the day from FIRST_TOUCH (all levels) and from FILTERED; optionally only after 09:30 / 10:00.
Exits (premium pts): +6/-11 (current), +10/-10, +15/-10, +20/-10, +30/-15. Entry at the crossing ("cross", the same
fill model as all earlier tests) and at that minute's close ("close", one step later).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_filtered import filtered_signals              # noqa: E402
from loss_days_breakdown import all_signals                 # noqa: E402


def first_per_day(sig, days, after):
    seen = set(); out = []
    for s in sig:
        if s["day"] in seen or days[s["day"]][s["j"]]["t"].strftime("%H:%M") < after:
            continue
        seen.add(s["day"]); out.append(s)
    return out


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1)
    ds = sorted(days); half = ds[len(ds) // 2]
    base = {"FIRST_TOUCH": all_signals(days), "FILTERED": filtered_signals(days)}
    print("days %d, halves split at %s" % (len(ds), half))
    for name, sig in base.items():
        for after in ("09:15", "09:30", "10:00"):
            first = first_per_day(sig, days, after)
            for tp, sl in ((6, 11), (10, 10), (15, 10), (20, 10), (30, 15)):
                ro.TP, ro.SL = tp, sl
                for mode in ("cross", "close"):
                    h = [[], []]
                    for s in first:
                        r = ro.trade(days, opt, s, mode, 0)
                        if r is not None:
                            h[s["day"] >= half].append(r["pts"] * ro.LOT - ro.CHARGES)
                    a = h[0] + h[1]
                    if not a:
                        continue
                    print("  %-11s first after %s  exit +%2d/-%2d  entry %-5s | days %3d  win %4.1f%% | net Rs H1 %+7.0f / H2 %+7.0f / total %+7.0f"
                          % (name, after, tp, sl, mode, len(a), 100 * sum(x > 0 for x in a) / len(a), sum(h[0]), sum(h[1]), sum(a)))


if __name__ == "__main__":
    main()
