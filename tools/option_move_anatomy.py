#!/usr/bin/env python3
"""How, when and why SENSEX ATM weekly options move: 1 year of real expired-option 1-min data (rolling ATM, nearest
weekly). Local files only; no orders.

  python3 tools/option_move_anatomy.py HISTORY_DIR

For every 15-min window it splits the change of the ATM CALL and PUT into
  delta part  = delta x index change (delta from the call/put split of the straddle, ~0.5)
  vega part   = vega x IV change (vega ~ straddle / (2 x IV) per side, Bachelier approximation)
  rest        = time decay + everything else (residual)
grouped by time of day and by trading days to expiry (0 = expiry day). It also reports how far the index must move
in 15 min for a fresh ATM buyer to break even, and how often it actually does.
"""
import json, sys, datetime
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_real_options import load_options              # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def main():
    hdir = sys.argv[1]
    opt = load_options(hdir, 1)
    iv, spot = {}, {}
    for f in Path(hdir).glob("OPT_1_ATM_*.json"):
        rec = json.load(open(f)); side = "CE" if rec["series"][2] == "CALL" else "PE"
        leg = rec["payload"].get(side.lower()) or {}
        ts = leg.get("timestamp") or []
        iv[(rec["from"], side)] = dict(zip(ts, leg.get("iv") or []))
        spot[(rec["from"], side)] = dict(zip(ts, leg.get("spot") or []))
    exp = sorted(expiry_days(opt)); days = sorted({d for d, _ in opt})
    dte = {}
    for d in days:
        nxt = next((e for e in exp if e >= d), None)
        dte[d] = None if nxt is None else sum(1 for x in days if d <= x < nxt)
    agg = defaultdict(lambda: defaultdict(float)); be = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for d in days:
        C, P = opt.get((d, "CE")), opt.get((d, "PE"))
        if not C or not P or dte[d] is None:
            continue
        g = min(dte[d], 4)
        ts = sorted(set(C) & set(P))
        for a, b2 in zip(ts, ts[15:]):
            if b2 - a != 900 or C[a][0] != C[b2][0] or P[a][0] != P[b2][0]:
                continue                                        # same strike for the whole window only
            hm = datetime.datetime.fromtimestamp(a, IST).strftime("%H:%M")
            if hm[3:] not in ("00", "15", "30", "45") or hm > "15:00":
                continue
            S0, S1 = spot[(d, "CE")].get(a), spot[(d, "CE")].get(b2)
            v0, v1 = iv[(d, "CE")].get(a), iv[(d, "CE")].get(b2)
            if not S0 or not S1:
                continue
            c0, c1, p0, p1 = C[a][4], C[b2][4], P[a][4], P[b2][4]
            st = c0 + p0
            dc = 0.5 + (c0 - p0) / (2 * st) * 0.8 if st > 0 else 0.5    # rough ATM call delta
            dS = S1 - S0
            vega = st / 2 / v0 if v0 and v0 > 0 else 0.0                  # per IV point, per side (Bachelier)
            dv = (v1 - v0) if v0 and v1 else 0.0
            for side, x0, x1, de in (("CE", c0, c1, dc), ("PE", p0, p1, dc - 1)):
                tot = x1 - x0; dpart = de * dS; vpart = vega * dv
                k = (g, hm[:2] + (":00" if hm[3:] < "30" else ":30"))
                A = agg[k]
                A["n"] += 1; A["tot"] += tot; A["delta"] += dpart; A["vega"] += vpart; A["rest"] += tot - dpart - vpart
                A["absS"] += abs(dS); A["prem"] += x0
            # breakeven for a fresh ATM buyer over 15 min: needed index move = (decay + vega drift) / 0.5
            need = -((c1 + p1) - (c0 + p0) - 0) / 1.0          # straddle loss when the index does not move ~ decay
            B = be[(g, hm[:2])]
            B[0] += 1; B[1] += abs(dS) > 0; B[2] += abs(dS); B[3] += (c1 + p1) - (c0 + p0)
    print("ATM option change per 15 min, averaged per side (CE and PE pooled), premium points")
    print("DTE = trading days to expiry (0 = expiry day; 4 = 4 or more)")
    print("%-3s %-5s %6s %8s %8s %8s %8s %8s %9s" % ("DTE", "time", "n", "premium", "total", "delta", "IV", "decay+", "|index|"))
    for k in sorted(agg):
        A = agg[k]; n = A["n"]
        print("%-3d %-5s %6d %8.1f %+8.2f %+8.2f %+8.2f %+8.2f %9.1f" % (k[0], k[1], n, A["prem"] / n, A["tot"] / n,
              A["delta"] / n, A["vega"] / n, A["rest"] / n, A["absS"] / n))
    print("\nBy days to expiry (all times): average 15-min change per side")
    by = defaultdict(lambda: defaultdict(float))
    for (g, _), A in agg.items():
        for x in ("n", "prem", "tot", "delta", "vega", "rest", "absS"):
            by[g][x] += A[x]
    for g in sorted(by):
        A = by[g]; n = A["n"]
        print("DTE %d: premium %.0f, total %+.2f, IV part %+.2f, decay+rest %+.2f per 15 min; avg |index move| %.1f pts; "
              "decay per hour = %.1f%% of premium" % (g, A["prem"] / n, A["tot"] / n, A["vega"] / n, A["rest"] / n,
              A["absS"] / n, -400 * (A["rest"] / n) / (A["prem"] / n)))


