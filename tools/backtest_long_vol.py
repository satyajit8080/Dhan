#!/usr/bin/env python3
"""Option BUYING built on the mechanism: a bought option only wins when the realised move beats the premium, so buy
the ATM straddle (no direction bet) when it is cheap versus how much SENSEX has actually been moving.

  python3 tools/backtest_long_vol.py candles.csv HISTORY_DIR

Entry 09:30 or 10:00 (also 13:00); buy the ATM straddle (real expired premiums, fixed strike rebuilt with the
Bachelier fit of backtest_theta_timing.py). Conditions:
  all days | cheap: straddle / (average high-low range of the previous 10 days) below 0.35 / 0.45 / 0.55
  | wide open: the first 15 min range (09:15-09:30) above 1.5x / 2x its 10-day average  | both
by days to expiry (any, 0, 1, 2+). Exit: 15:20, or take profit +20 % / +40 % with stop -20 %.
Costs: 4 pts slippage + Rs 140 per trade, lot 20. Halves split at 28 Apr 2026; local files only; no orders.
"""
import itertools, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
from backtest_real_options import load_options              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402
from backtest_theta_timing import rows_by_day, straddle_at, at, LOT, CH, SLIP   # noqa: E402


def main():
    days_ix = b.load(sys.argv[1]); opt = load_options(sys.argv[2], 1)
    exp = sorted(expiry_days(opt)); R = rows_by_day(opt)
    ds = sorted(d for d in days_ix if d in R); half = ds[len(ds) // 2]
    rng = {d: max(x["high"] for x in days_ix[d]) - min(x["low"] for x in days_ix[d]) for d in ds}
    orng = {d: max(x["high"] for x in days_ix[d] if x["t"].strftime("%H:%M") < "09:30")
            - min(x["low"] for x in days_ix[d] if x["t"].strftime("%H:%M") < "09:30") for d in ds}
    dte = {d: sum(1 for x in ds if d <= x < next((e for e in exp if e >= d), "9999")) for d in ds}
    trades = []
    for n, d in enumerate(ds):
        if n < 10:
            continue
        avg_r = sum(rng[x] for x in ds[n - 10:n]) / 10; avg_o = sum(orng[x] for x in ds[n - 10:n]) / 10
        for ent in ("09:30", "10:00", "13:00"):
            e = at(R[d], ent)
            if not e:
                continue
            K = e["K"]; prem = e["ce"] + e["pe"]
            path = [(r["hm"], straddle_at(K, r)) for r in R[d] if r["hm"] > e["hm"]]
            if not path:
                continue
            res = {}
            for tp, sl in ((None, None), (0.2, 0.2), (0.4, 0.2)):
                pnl = None
                for hm, v in path:
                    if tp and v >= prem * (1 + tp):
                        pnl = v - prem - SLIP; break
                    if sl and v <= prem * (1 - sl):
                        pnl = v - prem - SLIP - 2; break
                    if hm >= "15:20":
                        pnl = v - prem - SLIP; break
                res[(tp, sl)] = pnl if pnl is not None else path[-1][1] - prem - SLIP
            trades.append(dict(day=d, ent=ent, dte=dte[d], cheap=prem / avg_r, wide=orng[d] / avg_o, res=res))
    conds = {"all days": lambda t: True}
    for c in (0.35, 0.45, 0.55):
        conds["cheap < %.2f" % c] = (lambda c: lambda t: t["cheap"] < c)(c)
    for w in (1.5, 2.0):
        conds["wide open > %.1fx" % w] = (lambda w: lambda t: t["wide"] > w)(w)
        conds["cheap<0.45 & wide>%.1fx" % w] = (lambda w: lambda t: t["cheap"] < 0.45 and t["wide"] > w)(w)
    dtes = {"any DTE": lambda t: True, "expiry day": lambda t: t["dte"] == 0, "1 day before": lambda t: t["dte"] == 1,
            "2+ days": lambda t: t["dte"] >= 2}
    out = []
    for (cn, cf), (dn, df), ent, ex in itertools.product(conds.items(), dtes.items(), ("09:30", "10:00", "13:00"),
                                                         ((None, None), (0.2, 0.2), (0.4, 0.2))):
        g = [t for t in trades if t["ent"] == ent and cf(t) and df(t)]
        h = [[t["res"][ex] for t in g if t["day"] < half], [t["res"][ex] for t in g if t["day"] >= half]]
        if len(h[0]) < 10 or len(h[1]) < 10:
            continue
        net = [sum(x) * LOT - CH * len(x) for x in h]
        a = h[0] + h[1]
        out.append((net[0], net[1], cn, dn, ent, ex, len(h[0]), len(h[1]), 100 * sum(p > 0 for p in a) / len(a)))
    print("long straddle combinations (>= 10 trades per half): %d; positive in H1 %d, H2 %d, BOTH %d"
          % (len(out), sum(o[0] > 0 for o in out), sum(o[1] > 0 for o in out), sum(o[0] > 0 and o[1] > 0 for o in out)))
    base = [o for o in out if o[2] == "all days" and o[3] == "any DTE" and o[5] == (None, None)]
    print("\nbaseline (buy every day, hold to 15:20):")
    for o in base:
        print("  entry %s | n %d/%d win %.1f%% | net Rs H1 %+.0f / H2 %+.0f" % (o[4], o[6], o[7], o[8], o[0], o[1]))
    print("\nbest 15 by the weaker half:")
    for o in sorted(out, key=lambda o: -min(o[0], o[1]))[:15]:
        ex = "hold to 15:20" if o[5][0] is None else "TP +%d%% / SL -%d%%" % (o[5][0] * 100, o[5][1] * 100)
        print("  %-24s %-12s %s %-18s | n %3d/%3d win %4.1f%% | net Rs H1 %+8.0f / H2 %+8.0f"
              % (o[2], o[3], o[4], ex, o[6], o[7], o[8], o[0], o[1]))


if __name__ == "__main__":
    main()
