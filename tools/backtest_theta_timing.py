#!/usr/bin/env python3
"""Sell ATM straddle where the decay is fastest (from option_move_anatomy.py): two timing ideas, real expired premiums.

  python3 tools/backtest_theta_timing.py HISTORY_DIR

A  OVERNIGHT: sell the ATM straddle at the close of day d (15:20) and buy it back next morning (09:20/09:30/09:45/10:00)
   on the same expiry (never across an expiry). Captures the overnight premium release seen at the open.
B  EXPIRY AFTERNOON: on expiry day, sell the ATM straddle at 12:00/13:00/13:30/14:00/14:30 and buy back at 15:20,
   optional stop when the straddle rises 25 % / 50 %.
Fixed-strike value rebuilt each minute from the rolling ATM straddle with a Bachelier fit around F = K + CE - PE
(same method as backtest_short_straddle.py; 0.7 % median error at strike switches). Costs: 4 pts slippage per trade
and Rs 140 per trade (2 legs in, 2 out), lot 20. Halves split at 28 Apr 2026. Local files only; no orders.
"""
import datetime, math, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_real_options import load_options              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
LOT, CH, SLIP = 20, 140.0, 4.0
phi = lambda x: math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
Phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def rows_by_day(opt):
    D = {}
    for (d, side), leg in opt.items():
        for t, (k, o, h, l, c) in leg.items():
            D.setdefault(d, {}).setdefault(t, {})[side] = (k, c)
    out = {}
    for d, m in D.items():
        r = []
        for t in sorted(m):
            v = m[t]
            if len(v) == 2 and v["CE"][0] == v["PE"][0]:
                r.append(dict(hm=datetime.datetime.fromtimestamp(t, IST).strftime("%H:%M"), K=v["CE"][0],
                              ce=v["CE"][1], pe=v["PE"][1]))
        out[d] = r
    return out


def straddle_at(K, r):
    F = r["K"] + r["ce"] - r["pe"]; x = F - r["K"]; a = r["ce"] + r["pe"]
    lo, hi = 1e-3, max(3 * a, 1.0)
    for _ in range(40):
        s = (lo + hi) / 2; d = x / s
        if x * (2 * Phi(d) - 1) + 2 * s * phi(d) > a:
            hi = s
        else:
            lo = s
    d = (F - K) / s
    return (F - K) * (2 * Phi(d) - 1) + 2 * s * phi(d)


def at(R, hm):
    return next((r for r in R if r["hm"] >= hm), None)


def report(name, res, half):
    if not res:
        return
    h = [[p for d, p in res if d < half], [p for d, p in res if d >= half]]
    a = h[0] + h[1]; n = len(a)
    net = [sum(x) * LOT - CH * len(x) for x in h]
    print("%-46s n %3d win %4.1f%% avg %+6.1f worst %+6.0f | net 1 lot Rs H1 %+8.0f / H2 %+8.0f / total %+8.0f"
          % (name, n, 100 * sum(p > 0 for p in a) / n, sum(a) / n, min(a), net[0], net[1], net[0] + net[1]))


def main():
    opt = load_options(sys.argv[1], 1)
    exp = expiry_days(opt); R = rows_by_day(opt); days = sorted(R); half = days[len(days) // 2]
    print("days %d, expiry days %d, halves split at %s\n" % (len(days), len(exp), half))
    print("A. OVERNIGHT short straddle (sell 15:20, buy back next morning, same expiry)")
    for out in ("09:20", "09:30", "09:45", "10:00"):
        res = []
        for d, nx in zip(days, days[1:]):
            if d in exp:
                continue                                    # next morning would be a new expiry series
            e, x = at(R[d], "15:20"), at(R[nx], out)
            if not e or not x:
                continue
            K = e["K"]; prem = e["ce"] + e["pe"]
            res.append((d, prem - straddle_at(K, x) - SLIP))
        report("sell 15:20 -> buy back %s" % out, res, half)
    print("\nB. EXPIRY-DAY AFTERNOON short straddle (buy back 15:20)")
    for ent in ("12:00", "13:00", "13:30", "14:00", "14:30"):
        for stop in (0, 0.25, 0.5):
            res = []
            for d in sorted(exp):
                Rd = R.get(d) or []
                e = at(Rd, ent)
                if not e:
                    continue
                K = e["K"]; prem = e["ce"] + e["pe"]; pnl = None
                for r in Rd:
                    if r["hm"] <= e["hm"]:
                        continue
                    v = straddle_at(K, r)
                    if stop and v >= prem * (1 + stop):
                        pnl = prem - v - SLIP - 2; break
                    if r["hm"] >= "15:20":
                        pnl = prem - v - SLIP; break
                if pnl is None:
                    pnl = prem - straddle_at(K, Rd[-1]) - SLIP
                res.append((d, pnl))
            report("sell %s, stop %s" % (ent, "%d%%" % (stop * 100) if stop else "none"), res, half)


if __name__ == "__main__":
    main()
