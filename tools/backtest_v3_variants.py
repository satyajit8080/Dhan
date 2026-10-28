#!/usr/bin/env python3
"""Search for a profitable variant of scalping rules v3, with an honest out-of-sample check.

  python3 tools/backtest_v3_variants.py candles.csv HISTORY_DIR

Signals: v3 entry (1-min close beyond the trigger, next candle holds) from backtest_v3_rules.py, all levels >= 2
touches, no-trade windows applied, expiry days kept but flagged. Each signal is traded under a grid of
  instrument  ATM option (real premium, Rs 70/trade, slippage 1.5) or SENSEX future (index pts, Rs 400/trade, slip 2)
  stop        close back through the level | fixed (option premium pts or index pts)
  target      k x stop distance (index) | fixed premium/index pts
  hold        5 / 10 / 20 / 30 min
  filters     side, fresh/old level, time of day, with/against the move since the open, first signal of the day,
              expiry day / day before, extreme (new day high/low)
Selection: every combination is ranked on the FIRST half only (27 Oct 2025 - 27 Apr 2026, at least 40 trades);
the top 15 are then shown on the SECOND half, which was not used to pick them. Local files only; no orders.
"""
import argparse, itertools, pickle, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
from backtest_real_options import load_options              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

LOT = 20
COST = {"opt": (70.0, 1.5), "fut": (400.0, 2.0)}           # Rs per trade, slippage pts per trade


def all_signals(days, exp):
    out = []
    ds = sorted(days)
    before_exp = {ds[i - 1] for i, d in enumerate(ds) if d in exp and i > 0}
    for d in ds:
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        first = True
        for e in b.day_events(bars):
            j = idx[e["time"]]; up = e["side"] == "CE"; sg = 1 if up else -1
            trig = e["trigger"]; lvl = trig - 5 * sg
            k = next((k for k in range(j, min(len(bars), j + 3)) if (bars[k]["close"] - trig) * sg > 0), None)
            if k is None or k + 1 >= len(bars) or (bars[k + 1]["close"] - trig) * sg <= 0:
                continue
            q = k + 1
            if b.rt.in_skip_window(bars[q]["t"]):
                continue
            hi = max(x["high"] for x in bars[:q + 1]); lo = min(x["low"] for x in bars[:q + 1])
            out.append(dict(day=d, q=q, side=e["side"], lvl=lvl, ient=bars[q]["close"], fresh=e["touches"] < 3,
                            t=bars[q]["t"].strftime("%H:%M"), trend=(bars[q]["close"] - bars[0]["open"]) * sg > 0,
                            first=first, exp=d in exp, pre=d in before_exp,
                            extreme=(bars[q]["high"] >= hi) if up else (bars[q]["low"] <= lo)))
            first = False
    return out


def path(days, opt, s):
    """Per-minute (index high, low, close, option high, low, close) for 30 min after entry; option None if missing."""
    bars = days[s["day"]]; leg = opt.get((s["day"], s["side"])) or {}
    t0 = bars[s["q"]]["timestampMs"] // 1000
    K = leg[t0][0] if t0 in leg else None; cs = 1 if s["side"] == "CE" else -1
    rows = []
    for k in range(s["q"], min(len(bars), s["q"] + 31)):
        x = bars[k]; t = x["timestampMs"] // 1000
        o = None
        if K is not None and t in leg:
            k2, oo, h, l, c = leg[t]; sh = 0.0 if k2 == K else 0.5 * (k2 - K) * cs
            o = (h + sh, l + sh, c + sh)
        rows.append((x["high"], x["low"], x["close"], o))
    return rows


def sim(s, rows, inst, stop, tgt, hold):
    sg = 1 if s["side"] == "CE" else -1
    risk = (s["ient"] - s["lvl"]) * sg
    if inst == "opt":
        if rows[0][3] is None:
            return None
        entry = rows[0][3][2]
    else:
        entry = s["ient"]
    last = entry
    for x in rows[1:hold + 1]:
        ih, il, ic, o = x
        if inst == "opt":
            if o is None:
                continue
            ph, pl, pc = o
            fav, adv, cur = ph - entry, entry - pl, pc - entry
            ifav = (ih - s["ient"]) * sg
        else:
            fav = ((ih if sg > 0 else -il) - (s["ient"] if sg > 0 else -s["ient"]))
            adv = ((s["ient"] if sg > 0 else -s["ient"]) - (il if sg > 0 else -ih))
            cur = (ic - s["ient"]) * sg; ifav = fav
        # stop first (conservative)
        if stop[0] == "close":
            if (ic - s["lvl"]) * sg < 0:
                return cur
        elif adv >= stop[1]:
            return -stop[1]
        if tgt[0] == "rr":
            if ifav >= tgt[1] * risk:
                return min(fav, (0.5 if inst == "opt" else 1.0) * tgt[1] * risk)
        elif fav >= tgt[1]:
            return tgt[1]
        last = cur
    return last


