#!/usr/bin/env python3
"""Can the next 1-min / 5-min SENSEX candle be predicted from everything we have? Local files only; no orders.

  python3 tools/predict_next_candle.py sensex_1min.csv SENSEX_OPT_HISTORY_DIR STOCK_DATA_DIR

Inputs at each minute t (09:20-15:20, only information known at the close of minute t):
  SENSEX       move over the last 1 / 5 / 15 min, position vs day open and vs day range, minute of day
  FRESH        FRESH_EXTREME / FRESH_CALM breakout firing in this minute (+1 CE, -1 PE), same replica as
               backtest_fresh_extreme.py / backtest_real_options.py
  SENSEX opt   rolling ATM CE minus PE premium change (1 / 5 min, same strike), PE minus CE OI change (5 min)
  stocks       up to 30 SENSEX stocks (approximate weights): weighted move 1 / 5 min, gap to SENSEX,
               breadth (share above day open, share up in the last 5 min), bank basket move
  stock opts   weighted CE minus PE premium change, PE minus CE OI change, CE share of option volume (5 min)
Targets: SENSEX close(t+1) - close(t) and close(t+5) - close(t), points.
Tests: (1) each input alone (correlation, sign hit rate) on both halves; (2) a linear model and a gradient-boosting
model trained on the FIRST half (before 28 Apr 2026) only and scored on the SECOND half: sign accuracy, correlation,
and the average move captured when the model is most confident (top 10 % / 2 %), compared with round-trip costs
(~5 pts for an ATM option, ~22 pts for a future). Needs numpy (and scikit-learn for the boosting model).
"""
import csv, datetime, glob, json, os, sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_fresh_extreme as bfe                         # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
W = {"HDFCBANK": 15, "ICICIBANK": 10, "RELIANCE": 10, "INFY": 6, "BHARTIARTL": 5, "LT": 4.5, "ITC": 4, "TCS": 4,
     "AXISBANK": 3.5, "KOTAKBANK": 3.5, "SBIN": 3, "M&M": 3, "BAJFINANCE": 2.5, "HINDUNILVR": 2.5, "SUNPHARMA": 2,
     "HCLTECH": 2, "MARUTI": 1.8, "NTPC": 1.7, "ULTRACEMCO": 1.5, "TITAN": 1.5, "TMPV": 1.2, "POWERGRID": 1.3,
     "BAJAJFINSV": 1.2, "TATASTEEL": 1.2, "ASIANPAINT": 1.1, "ADANIPORTS": 1.1, "TECHM": 1.0, "ETERNAL": 1.2,
     "TRENT": 1.0, "BEL": 1.0}                               # approximate SENSEX weights (2025), %
