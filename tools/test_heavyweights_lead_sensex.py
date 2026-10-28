#!/usr/bin/env python3
"""Can the SENSEX heavyweights and their option chains predict SENSEX's next minutes? Local files only; no orders.

  python3 tools/test_heavyweights_lead_sensex.py sensex_1min.csv STOCK_DATA_DIR

Stocks: the 8 fetched by backtest_stock_option_buying.py (rolling ATM monthly CE/PE, 1-min, with stock price, OI,
volume), weighted by their approximate SENSEX weights. Every 5 minutes (09:30-14:30) these features are measured
over the last L = 5 / 15 min, all signed so that positive = bullish:
  sensex_mom      SENSEX's own move (control: plain momentum)
  basket_mom      weighted stock move (%)
  basket_gap      basket move minus SENSEX move (does the index lag its stocks?)
  opt_premium     weighted (CE % change - PE % change), same strike only
  opt_oi          weighted (PE OI change - CE OI change) / OI   (put writing = bullish, the NSE-analyzer idea)
  opt_volume      weighted (CE volume - PE volume) / (CE + PE volume)
  breadth         number of the 8 stocks above their own day open, minus 4
Target: SENSEX move over the next H = 5 / 15 / 30 min (points). Reported per half (split 28 Apr 2026): correlation,
sign hit rate, and the average SENSEX move after the strongest 20 % of readings in each direction, compared with the
round-trip cost of trading it (about 5 pts for an ATM option, about 22 pts for a future).
"""
import csv, datetime, glob, json, os, sys
from collections import defaultdict

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
W = {"HDFCBANK": .15, "ICICIBANK": .10, "RELIANCE": .10, "INFY": .06, "BHARTIARTL": .05, "TCS": .04,
     "AXISBANK": .035, "SBIN": .03}                          # approximate SENSEX weights (2025)


def load_sensex(path):
    out = {}
    for r in csv.DictReader(open(path)):
        out[int(r["Epoch"])] = float(r["Close"])
    return out


def load_stocks(ddir):
    S = defaultdict(dict)                                    # (stock, side) -> {t: (K, close, oi, vol, spot)}
    for f in glob.glob(os.path.join(ddir, "*.json")):
        name, side, _ = os.path.basename(f)[:-5].split("_")
        p = json.load(open(f)); leg = p.get("ce" if side == "CALL" else "pe") if isinstance(p, dict) else None
        if not leg or not leg.get("timestamp"):
            continue
        n = len(leg["timestamp"])
        for i, t in enumerate(leg["timestamp"]):
            S[(name, side)][t] = (leg["strike"][i], leg["close"][i], (leg.get("oi") or [0] * n)[i],
                                  (leg.get("volume") or [0] * n)[i], leg["spot"][i])
    return S


def corr(x, y):
    n = len(x); mx = sum(x) / n; my = sum(y) / n
    sx = sum((a - mx) ** 2 for a in x) ** .5; sy = sum((b - my) ** 2 for b in y) ** .5
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else 0.0


def main():
    sx = load_sensex(sys.argv[1]); S = load_stocks(sys.argv[2])
    times = sorted(sx); day_open = {}
    for t in times:
        d = datetime.datetime.fromtimestamp(t, IST).date()
        day_open.setdefault(d, None)
    st_open = {}
    for (name, side), m in S.items():
        if side != "CALL":
            continue
        for t in sorted(m):
            d = datetime.datetime.fromtimestamp(t, IST).date()
            st_open.setdefault((name, d), m[t][4])
    rows = []
    for t in times:
        dt = datetime.datetime.fromtimestamp(t, IST); hm = dt.strftime("%H:%M")
        if hm < "09:30" or hm > "14:30" or dt.minute % 5:
            continue
        for L in (5, 15):
            t0 = t - 60 * L
            if t0 not in sx:
                continue
            f = defaultdict(float); ok = True; wsum = 0.0; br = 0
            for name, w in W.items():
                C, P = S.get((name, "CALL"), {}), S.get((name, "PUT"), {})
                if t not in C or t0 not in C or t not in P or t0 not in P:
                    ok = False; break
                c1, c0, p1, p0 = C[t], C[t0], P[t], P[t0]
                wsum += w
                f["basket_mom"] += w * 100 * (c1[4] - c0[4]) / c0[4]
                if c1[0] == c0[0] and p1[0] == p0[0] and c0[1] > 0 and p0[1] > 0:
                    f["opt_premium"] += w * 100 * ((c1[1] - c0[1]) / c0[1] - (p1[1] - p0[1]) / p0[1])
                    if c0[2] and p0[2]:
                        f["opt_oi"] += w * ((p1[2] - p0[2]) / p0[2] - (c1[2] - c0[2]) / c0[2])
                cv = sum(C[x][3] for x in range(t0 + 60, t + 1, 60) if x in C)
                pv = sum(P[x][3] for x in range(t0 + 60, t + 1, 60) if x in P)
                if cv + pv:
                    f["opt_volume"] += w * (cv - pv) / (cv + pv)
                o = st_open.get((name, dt.date()))
                br += 1 if o and c1[4] > o else 0
            if not ok:
                continue
            f["basket_mom"] /= wsum
            sm = 100 * (sx[t] - sx[t0]) / sx[t0]
            f["sensex_mom"] = sm; f["basket_gap"] = f["basket_mom"] - sm; f["breadth"] = br - 4
            fut = {H: sx[t + 60 * H] - sx[t] for H in (5, 15, 30) if t + 60 * H in sx}
            rows.append((dt.date().isoformat(), L, dict(f), fut))
    days = sorted({r[0] for r in rows}); half = days[len(days) // 2]
    print("readings %d (every 5 min, 09:30-14:30), days %d, halves split at %s" % (len(rows), len(days), half))
    feats = ["sensex_mom", "basket_mom", "basket_gap", "opt_premium", "opt_oi", "opt_volume", "breadth"]
    print("\n%-12s L  H  | corr H1   H2  | sign hit H1   H2  | top-20%% bullish -> SENSEX next move H1 / H2 | bearish H1 / H2"
          % "feature")
    for L in (5, 15):
        for fe in feats:
            for H in (5, 15, 30):
                out = []
                for h in (0, 1):
                    xs = [(r[2].get(fe, 0.0), r[3][H]) for r in rows if r[1] == L and H in r[3] and (r[0] >= half) == h]
                    if len(xs) < 50:
                        out = None; break
                    x = [a for a, _ in xs]; y = [b for _, b in xs]
                    hit = 100 * sum(1 for a, b in xs if a * b > 0) / max(1, sum(1 for a, b in xs if a * b != 0))
                    srt = sorted(xs); k = len(srt) // 5
                    bull = sum(b for _, b in srt[-k:]) / k; bear = sum(b for _, b in srt[:k]) / k
                    out.append((corr(x, y), hit, bull, bear))
                if out:
                    print("%-12s %2d %2d | %+.3f %+.3f | %5.1f%% %5.1f%% | %+7.1f / %+7.1f pts            | %+7.1f / %+7.1f"
                          % (fe, L, H, out[0][0], out[1][0], out[0][1], out[1][1], out[0][2], out[1][2], out[0][3], out[1][3]))


if __name__ == "__main__":
    main()
