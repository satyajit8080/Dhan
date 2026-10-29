#!/usr/bin/env python3
"""Only buy CE when the market is green (and PE when red)? FIRST_TOUCH and FILTERED crossings, 1 year, real ATM
premiums, +6/-11, 10-min stop, net = pts x 20 - Rs 70. Local files only; no orders.

  python3 tools/test_ce_when_green.py INDEX_CSV OPTION_HISTORY_DIR

"Green" known AT the crossing (tradeable): index above previous close / above today's open / both / previous day green.
"Day closes green" uses the day's final close (look-ahead, NOT tradeable): shown only to explain why it looks good
on a chart afterwards.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_filtered import filtered_signals              # noqa: E402
from loss_days_breakdown import all_signals                 # noqa: E402


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1)
    ds = sorted(days); half = ds[len(ds) // 2]
    prevc = {d: days[ds[i - 1]][-1]["close"] for i, d in enumerate(ds) if i}
    prevg = {d: days[ds[i - 1]][-1]["close"] > days[ds[i - 1]][0]["open"] for i, d in enumerate(ds) if i}
    for name, sig in (("FIRST_TOUCH", all_signals(days)), ("FILTERED", filtered_signals(days))):
        rows = []
        for s in sig:
            if s["day"] not in prevc:
                continue
            r = ro.trade(days, opt, s, "cross", 0)
            if r is None:
                continue
            bars = days[s["day"]]; px = bars[s["j"] - 1]["close"] if s["j"] else bars[0]["open"]
            g = dict(vs_prev=px > prevc[s["day"]], vs_open=px > bars[0]["open"], prev_day=prevg[s["day"]],
                     close_green=bars[-1]["close"] > prevc[s["day"]])
            g["both"] = g["vs_prev"] and g["vs_open"]; g["both_red"] = not g["vs_prev"] and not g["vs_open"]
            rows.append((s, g, r["pts"] * ro.LOT - ro.CHARGES))
        print("\n==== %s (%d trades)" % (name, len(rows)))
        tests = [("no filter (all CE and PE)", lambda s, g: True),
                 ("CE only, any day", lambda s, g: s["side"] == "CE"),
                 ("CE only when above prev close", lambda s, g: s["side"] == "CE" and g["vs_prev"]),
                 ("CE only when above today's open", lambda s, g: s["side"] == "CE" and g["vs_open"]),
                 ("CE only when above both", lambda s, g: s["side"] == "CE" and g["both"]),
                 ("CE only after a green previous day", lambda s, g: s["side"] == "CE" and g["prev_day"]),
                 ("CE when green + PE when red (both refs)", lambda s, g: (s["side"] == "CE" and g["both"]) or (s["side"] == "PE" and g["both_red"])),
                 ("LOOK-AHEAD: CE when day CLOSES green", lambda s, g: s["side"] == "CE" and g["close_green"])]
        for tn, f in tests:
            v = [(s["day"] >= half, x) for s, g, x in rows if f(s, g)]
            if not v:
                continue
            a = [x for _, x in v]; h1 = sum(x for k, x in v if not k); h2 = sum(x for k, x in v if k)
            print("  %-42s trades %4d  win %4.1f%%  net Rs %+9.0f  (H1 %+8.0f / H2 %+8.0f)  per trade %+5.0f"
                  % (tn, len(a), 100 * sum(x > 0 for x in a) / len(a), sum(a), h1, h2, sum(a) / len(a)))


if __name__ == "__main__":
    main()
