#!/usr/bin/env python3
"""Search for an 80 %+ win-rate version of FILTERED by adding option-data, breakout and weighted-stock filters.
1 year of SENSEX 1-min bars, real expired ATM premiums (with OI) and 30 stocks. Local files only; no orders.

  python3 tools/search_filtered_plus.py candles.csv HISTORY_DIR STOCK_DATA_DIR

Base: FILTERED crossings (day trend + fresh level + session range >= 300; see backtest_filtered.py) and, for
comparison, all FIRST_TOUCH crossings. Extra yes/no filters (each tested both ways), alone and in pairs/triples:
  option   ivhi     ATM straddle at the crossing above its 20-day median (high implied volatility)
           ivup     straddle rose over the last 15 min (same strike)
           pcr_ok   ATM PE OI / CE OI > 1 for CE, < 1 for PE (local PCR on the trade side)
           oi_ok    PE OI change minus CE OI change (15 min) agrees with the side (put writing for CE)
           exp0     expiry day   / exp1  day before expiry  (gamma and theta regime)
  breakout calm     index moved <= 15 pts toward the trigger in the 3 min before the cross
           extreme  the trigger is a new session high (CE) / low (PE)
           confirm  the crossing minute closed beyond the trigger
           am       before 11:30
           t2       exactly 2 touches
  stocks   breadth  share of the 30 stocks above previous close agrees with the side (> 0.5 CE, < 0.5 PE)
           basket5  weighted stock move over the last 5 min agrees with the side
           top3     HDFCBANK, RELIANCE, ICICIBANK all moved with the side over the last 5 min
Exits (premium points): +6/-11 (FILTERED), +4/-11, +3/-11, +6/-15, all with a 10-min time stop.
`confirm` uses the close of the crossing minute, so trades filtered on confirm=Y enter at that close (no look-ahead).
Selection on the FIRST half only (>= 30 trades), then shown on the SECOND half, with net Rs (lot 20, Rs 70/trade,
no slippage) next to the win rate: a high win rate with a small target can still lose money.
"""
import datetime, glob, itertools, json, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
W = {"HDFCBANK": 13.84, "RELIANCE": 10.61, "ICICIBANK": 9.52, "LT": 6.28, "BHARTIARTL": 5.62, "SBIN": 5.21,
     "INFY": 4.97, "AXISBANK": 3.75, "M&M": 3.36, "KOTAKBANK": 3.33, "ITC": 3.11, "BAJFINANCE": 3.0, "TCS": 2.85,
     "ETERNAL": 2.66, "HINDUNILVR": 2.21, "BEL": 2.18, "POWERGRID": 2.03, "MARUTI": 1.93, "ULTRACEMCO": 1.86,
     "SUNPHARMA": 1.79, "NTPC": 1.7, "ADANIPORTS": 1.67, "HCLTECH": 1.52, "ASIANPAINT": 1.43, "TECHM": 1.38,
     "TITAN": 1.19, "TMPV": 0.66, "BAJAJFINSV": 0.33}       # measured SENSEX influence (docs/SENSEX_STOCK_LOGIC_SPEC.md)


def load_oi(hdir):
    out = {}
    for f in glob.glob(os.path.join(hdir, "OPT_1_ATM_*.json")):
        rec = json.load(open(f)); side = "CE" if rec["series"][2] == "CALL" else "PE"
        leg = rec["payload"].get(side.lower()) or {}
        out[(rec["from"], side)] = dict(zip(leg.get("timestamp") or [], leg.get("oi") or []))
    return out


def load_spots(sdir):
    sp = defaultdict(dict)
    for f in glob.glob(os.path.join(sdir, "*_CALL_*.json")):
        n = os.path.basename(f).rsplit("_", 2)[0]
        p = json.load(open(f)); leg = p.get("ce") if isinstance(p, dict) else None
        if leg and leg.get("timestamp"):
            for t, s in zip(leg["timestamp"], leg["spot"]):
                if s:
                    sp[n][int(t)] = s
    return sp


