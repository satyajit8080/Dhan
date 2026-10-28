#!/usr/bin/env python3
"""Broad intraday rule search on 1 year of SENSEX 1-min bars + real expired ATM option premiums, with an honest check.

  python3 tools/backtest_everything.py candles.csv HISTORY_DIR

One decision per day per rule: at a fixed entry time, a rule says UP, DOWN or nothing; the trade is held for a fixed
time. Everything is a grid:
  entry time   every 15 min from 09:30 to 14:45
  hold         5, 15, 30, 60 min, or to 15:25
  instrument   ATM option bought at the entry-minute close (real premium; Rs 70 + 1.5 pts per trade)
               or SENSEX future (index points; Rs 400 + 2 pts per trade), lot 20
  rule         momentum over 5/15/30/60/120 min, price vs day open, opening gap, previous day's move, position in the
               day's range, rolling-ATM open-interest change (calls vs puts, the NSE-analyzer idea), each as
               FOLLOW and FADE, plus always-UP / always-DOWN
  filter       none, each weekday, expiry day, day before expiry, ATM straddle above / below its 20-day median
Selection uses the FIRST half only (before 28 Apr 2026); the best are then shown on the SECOND half. As a chance
baseline the same count is repeated with each day's outcomes randomly sign-flipped (no edge by construction).
Local files only; no orders, no API calls.
"""
import argparse, datetime, json, random, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402
from backtest_real_options import load_options              # noqa: E402

LOT = 20
COST = {"opt": (70.0, 1.5), "fut": (400.0, 2.0)}
ENTRIES = ["%02d:%02d" % divmod(m, 60) for m in range(9 * 60 + 30, 14 * 60 + 46, 15)]
HOLDS = [5, 15, 30, 60, "close"]


def load_oi(hdir):
    out = {}
    for f in Path(hdir).glob("OPT_1_ATM_*.json"):
        rec = json.load(open(f)); side = "CE" if rec["series"][2] == "CALL" else "PE"
        leg = rec["payload"].get(side.lower()) or {}
        out[(rec["from"], side)] = dict(zip(leg.get("timestamp") or [], leg.get("oi") or []))
    return out