FILTERS = {
    "all": lambda s: True,
    "fresh": lambda s: s["fresh"], "old": lambda s: not s["fresh"],
    "CE": lambda s: s["side"] == "CE", "PE": lambda s: s["side"] == "PE",
    "am<11:30": lambda s: s["t"] < "11:30", "pm>=13:00": lambda s: s["t"] >= "13:00",
    "with-trend": lambda s: s["trend"], "counter-trend": lambda s: not s["trend"],
    "first/day": lambda s: s["first"], "no-expiry-day": lambda s: not s["exp"],
    "expiry-day": lambda s: s["exp"], "day-before-expiry": lambda s: s["pre"],
    "extreme": lambda s: s["extreme"], "with-trend+extreme": lambda s: s["trend"] and s["extreme"],
    "with-trend+am": lambda s: s["trend"] and s["t"] < "11:30",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile"); ap.add_argument("hdir"); ap.add_argument("--cache")
    a = ap.parse_args()
    if a.cache and Path(a.cache).exists():
        sig, rows, half = pickle.load(open(a.cache, "rb"))
    else:
        days = b.load(a.csvfile); opt = load_options(a.hdir, 1)
        sig = all_signals(days, expiry_days(opt))
        rows = [path(days, opt, s) for s in sig]
        ds = sorted(days); half = ds[len(ds) // 2]
        if a.cache:
            pickle.dump((sig, rows, half), open(a.cache, "wb"))
    print("signals %d, halves split at %s" % (len(sig), half))
    grids = {
        "opt": dict(stop=[("close",), ("fix", 6), ("fix", 8), ("fix", 11), ("fix", 15)],
                    tgt=[("rr", 1.0), ("rr", 1.5), ("rr", 2.0), ("rr", 3.0), ("fix", 6), ("fix", 10), ("fix", 15), ("fix", 25)]),
        "fut": dict(stop=[("close",), ("fix", 15), ("fix", 25), ("fix", 40)],
                    tgt=[("rr", 1.0), ("rr", 2.0), ("rr", 3.0), ("fix", 30), ("fix", 50), ("fix", 80)]),
    }
    res = []
    for inst, g in grids.items():
        ch, sl = COST[inst]
        for stop, tgt, hold in itertools.product(g["stop"], g["tgt"], (5, 10, 20, 30)):
            pnl = [sim(s, r, inst, stop, tgt, hold) for s, r in zip(sig, rows)]
            for fname, f in FILTERS.items():
                h = [[], []]
                for s, p in zip(sig, pnl):
                    if p is not None and f(s):
                        h[s["day"] >= half].append(p - sl)
                if len(h[0]) < 40:
                    continue
                net = [sum(x) * LOT - ch * len(x) for x in h]
                win = [100 * sum(p > 0 for p in x) / max(1, len(x)) for x in h]
                res.append((net[0], net[1], inst, stop, tgt, hold, fname, len(h[0]), len(h[1]), win[0], win[1]))
    print("combinations tested: %d" % len(res))
    print("positive after costs in first half: %d; in second half: %d; in BOTH halves: %d"
          % (sum(r[0] > 0 for r in res), sum(r[1] > 0 for r in res), sum(r[0] > 0 and r[1] > 0 for r in res)))
    print("\nTop 15 chosen on the FIRST half only, then checked on the second half:")
    for r in sorted(res, key=lambda r: -r[0])[:15]:
        print("%-3s stop %-10s target %-10s hold %2d %-20s | H1 n %3d win %4.1f%% net Rs %+8.0f | H2 n %3d win %4.1f%% net Rs %+8.0f"
              % (r[2], "/".join(map(str, r[3])), "/".join(map(str, r[4])), r[5], r[6], r[7], r[9], r[0], r[8], r[10], r[1]))
    both = sorted([r for r in res if r[0] > 0 and r[1] > 0], key=lambda r: -min(r[0], r[1]))
    print("\nPositive in both halves (best 15 by the weaker half):")
    for r in both[:15]:
        print("%-3s stop %-10s target %-10s hold %2d %-20s | H1 n %3d win %4.1f%% net Rs %+8.0f | H2 n %3d win %4.1f%% net Rs %+8.0f"
              % (r[2], "/".join(map(str, r[3])), "/".join(map(str, r[4])), r[5], r[6], r[7], r[9], r[0], r[8], r[10], r[1]))


if __name__ == "__main__":
    main()