BANKS = ("HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "SBIN")


def load_stocks(ddir):
    S = defaultdict(dict)
    for f in glob.glob(os.path.join(ddir, "*.json")):
        base = os.path.basename(f)[:-5]
        name, side = base.rsplit("_", 2)[0], base.rsplit("_", 2)[1]
        p = json.load(open(f)); leg = p.get("ce" if side == "CALL" else "pe") if isinstance(p, dict) else None
        if not leg or not leg.get("timestamp"):
            continue
        n = len(leg["timestamp"])
        for i, t in enumerate(leg["timestamp"]):
            S[(name, side)][t] = (leg["strike"][i], leg["close"][i], (leg.get("oi") or [0] * n)[i],
                                  (leg.get("volume") or [0] * n)[i], leg["spot"][i])
    return S


def load_sensex_opt(hdir):
    O = {}
    for f in glob.glob(os.path.join(hdir, "OPT_1_ATM_*.json")):
        rec = json.load(open(f)); side = "CALL" if rec["series"][2] == "CALL" else "PUT"
        leg = rec["payload"].get("ce" if side == "CALL" else "pe") or {}
        for i, t in enumerate(leg.get("timestamp") or []):
            O.setdefault(side, {})[t] = (leg["strike"][i], leg["close"][i], (leg.get("oi") or [0] * 9999)[i])
    return O


def fresh_events(days):
    """{epoch_s of the crossing minute: +1 / -1} for FRESH_EXTREME and FRESH_CALM."""
    fx, fc = {}, {}
    for d, bars in days.items():
        idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        thu = datetime.date.fromisoformat(d).weekday() == 3
        for e in bfe.day_events(bars):
            if not (e["fresh"] and e["extreme"]):
                continue
            j = idx[e["time"]]; sg = 1 if e["side"] == "CE" else -1
            t = bars[j]["timestampMs"] // 1000
            fx[t] = sg
            mom3 = (bars[j - 1]["close"] - bars[j - 4]["close"]) * sg if j >= 4 else None
            if not thu and mom3 is not None and mom3 <= 15:
                fc[t] = sg
    return fx, fc


def build(csvfile, hdir, sdir):
    days = bfe.load(csvfile)
    fx, fc = fresh_events(days)
    O = load_sensex_opt(hdir); S = load_stocks(sdir)
    names = [n for n in W if (n, "CALL") in S]
    print("stocks with data: %d (%s)" % (len(names), ",".join(names)))
    X, Y1, Y5, D, OPT = [], [], [], [], []
    cols = None
    for d in sorted(days):
        bars = days[d]; ts = [b["timestampMs"] // 1000 for b in bars]; cl = [b["close"] for b in bars]
        o0 = bars[0]["open"]
        st_open = {}
        for n in names:
            m = S[(n, "CALL")]
            first = next((m[t][4] for t in ts if t in m), None)
            st_open[n] = first
        hi = lo = cl[0]
        for j in range(15, len(bars) - 5):
            hi = max(hi, bars[j]["high"]); lo = min(lo, bars[j]["low"])
            hm = bars[j]["t"].strftime("%H:%M")
            if hm < "09:20" or hm > "15:20":
                continue
            t, t1, t5 = ts[j], ts[j - 1], ts[j - 5]
            f = {}
            f["sx_r1"] = cl[j] - cl[j - 1]; f["sx_r5"] = cl[j] - cl[j - 5]; f["sx_r15"] = cl[j] - cl[j - 15]
            f["sx_vs_open"] = cl[j] - o0
            f["sx_range_pos"] = (cl[j] - lo) / (hi - lo) - 0.5 if hi > lo else 0.0
            f["minute"] = bars[j]["t"].hour * 60 + bars[j]["t"].minute - 555
            f["fresh_extreme"] = fx.get(t, 0); f["fresh_calm"] = fc.get(t, 0)
            for L, tl in ((1, t1), (5, t5)):
                c, p = O.get("CALL", {}), O.get("PUT", {})
                v = 0.0; vo = 0.0
                if t in c and tl in c and t in p and tl in p and c[t][0] == c[tl][0] and p[t][0] == p[tl][0]:
                    v = (c[t][1] - c[tl][1]) - (p[t][1] - p[tl][1])
                    if L == 5 and c[tl][2] and p[tl][2]:
                        vo = (p[t][2] - p[tl][2]) / p[tl][2] - (c[t][2] - c[tl][2]) / c[tl][2]
                f["sxopt_prem%d" % L] = v
                if L == 5:
                    f["sxopt_oi5"] = vo
            # stocks
            agg = defaultdict(float); wsum = 0.0; above = up5 = 0; nb = 0
            bank = 0.0; bw = 0.0
            for n in names:
                C, P = S[(n, "CALL")], S.get((n, "PUT"), {})
                if t not in C or t1 not in C or t5 not in C:
                    continue
                w = W[n]; wsum += w; nb += 1
                sp, sp1, sp5 = C[t][4], C[t1][4], C[t5][4]
                agg["st_r1"] += w * 100 * (sp - sp1) / sp1; agg["st_r5"] += w * 100 * (sp - sp5) / sp5
                if n in BANKS:
                    bank += w * 100 * (sp - sp5) / sp5; bw += w
                above += 1 if st_open[n] and sp > st_open[n] else 0; up5 += 1 if sp > sp5 else 0
                if t in P and t5 in P and C[t][0] == C[t5][0] and P[t][0] == P[t5][0] and C[t5][1] > 0 and P[t5][1] > 0:
                    agg["stopt_prem5"] += w * 100 * ((C[t][1] - C[t5][1]) / C[t5][1] - (P[t][1] - P[t5][1]) / P[t5][1])
                    if C[t5][2] and P[t5][2]:
                        agg["stopt_oi5"] += w * ((P[t][2] - P[t5][2]) / P[t5][2] - (C[t][2] - C[t5][2]) / C[t5][2])
                cv = sum(C[x][3] for x in range(t5 + 60, t + 1, 60) if x in C)
                pv = sum(P[x][3] for x in range(t5 + 60, t + 1, 60) if x in P)
                if cv + pv:
                    agg["stopt_vol5"] += w * (cv - pv) / (cv + pv)
            if nb < max(5, len(names) // 2):
                continue
            for k in ("st_r1", "st_r5", "stopt_prem5", "stopt_oi5", "stopt_vol5"):
                f[k] = agg[k] / wsum
            f["st_gap1"] = f["st_r1"] - 100 * f["sx_r1"] / cl[j - 1]
            f["st_gap5"] = f["st_r5"] - 100 * f["sx_r5"] / cl[j - 5]
            f["breadth_open"] = above / nb - 0.5; f["breadth_up5"] = up5 / nb - 0.5
            f["bank_r5"] = bank / bw if bw else 0.0
            if cols is None:
                cols = list(f)
            X.append([f[k] for k in cols]); Y1.append(cl[j + 1] - cl[j]); Y5.append(cl[j + 5] - cl[j]); D.append(d)
            # tradeable outcome: buy the SENSEX ATM CE (or PE) at the close of minute t, sell 1 / 5 min later, same strike
            row = []
            for side in ("CALL", "PUT"):
                m = O.get(side, {})
                for H in (1, 5):
                    th = ts[j + H]
                    row.append(m[th][1] - m[t][1] if t in m and th in m and m[th][0] == m[t][0] else np.nan)
            OPT.append(row)
    return cols, np.array(X, float), np.array(Y1), np.array(Y5), np.array(D), np.array(OPT, float)


def main():
    cols, X, Y1, Y5, D, OPT = build(sys.argv[1], sys.argv[2], sys.argv[3])
    days = sorted(set(D)); half = days[len(days) // 2]; h2 = D >= half; h1 = ~h2
    print("minutes %d, days %d, halves split at %s" % (len(X), len(days), half))
    print("avg |next 1-min move| %.1f pts, |next 5-min move| %.1f pts" % (np.abs(Y1).mean(), np.abs(Y5).mean()))
    print("\n1) each input alone: correlation with the next move (H1 / H2) and sign hit rate (H1 / H2)")
    print("%-15s | next 1-min: corr        hit          | next 5-min: corr        hit" % "input")
    for i, c in enumerate(cols):
        out = []
        for Y in (Y1, Y5):
            r = []
            for m in (h1, h2):
                x, y = X[m, i], Y[m]
                cc = np.corrcoef(x, y)[0, 1] if x.std() > 0 else 0.0
                nz = (x != 0) & (y != 0)
                hit = 100 * np.mean(np.sign(x[nz]) == np.sign(y[nz])) if nz.any() else float("nan")
                r.append((cc, hit))
            out.append(r)
        print("%-15s | %+.3f/%+.3f  %4.1f%%/%4.1f%% | %+.3f/%+.3f  %4.1f%%/%4.1f%%" % (
            c, out[0][0][0], out[0][1][0], out[0][0][1], out[0][1][1], out[1][0][0], out[1][1][0], out[1][0][1], out[1][1][1]))
    # FRESH events alone: what happened next when they fired
    for c in ("fresh_extreme", "fresh_calm"):
        i = cols.index(c)
        for Y, nm in ((Y1, "1-min"), (Y5, "5-min")):
            for m, hn in ((h1, "H1"), (h2, "H2")):
                s = X[m, i] != 0
                if s.any():
                    mv = Y[m][s] * X[m, i][s]
                    print("  %s fired (%s, n %d): next %s move in signal direction avg %+.1f pts, right %.1f%%"
                          % (c, hn, s.sum(), nm, mv.mean(), 100 * np.mean(mv > 0)))
    print("\n2) models trained on the FIRST half only, scored on the SECOND half")
    mu, sd = X[h1].mean(0), X[h1].std(0) + 1e-12
    Z = (X - mu) / sd
    models = [("linear", None)]
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
        models.append(("boosting", HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                                                 min_samples_leaf=200, l2_regularization=1.0)))
    except ImportError:
        print("  (scikit-learn not installed: boosting model skipped)")
    for Y, nm, H in ((Y1, "next 1-min", 1), (Y5, "next 5-min", 5)):
        for mn, mdl in models:
            if mdl is None:
                A = np.c_[Z[h1], np.ones(h1.sum())]
                beta = np.linalg.lstsq(A, Y[h1], rcond=None)[0]
                pred = np.c_[Z, np.ones(len(Z))] @ beta
            else:
                mdl.fit(Z[h1], Y[h1]); pred = mdl.predict(Z)
            for m, hn in ((h1, "train H1"), (h2, "TEST H2")):
                p, y = pred[m], Y[m]
                acc = 100 * np.mean(np.sign(p) == np.sign(y))
                cc = np.corrcoef(p, y)[0, 1]
                line = "  %-8s %-10s %-8s | sign right %.1f%% | corr %+.3f" % (mn, nm, hn, acc, cc)
                for q in (90, 98):
                    thr = np.percentile(np.abs(pred[h1]), q)
                    s = np.abs(p) >= thr
                    if s.sum() >= 20:
                        cap = (np.sign(p[s]) * y[s]).mean()
                        line += " | top %d%%: n %d right %.1f%% avg %+.1f pts" % (100 - q, s.sum(),
                                                                              100 * np.mean(np.sign(p[s]) == np.sign(y[s])), cap)
                print(line)
                # tradeable: buy SENSEX ATM CE when the model says up, PE when down; premium change, before costs
                oc = OPT[m][:, 0 if H == 1 else 1]; op = OPT[m][:, 2 if H == 1 else 3]
                for q in (90, 98):
                    thr = np.percentile(np.abs(pred[h1]), q)
                    s = (np.abs(p) >= thr)
                    g = np.where(p > 0, oc, op)[s]; g = g[~np.isnan(g)]
                    if len(g) >= 20:
                        print("      -> buying the SENSEX ATM option on the top %d%%: n %d, premium change avg %+.2f pts, "
                              "up in %.1f%% (costs ~5 pts per round trip) " % (100 - q, len(g), g.mean(), 100 * np.mean(g > 0)))
    print("\nCosts to beat per trade: ~5 SENSEX pts (ATM option round trip), ~22 pts (future).")


if __name__ == "__main__":
    main()