def build(days, opt, oi, exp):
    ds = sorted(days); rows = []
    before = {ds[i - 1] for i, d in enumerate(ds) if d in exp and i > 0}
    strad = {}
    for d in ds:                                            # 09:30 ATM straddle per day (for the IV-regime filter)
        t = next((x["timestampMs"] // 1000 for x in days[d] if x["t"].strftime("%H:%M") == "09:30"), None)
        c, p = opt.get((d, "CE"), {}).get(t), opt.get((d, "PE"), {}).get(t)
        strad[d] = (c[4] + p[4]) if c and p else None
    for n, d in enumerate(ds):
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        prev = days[ds[n - 1]] if n else None
        hist = [strad[x] for x in ds[max(0, n - 20):n] if strad.get(x)]
        med = sorted(hist)[len(hist) // 2] if len(hist) >= 10 else None
        for et in ENTRIES:
            j = idx.get(et)
            if j is None or j < 121 - 15 and et != "09:30" and j < 5:
                continue
            S = bars[j]["close"]; t0 = bars[j]["timestampMs"] // 1000
            mom = {L: (S - bars[j - L]["close"]) if j - L >= 0 else None for L in (5, 15, 30, 60, 120)}
            hi = max(x["high"] for x in bars[:j + 1]); lo = min(x["low"] for x in bars[:j + 1])
            ce_oi, pe_oi = oi.get((d, "CE"), {}), oi.get((d, "PE"), {})
            t_open = bars[0]["timestampMs"] // 1000
            oic = None
            if t0 in ce_oi and t0 in pe_oi and t_open in ce_oi and t_open in pe_oi:
                oic = (pe_oi[t0] - pe_oi[t_open]) - (ce_oi[t0] - ce_oi[t_open])     # > 0: more put writing -> UP
            f = dict(day=d, et=et, wd=datetime.date.fromisoformat(d).weekday(), exp=d in exp, pre=d in before,
                     ivhi=None if med is None or strad[d] is None else strad[d] > med,
                     mom=mom, vsopen=S - bars[0]["open"],
                     gap=(bars[0]["open"] - prev[-1]["close"]) if prev else None,
                     prevday=(prev[-1]["close"] - prev[0]["open"]) if prev else None,
                     rangepos=(S - lo) / (hi - lo) - 0.5 if hi > lo else None, oic=oic)
            pnl = {}
            for h in HOLDS:
                k = (idx.get("15:25") if h == "close" else j + h)
                if k is None or k >= len(bars) or k <= j:
                    continue
                pnl[("fut", "UP", h)] = bars[k]["close"] - S
                pnl[("fut", "DOWN", h)] = S - bars[k]["close"]
                tk = bars[k]["timestampMs"] // 1000
                for side, dr in (("CE", "UP"), ("PE", "DOWN")):
                    leg = opt.get((d, side), {})
                    if t0 in leg and tk in leg:
                        K = leg[t0][0]; k2 = leg[tk][0]; cs = 1 if side == "CE" else -1
                        exitp = leg[tk][4] + (0.0 if k2 == K else 0.5 * (k2 - K) * cs)
                        pnl[("opt", dr, h)] = exitp - leg[t0][4]
            rows.append((f, pnl))
    return rows, ds[len(ds) // 2]


def sgn(x):
    return None if x is None or x == 0 else (1 if x > 0 else -1)


RULES = {"always UP": lambda f: 1, "always DOWN": lambda f: -1}
for L in (5, 15, 30, 60, 120):
    RULES["momentum %d min" % L] = (lambda L: lambda f: sgn(f["mom"][L]))(L)
RULES["vs day open"] = lambda f: sgn(f["vsopen"])
RULES["opening gap"] = lambda f: sgn(f["gap"])
RULES["previous day"] = lambda f: sgn(f["prevday"])
RULES["range position"] = lambda f: sgn(f["rangepos"]) if f["rangepos"] is not None and abs(f["rangepos"]) > 1 / 6 else None
RULES["OI change (puts-calls)"] = lambda f: sgn(f["oic"])
FILTERS = {"all days": lambda f: True, "expiry day": lambda f: f["exp"], "day before expiry": lambda f: f["pre"],
           "high IV": lambda f: f["ivhi"] is True, "low IV": lambda f: f["ivhi"] is False}
for w, nm in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri")):
    FILTERS[nm] = (lambda w: lambda f: f["wd"] == w)(w)


def evaluate(rows, half, flip=None):
    res = []
    for rn, rule in RULES.items():
        modes = (("follow", 1),) if rn.startswith("always") else (("follow", 1), ("fade", -1))
        dirs = [rule(f) for f, _ in rows]
        for mode, m in modes:
            for fn, flt in FILTERS.items():
                keep = [i for i, (f, _) in enumerate(rows) if dirs[i] and flt(f)]
                for inst in ("opt", "fut"):
                    ch, sl = COST[inst]
                    for et in ENTRIES:
                        for h in HOLDS:
                            hh = [[], []]
                            for i in keep:
                                f, pnl = rows[i]
                                if f["et"] != et:
                                    continue
                                dr = "UP" if dirs[i] * m > 0 else "DOWN"
                                p = pnl.get((inst, dr, h))
                                if p is None:
                                    continue
                                if flip is not None and flip[f["day"]]:
                                    p = pnl.get((inst, "DOWN" if dr == "UP" else "UP", h), p)
                                hh[f["day"] >= half].append(p - sl)
                            if len(hh[0]) < 20 or len(hh[1]) < 20:
                                continue
                            net = [sum(x) * LOT - ch * len(x) for x in hh]
                            win = [100 * sum(p > 0 for p in x) / len(x) for x in hh]
                            res.append((net[0], net[1], rn, mode, fn, inst, et, h, len(hh[0]), len(hh[1]), win[0], win[1]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile"); ap.add_argument("hdir")
    a = ap.parse_args()
    days = b.load(a.csvfile); opt = load_options(a.hdir, 1); oi = load_oi(a.hdir)
    rows, half = build(days, opt, oi, expiry_days(opt))
    print("decision points %d (days x entry times), halves split at %s" % (len(rows), half))
    res = evaluate(rows, half)
    n = len(res); both = [r for r in res if r[0] > 0 and r[1] > 0]
    print("combinations with >= 20 trades in each half: %d" % n)
    print("positive after costs: first half %d, second half %d, BOTH %d (%.2f%%)"
          % (sum(r[0] > 0 for r in res), sum(r[1] > 0 for r in res), len(both), 100 * len(both) / n))
    random.seed(7)
    base = []
    for _ in range(3):
        flip = {d: random.random() < 0.5 for d in days}
        rr = evaluate(rows, half, flip)
        base.append(100 * sum(r[0] > 0 and r[1] > 0 for r in rr) / len(rr))
    print("chance baseline (random day sign flips, 3 runs): positive in both halves %s %%" % ", ".join("%.2f" % x for x in base))
    fmt = "%-24s %-6s %-17s %-3s %s hold %-5s | H1 n %3d win %4.1f%% net Rs %+8.0f | H2 n %3d win %4.1f%% net Rs %+8.0f"
    print("\nTop 20 chosen on the FIRST half only -> their SECOND-half result:")
    for r in sorted(res, key=lambda r: -r[0])[:20]:
        print(fmt % (r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[10], r[0], r[9], r[11], r[1]))
    h2pos = sum(1 for r in sorted(res, key=lambda r: -r[0])[:100] if r[1] > 0)
    print("of the top 100 first-half picks, %d were positive in the second half" % h2pos)
    print("\nPositive in both halves, best 20 by the weaker half:")
    for r in sorted(both, key=lambda r: -min(r[0], r[1]))[:20]:
        print(fmt % (r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[10], r[0], r[9], r[11], r[1]))


if __name__ == "__main__":
    main()
