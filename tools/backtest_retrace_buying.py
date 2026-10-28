#!/usr/bin/env python3
"""Retracement (pullback) entry for option BUYING: wait for an index impulse, let it pull back, buy when it resumes.
Real expired nearest-weekly ATM premiums (rolling, strike rolls bridged with delta 0.5). Local files only; no orders.

  python3 tools/backtest_retrace_buying.py INDEX_CSV OPTION_HISTORY_DIR {sensex|nifty}

Impulse   within the last W minutes (15 / 30) the index close rose >= X points from its lowest close to its highest
          close (low first) -> up impulse (CE); the mirror -> down impulse (PE)
Pullback  the close then gives back >= R of the impulse (38.2 / 50 / 61.8 %) without closing beyond the impulse start
Entry     "touch":  at the close of the pullback minute
          "resume": first later minute (within 10) whose close is beyond the previous minute's high (CE) / low (PE);
                    cancelled if the index closes beyond the impulse start first
          entry at that minute's option close (lag 0) or the next minute's (lag 1)
Filter    all signals, or only with the day trend (index above today's open for CE, below for PE)
Exits     hold 5 / 10 / 15 min, or premium target / stop with a 10-min time stop (backtest_spike_scalp.trade)
One trade at a time; 09:30-14:45. Costs per round trip: SENSEX 5 pts (lot 20), NIFTY 1.5 pts (lot 65).
Halves split at the middle day; a combination counts only if positive after costs in BOTH halves.
"""
import itertools, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_spike_scalp import trade                      # noqa: E402

CFG = {"sensex": dict(th=(80, 120, 160, 220), cost=5.0, lot=20, tps=((10, 10), (20, 10), (20, 15), (6, 11))),
       "nifty": dict(th=(25, 35, 50, 70), cost=1.5, lot=65, tps=((3, 3), (6, 3), (6, 5), (2, 3.5)))}


def signals(days, X, W, R, mode, trend):
    out = []
    for d in sorted(days):
        bars = days[d]; c = [x["close"] for x in bars]; o0 = bars[0]["open"]; busy = -1; armed = None
        for j in range(W, len(bars) - 2):
            hm = bars[j]["t"].strftime("%H:%M")
            if hm < "09:30" or hm > "14:45":
                continue
            if armed:
                sg, start, until = armed
                if (c[j] - start) * sg <= 0 or j > until:
                    armed = None
                elif (c[j] > bars[j - 1]["high"]) if sg > 0 else (c[j] < bars[j - 1]["low"]):
                    armed = None
                    if j > busy and (not trend or (c[j] - o0) * sg > 0):
                        out.append(dict(day=d, j=j, side="CE" if sg > 0 else "PE")); busy = j + 15
                continue
            if j <= busy:
                continue
            win = range(j - W, j)
            for sg in (1, -1):
                b_ = max(win, key=lambda k: c[k] * sg)                 # impulse extreme
                a_ = min(range(j - W, b_ + 1), key=lambda k: c[k] * sg)  # impulse start (before the extreme)
                imp = (c[b_] - c[a_]) * sg
                if imp < X or b_ == a_:
                    continue
                back = (c[b_] - c[j]) * sg
                if back >= R * imp and (c[j] - c[a_]) * sg > 0:
                    if mode == "touch":
                        if j > busy and (not trend or (c[j] - o0) * sg > 0):
                            out.append(dict(day=d, j=j, side="CE" if sg > 0 else "PE")); busy = j + 15
                    else:
                        armed = (sg, c[a_], j + 10)
                    break
    return out


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); cfg = CFG[sys.argv[3]]
    ds = sorted(days); half = ds[len(ds) // 2]
    print("%s: days %d, halves split at %s, cost %.1f pts/trade, lot %d" % (sys.argv[3].upper(), len(ds), half, cfg["cost"], cfg["lot"]))
    exits = [("hold", m, None) for m in (5, 10, 15)] + [("tp", a, bb) for a, bb in cfg["tps"]]
    res = []
    for X, W, R, mode, tr in itertools.product(cfg["th"], (15, 30), (0.382, 0.5, 0.618), ("touch", "resume"), (False, True)):
        sig = signals(days, X, W, R, mode, tr)
        for lag, ex in itertools.product((0, 1), exits):
            h = [[], []]
            for s in sig:
                p = trade(days, opt, s, lag, ex)
                if p is not None:
                    h[s["day"] >= half].append(p - cfg["cost"])
            if len(h[0]) < 15 or len(h[1]) < 15:
                continue
            a = h[0] + h[1]
            res.append(dict(m=(sum(h[0]) / len(h[0]), sum(h[1]) / len(h[1])), key=(X, W, R, mode, tr, lag, ex), n=(len(h[0]), len(h[1])),
                            win=100 * sum(x > 0 for x in a) / len(a), wgross=100 * sum(x + cfg["cost"] > 0 for x in a) / len(a),
                            rs=(sum(h[0]) * cfg["lot"], sum(h[1]) * cfg["lot"])))
    print("combinations %d; positive after costs: H1 %d, H2 %d, BOTH %d; average win rate %.1f%% (before costs %.1f%%)"
          % (len(res), sum(r["m"][0] > 0 for r in res), sum(r["m"][1] > 0 for r in res),
             sum(r["m"][0] > 0 and r["m"][1] > 0 for r in res), sum(r["win"] for r in res) / len(res), sum(r["wgross"] for r in res) / len(res)))

    def show(r):
        X, W, R, mode, tr, lag, ex = r["key"]
        e = "hold %d min" % ex[1] if ex[0] == "hold" else "+%g/-%g" % (ex[1], ex[2])
        print("  impulse>=%3d/%2dm pullback %4.1f%% %-6s %-9s lag %d exit %-11s | n %4d/%4d | win %4.1f%% (gross %4.1f%%) | "
              "avg net pts H1 %+5.1f / H2 %+5.1f | Rs/lot H1 %+8.0f / H2 %+8.0f"
              % (X, W, 100 * R, mode, "with-trend" if tr else "all", lag, e, r["n"][0], r["n"][1], r["win"], r["wgross"],
                 r["m"][0], r["m"][1], r["rs"][0], r["rs"][1]))
    print("\nBest 10 chosen on the FIRST half -> second half:")
    for r in sorted(res, key=lambda r: -r["m"][0])[:10]:
        show(r)
    both = [r for r in res if r["m"][0] > 0 and r["m"][1] > 0]
    if both:
        print("\nPositive in both halves:")
        for r in sorted(both, key=lambda r: -min(r["m"]))[:10]:
            show(r)


if __name__ == "__main__":
    main()