def build(days, opt, oi, spots):
    ds = sorted(days); exp = expiry_days(opt)
    pre = {ds[i - 1] for i, d in enumerate(ds) if d in exp and i > 0}
    strad09 = {}
    for d in ds:
        t = next((x["timestampMs"] // 1000 for x in days[d] if x["t"].strftime("%H:%M") >= "09:30"), None)
        c, p = opt.get((d, "CE"), {}).get(t), opt.get((d, "PE"), {}).get(t)
        strad09[d] = c[4] + p[4] if c and p else None
    rows = []
    for di, d in enumerate(ds):
        if di == 0:
            continue
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        o0 = bars[0]["open"]
        pts = [x["timestampMs"] // 1000 for x in days[ds[di - 1]]]
        st_pc = {n: next((spots[n][t] for t in reversed(pts) if t in spots[n]), None) for n in W if n in spots}
        hist = [strad09[x] for x in ds[max(0, di - 20):di] if strad09.get(x)]
        med = sorted(hist)[len(hist) // 2] if len(hist) >= 10 else None
        for e in b.day_events(bars):
            j = idx[e["time"]]; up = e["side"] == "CE"; sg = 1 if up else -1
            if j < 16:
                continue
            entry = max(e["trigger"], bars[j]["open"]) if up else min(e["trigger"], bars[j]["open"])
            hi = max(x["high"] for x in bars[:j]); lo = min(x["low"] for x in bars[:j])
            ref = bars[j - 1]["close"]
            base = ((ref > o0) if up else (ref < o0)) and e["fresh"] and hi - lo >= 300
            t = bars[j]["timestampMs"] // 1000; t15 = bars[j - 15]["timestampMs"] // 1000
            t1 = bars[j - 1]["timestampMs"] // 1000; t6 = bars[j - 6]["timestampMs"] // 1000
            C, P = opt.get((d, "CE"), {}), opt.get((d, "PE"), {})
            f = {}
            if t1 in C and t1 in P:
                st = C[t1][4] + P[t1][4]
                f["ivhi"] = None if med is None else st > med
                f["ivup"] = (C[t1][4] + P[t1][4] > C[t15][4] + P[t15][4]) if (t15 in C and t15 in P and C[t15][0] == C[t1][0] and P[t15][0] == P[t1][0]) else None
            OC, OP = oi.get((d, "CE"), {}), oi.get((d, "PE"), {})
            if OC.get(t1) and OP.get(t1):
                f["pcr_ok"] = (OP[t1] / OC[t1] > 1) if up else (OP[t1] / OC[t1] < 1)
                if OC.get(t15) and OP.get(t15) and t15 in C and C[t15][0] == C[t1][0]:
                    flow = (OP[t1] - OP[t15]) / OP[t15] - (OC[t1] - OC[t15]) / OC[t15]
                    f["oi_ok"] = (flow > 0) if up else (flow < 0)
            f["exp0"] = d in exp; f["exp1"] = d in pre
            mom3 = (bars[j - 1]["close"] - bars[j - 4]["close"]) * sg
            f["calm"] = mom3 <= 15; f["extreme"] = e["extreme"]
            f["confirm"] = ((bars[j]["close"] - e["trigger"]) * sg) > 0
            f["am"] = e["time"] < "11:30"; f["t2"] = e["touches"] == 2
            above = n = 0; bm = 0.0; ws = 0.0; top = 0
            for nm, w in W.items():
                s = spots.get(nm, {})
                v1, v6 = s.get(t1), s.get(t6)
                if v1 and st_pc.get(nm):
                    n += 1; above += v1 > st_pc[nm]
                if v1 and v6:
                    bm += w * (v1 - v6) / v6; ws += w
                    if nm in ("HDFCBANK", "RELIANCE", "ICICIBANK") and (v1 - v6) * sg > 0:
                        top += 1
            if n >= 20:
                f["breadth"] = (above / n > 0.5) if up else (above / n < 0.5)
            if ws:
                f["basket5"] = bm * sg > 0
            f["top3"] = top == 3
            rows.append(dict(day=d, j=j, side=e["side"], ientry=entry, rule="X", base=base, f=f))
    return rows


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); oi = load_oi(sys.argv[2]); spots = load_spots(sys.argv[3])
    ds = sorted(days); half = ds[len(ds) // 2]
    rows = build(days, opt, oi, spots)
    exits = [(6, 11), (4, 11), (3, 11), (6, 15)]
    pnl, pnl_close = {}, {}
    for ex in exits:
        ro.TP, ro.SL = ex
        pnl[ex] = [(r := ro.trade(days, opt, s, "cross", 0)) and r["pts"] for s in rows]
        # 'confirm' is only known when the crossing minute has closed: those trades enter at that minute's close
        pnl_close[ex] = [(r := ro.trade(days, opt, s, "close", 0)) and r["pts"] for s in rows]
    feats = sorted({k for r in rows for k in r["f"]})
    conds = [(k, v) for k in feats for v in (True, False)]
    print("crossings %d (FILTERED base %d), features %s, halves split at %s"
          % (len(rows), sum(r["base"] for r in rows), ",".join(feats), half))
    out = []
    for basename, bsel in (("FILTERED", lambda r: r["base"]), ("ALL", lambda r: True)):
        for k in (0, 1, 2, 3):
            for combo in itertools.combinations(conds, k):
                if len({c[0] for c in combo}) < k:
                    continue
                sel = [i for i, r in enumerate(rows) if bsel(r) and all(r["f"].get(c) == v for c, v in combo)]
                if len(sel) < 40:
                    continue
                late = any(c == "confirm" and v for c, v in combo)
                for ex in exits:
                    h = [[], []]
                    for i in sel:
                        p = (pnl_close if late else pnl)[ex][i]
                        if p is not None:
                            h[rows[i]["day"] >= half].append(p)
                    if len(h[0]) < 30 or len(h[1]) < 15:
                        continue
                    w = [100 * sum(p > 0 for p in x) / len(x) for x in h]
                    net = [sum(p * ro.LOT for p in x) - ro.CHARGES * len(x) for x in h]
                    out.append((w[0], w[1], net[0], net[1], basename, combo, ex, len(h[0]), len(h[1])))
    print("combinations evaluated: %d" % len(out))
    print("win >= 80 %% in first half: %d; of those >= 80 %% in second half too: %d; >= 80 %% in both AND net > 0 in both: %d"
          % (sum(o[0] >= 80 for o in out), sum(o[0] >= 80 and o[1] >= 80 for o in out),
             sum(o[0] >= 80 and o[1] >= 80 and o[2] > 0 and o[3] > 0 for o in out)))
    print("win >= 70 %% in both halves: %d; net > 0 in both halves (any win rate): %d"
          % (sum(o[0] >= 70 and o[1] >= 70 for o in out), sum(o[2] > 0 and o[3] > 0 for o in out)))
    fmt = "  %-8s %-58s exit +%g/-%g | n %3d/%3d | win H1 %4.1f%% H2 %4.1f%% | net Rs H1 %+7.0f / H2 %+7.0f"

    def show(o):
        c = " & ".join("%s=%s" % (k, "Y" if v else "N") for k, v in o[5]) or "(no extra filter)"
        print(fmt % (o[4], c, o[6][0], o[6][1], o[7], o[8], o[0], o[1], o[2], o[3]))
    print("\nHighest first-half win rates (chosen on H1) -> second half:")
    for o in sorted(out, key=lambda o: (-o[0], -o[7]))[:15]:
        show(o)
    print("\nBest by net profit in the weaker half:")
    for o in sorted(out, key=lambda o: -min(o[2], o[3]))[:12]:
        show(o)
    print("\nReference rows:")
    for o in out:
        if not o[5] and o[6] == (6, 11):
            show(o)


if __name__ == "__main__":
    main()