if __name__ == "__main__":
    main()


def regress(hdir):
    """Decay when the index stands still, how often CE/PE gain, and when the index moves (per days-to-expiry)."""
    opt = load_options(hdir, 1)
    spot = {}
    for f in Path(hdir).glob("OPT_1_ATM_CALL_*.json"):
        rec = json.load(open(f)); leg = rec["payload"].get("ce") or {}
        spot[rec["from"]] = dict(zip(leg.get("timestamp") or [], leg.get("spot") or []))
    exp = sorted(expiry_days(opt)); days = sorted({d for d, _ in opt})
    pts = defaultdict(list); tod = defaultdict(list)
    for d in days:
        nxt = next((e for e in exp if e >= d), None)
        if nxt is None:
            continue
        g = min(sum(1 for x in days if d <= x < nxt), 4)
        C, P, sp = opt.get((d, "CE")), opt.get((d, "PE")), spot.get(d, {})
        if not C or not P:
            continue
        ts = sorted(set(C) & set(P) & set(sp))
        for a, b2 in zip(ts, ts[15:]):
            if b2 - a != 900:
                continue
            hm = datetime.datetime.fromtimestamp(a, IST).strftime("%H:%M")
            dS = sp[b2] - sp[a]
            tod[hm[:2]].append(abs(dS))
            if C[a][0] != C[b2][0] or P[a][0] != P[b2][0] or hm[3:] not in ("00", "15", "30", "45"):
                continue
            ds = (C[b2][4] + P[b2][4]) - (C[a][4] + P[a][4])
            dc = C[b2][4] - C[a][4]; dp = P[b2][4] - P[a][4]
            pts[g].append((dS * dS, ds, dS, dc, dp))
    print("\nWhy: decay measured on 15-min windows where the index moved < 10 pts (same strike throughout)")
    for g in sorted(pts):
        r = pts[g]; n = len(r)
        still = [x[1] for x in r if x[0] < 100]                    # x[0] = move^2 -> |move| < 10
        aa = sum(still) / len(still)
        ce_win = 100 * sum(x[3] > 0 for x in r) / n; pe_win = 100 * sum(x[4] > 0 for x in r) / n
        need = (-aa / 2) / 0.5                                  # decay per side / delta 0.5
        hit = 100 * sum(1 for x in r if abs(x[2]) > need + 3) / n / 2
        print("DTE %d: n %4d | still-index straddle decay %+.1f pts per 15 min (%d windows) | CE up in %.0f%% of windows, "
              "PE up in %.0f%% | a CE/PE buyer needs > %.0f pts in its direction (+3 for spread): happens in ~%.0f%% of "
              "windows" % (g, n, aa, len(still), ce_win, pe_win, need + 3, hit))
    print("\nWhen the index moves: average |15-min move| by hour (all days)")
    print("  " + "  ".join("%s:00 %.1f" % (h, sum(v) / len(v)) for h, v in sorted(tod.items()) if h <= "15"))


if __name__ == "__main__" and len(sys.argv) > 1:
    regress(sys.argv[1])
