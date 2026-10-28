#!/usr/bin/env python3
""""Fable-style" scalp: buy the ATM CE/PE right after a fast index move and sell 1-5 minutes later.
Real expired nearest-weekly ATM premiums (rolling, strike rolls bridged with delta 0.5). Local files only; no orders.

  python3 tools/backtest_spike_scalp.py INDEX_CSV OPTION_HISTORY_DIR {sensex|nifty}

Signal: index close moved >= X points over the last N minutes (N = 1, 2), up -> ATM CE, down -> ATM PE; one trade at a
time; 09:20-15:10. Entry at the option close of the signal minute ("same", the most generous) or of the next minute
("next"). Exits: fixed hold 1 / 2 / 3 / 5 min, or premium target / stop (stop checked first on a bar that hits both).
Day filters: all days, expiry day, day before expiry. Costs per round trip in index points: SENSEX 5 (lot 20),
NIFTY 1.5 (lot 65). Halves split at the middle day.
"""
import itertools, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

CFG = {"sensex": dict(th=(40, 60, 80, 100), cost=5.0, lot=20, tps=((10, 10), (20, 10), (6, 11))),
       "nifty": dict(th=(12, 18, 25, 33), cost=1.5, lot=65, tps=((3, 3), (6, 3), (2, 3.5)))}


def signals(days, X, N):
    out = []
    for d in sorted(days):
        bars = days[d]; busy = -1
        for j in range(N, len(bars) - 1):
            hm = bars[j]["t"].strftime("%H:%M")
            if j <= busy or hm < "09:20" or hm > "15:10":
                continue
            mv = bars[j]["close"] - bars[j - N]["close"]
            if abs(mv) >= X:
                out.append(dict(day=d, j=j, side="CE" if mv > 0 else "PE"))
                busy = j + 5
    return out


def trade(days, opt, s, entry_lag, exit_rule):
    bars = days[s["day"]]; leg = opt.get((s["day"], s["side"]))
    j0 = s["j"] + entry_lag
    if not leg or j0 >= len(bars):
        return None
    t0 = bars[j0]["timestampMs"] // 1000
    if t0 not in leg:
        return None
    K = leg[t0][0]; cs = 1 if s["side"] == "CE" else -1; e = leg[t0][4]

    def px(t):
        k2, o, h, l, c = leg[t]
        sh = 0.0 if k2 == K else 0.5 * (k2 - K) * cs
        return h + sh, l + sh, c + sh
    kind, a, bb = exit_rule
    last = e
    hold = a if kind == "hold" else 10
    for k in range(j0 + 1, min(len(bars), j0 + hold + 1)):
        t = bars[k]["timestampMs"] // 1000
        if t not in leg:
            continue
        h, l, c = px(t)
        if kind == "tp":
            if e - l >= bb:
                return -bb
            if h - e >= a:
                return a
        last = c
    return last - e


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); cfg = CFG[sys.argv[3]]
    exp = expiry_days(opt); ds = sorted(days); half = ds[len(ds) // 2]
    pre = {ds[i - 1] for i, d in enumerate(ds) if d in exp and i > 0}
    print("%s: days %d, expiry days %d, halves split at %s, cost %.1f pts/trade, lot %d"
          % (sys.argv[3].upper(), len(ds), len(exp), half, cfg["cost"], cfg["lot"]))
    exits = [("hold", m, None) for m in (1, 2, 3, 5)] + [("tp", a, bb) for a, bb in cfg["tps"]]
    filt = {"all days": lambda d: True, "expiry day": lambda d: d in exp, "day before expiry": lambda d: d in pre}
    res = []
    for X, N in itertools.product(cfg["th"], (1, 2)):
        sig = signals(days, X, N)
        for (fn, f), lag, ex in itertools.product(filt.items(), (0, 1), exits):
            h = [[], []]
            for s in sig:
                if not f(s["day"]):
                    continue
                p = trade(days, opt, s, lag, ex)
                if p is not None:
                    h[s["day"] >= half].append(p - cfg["cost"])
            if len(h[0]) < 15 or len(h[1]) < 15:
                continue
            a = h[0] + h[1]
            res.append((sum(h[0]) / len(h[0]), sum(h[1]) / len(h[1]), X, N, fn, "same" if lag == 0 else "next",
                        ex, len(h[0]), len(h[1]), 100 * sum(x > 0 for x in a) / len(a),
                        100 * sum(x + cfg["cost"] > 0 for x in a) / len(a), sum(h[0]) * cfg["lot"], sum(h[1]) * cfg["lot"]))
    print("combinations %d; positive after costs: H1 %d, H2 %d, BOTH %d"
          % (len(res), sum(r[0] > 0 for r in res), sum(r[1] > 0 for r in res), sum(r[0] > 0 and r[1] > 0 for r in res)))
    gross = [r for r in res if r[10] > 0]
    fmt = ("  move>=%3d in %d min %-17s entry %-4s exit %-14s | n %4d/%4d | win before costs %4.1f%% after %4.1f%% | "
           "avg net pts H1 %+5.1f / H2 %+5.1f | Rs/lot H1 %+8.0f / H2 %+8.0f")

    def show(r):
        ex = "hold %d min" % r[6][1] if r[6][0] == "hold" else "+%g/-%g" % (r[6][1], r[6][2])
        print(fmt % (r[2], r[3], r[4], r[5], ex, r[7], r[8], r[10], r[9], r[0], r[1], r[11], r[12]))
    print("\nBest 10 chosen on the FIRST half -> second half:")
    for r in sorted(res, key=lambda r: -r[0])[:10]:
        show(r)
    print("\nFable-like setting (move >= %d in 1 min, same-minute entry, 1-2 min hold), every day filter:" % cfg["th"][1])
    for r in res:
        if r[2] == cfg["th"][1] and r[3] == 1 and r[5] == "same" and r[6][0] == "hold" and r[6][1] in (1, 2):
            show(r)


if __name__ == "__main__":
    main()
