#!/usr/bin/env python3
"""Final test: can the bullish / weak state of the 30 SENSEX stocks, SENSEX's own chart and the option data
(SENSEX ATM and stock ATM options) predict SENSEX over the next 15 / 30 / 60 min or to the close? No orders.

  python3 tools/final_predict_sensex.py sensex_1min.csv SENSEX_OPT_HISTORY_DIR STOCK_DATA_DIR

Design (following the reviews in docs/sessions/2026-10-28_NEXT_CANDLE_PREDICTION.md):
  * Decisions every 5 min, 09:30-14:55. Entry one minute AFTER the decision (t+1) so the few-second index lag
    cannot be harvested by construction; targets are measured from t+1.
  * Walk-forward: for each calendar month, models are trained only on earlier months (at least 3) and predict that
    month. Nothing from the future is used.
  * Scored in tradeable terms: buying the SENSEX ATM CE (model up) or PE (model down) at t+1 and selling at
    t+1+H on real rolling premiums (strike rolls bridged with delta 0.5) minus 5 pts; and a SENSEX future minus 22 pts.
  * Chance baseline: the same walk-forward with each day's outcomes randomly sign-flipped.
Needs numpy and scikit-learn.
"""
import csv, datetime, glob, json, os, random, sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_fresh_extreme as bfe                         # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
W = {"HDFCBANK": 15, "ICICIBANK": 10, "RELIANCE": 10, "INFY": 6, "BHARTIARTL": 5, "LT": 4.5, "ITC": 4, "TCS": 4,
     "AXISBANK": 3.5, "KOTAKBANK": 3.5, "SBIN": 3, "M&M": 3, "BAJFINANCE": 2.5, "HINDUNILVR": 2.5, "SUNPHARMA": 2,
     "HCLTECH": 2, "MARUTI": 1.8, "NTPC": 1.7, "ULTRACEMCO": 1.5, "TITAN": 1.5, "TMPV": 1.2, "POWERGRID": 1.3,
     "BAJAJFINSV": 1.2, "TATASTEEL": 1.2, "ASIANPAINT": 1.1, "ADANIPORTS": 1.1, "TECHM": 1.0, "ETERNAL": 1.2,
     "TRENT": 1.0, "BEL": 1.0}
SECTOR = {"IT": ("INFY", "TCS", "HCLTECH", "TECHM"), "BANKS": ("HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "SBIN"),
          "FIN": ("BAJFINANCE", "BAJAJFINSV"), "ENERGY": ("RELIANCE", "NTPC", "POWERGRID")}
HORIZONS = (15, 30, 60, "close")
if os.environ.get("WEIGHTS_JSON"):                         # another index (e.g. NIFTY): {stock: weight %}
    W = json.load(open(os.environ["WEIGHTS_JSON"]))
COST_OPT = float(os.environ.get("COST_OPT", "5"))           # index pts per option round trip
COST_FUT = float(os.environ.get("COST_FUT", "22"))          # index pts per future round trip


def load_legs(path_glob, key_side):
    out = defaultdict(dict)                                   # (name, side) -> {t: (K, close, oi, vol, spot)}
    for f in glob.glob(path_glob):
        base = os.path.basename(f)[:-5]
        name, side = key_side(base)
        p = json.load(open(f))
        p = p.get("payload", p) if isinstance(p, dict) else p
        leg = p.get("ce" if side == "CALL" else "pe") if isinstance(p, dict) else None
        if not leg or not leg.get("timestamp"):
            continue
        n = len(leg["timestamp"])
        oi = leg.get("oi") or [0] * n; vol = leg.get("volume") or [0] * n; sp = leg.get("spot") or [None] * n
        for i, t in enumerate(leg["timestamp"]):
            out[(name, side)][t] = (leg["strike"][i], leg["close"][i], oi[i] or 0, vol[i] or 0, sp[i])
    return out


def rsi(cl, j, n=14):
    if j < n:
        return 0.0
    g = l = 0.0
    for k in range(j - n + 1, j + 1):
        d = cl[k] - cl[k - 1]
        g += max(d, 0); l += max(-d, 0)
    return (100 * g / (g + l) - 50) if g + l else 0.0


def build(csvfile, hdir, sdir):
    days = bfe.load(csvfile); dk = sorted(days)
    SX = load_legs(os.path.join(hdir, "OPT_1_ATM_*.json"),
                   lambda b: ("SENSEX", "CALL" if "_CALL_" in b else "PUT"))
    ST = load_legs(os.path.join(sdir, "*.json"), lambda b: (b.rsplit("_", 2)[0], b.rsplit("_", 2)[1]))
    names = [n for n in W if (n, "CALL") in ST]
    print("stocks with data: %d / %d" % (len(names), len(W)))
    # FRESH events
    fresh = {}
    for d, bars in days.items():
        idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        for e in bfe.day_events(bars):
            if e["fresh"] and e["extreme"]:
                fresh[bars[idx[e["time"]]]["timestampMs"] // 1000] = 1 if e["side"] == "CE" else -1
    # expiry days from the SENSEX straddle roll
    def strad(d, hm):
        bars = days[d]; t = next((b["timestampMs"] // 1000 for b in bars if b["t"].strftime("%H:%M") >= hm), None)
        c, p = SX[("SENSEX", "CALL")].get(t), SX[("SENSEX", "PUT")].get(t)
        return c[1] + p[1] if c and p else None
    exp = {a for a, b in zip(dk, dk[1:]) if strad(a, "15:00") and strad(b, "09:20") and strad(b, "09:20") > 2 * strad(a, "15:00")}
    prev_close_stock = {}
    for i, d in enumerate(dk):
        if i == 0:
            continue
        ts_prev = [b["timestampMs"] // 1000 for b in days[dk[i - 1]]]
        for n in names:
            C = ST[(n, "CALL")]
            v = next((C[t][4] for t in reversed(ts_prev) if t in C and C[t][4]), None)
            prev_close_stock[(n, d)] = v
    rows, meta, cols = [], [], None
    for i, d in enumerate(dk):
        if i == 0:
            continue
        bars = days[d]; ts = [b["timestampMs"] // 1000 for b in bars]; cl = [b["close"] for b in bars]
        pcs = days[dk[i - 1]][-1]["close"]; o0 = bars[0]["open"]
        orh = max(b["high"] for b in bars[:15]); orl = min(b["low"] for b in bars[:15])
        st_open = {}
        for n in names:
            C = ST[(n, "CALL")]; st_open[n] = next((C[t][4] for t in ts if t in C and C[t][4]), None)
        i_close = next((k for k, b in enumerate(bars) if b["t"].strftime("%H:%M") >= "15:25"), len(bars) - 1)
        hi = lo = cl[0]
        for j in range(len(bars)):
            hi = max(hi, bars[j]["high"]); lo = min(lo, bars[j]["low"])
            hm = bars[j]["t"].strftime("%H:%M")
            if hm < "09:30" or hm > "14:55" or bars[j]["t"].minute % 5 or j + 1 >= i_close:
                continue
            t = ts[j]; f = {}
            # SENSEX chart
            for L in (5, 15, 30, 60):
                f["sx_r%d" % L] = cl[j] - cl[max(0, j - L)]
            f["sx_vs_open"] = 100 * (cl[j] - o0) / o0; f["sx_vs_prev"] = 100 * (cl[j] - pcs) / pcs
            f["sx_range_pos"] = (cl[j] - lo) / (hi - lo) - 0.5 if hi > lo else 0.0
            f["sx_vs_twap"] = cl[j] - sum(cl[:j + 1]) / (j + 1)
            f["sx_rsi14"] = rsi(cl, j)
            f["sx_ma20_slope"] = (sum(cl[max(0, j - 19):j + 1]) / min(20, j + 1)) - (sum(cl[max(0, j - 24):max(1, j - 4)]) / max(1, min(20, j - 4)))
            f["sx_or_state"] = 1 if cl[j] > orh else (-1 if cl[j] < orl else 0)
            f["sx_newhl5"] = 1 if max(b["high"] for b in bars[max(0, j - 4):j + 1]) >= hi else (-1 if min(b["low"] for b in bars[max(0, j - 4):j + 1]) <= lo else 0)
            f["sx_rv30"] = float(np.std([cl[k] - cl[k - 1] for k in range(max(1, j - 29), j + 1)])) if j > 2 else 0.0
            f["minute"] = j; f["expiry_day"] = 1 if d in exp else 0
            f["fresh5"] = sum(fresh.get(ts[k], 0) for k in range(max(0, j - 4), j + 1))
            # SENSEX ATM options
            C, P = SX[("SENSEX", "CALL")], SX[("SENSEX", "PUT")]
            for L in (5, 15):
                tl = ts[max(0, j - L)]
                ok = t in C and tl in C and t in P and tl in P and C[t][0] == C[tl][0] and P[t][0] == P[tl][0]
                f["sxo_cmp%d" % L] = ((C[t][1] - C[tl][1]) - (P[t][1] - P[tl][1])) if ok else 0.0
                f["sxo_strad%d" % L] = ((C[t][1] + P[t][1]) - (C[tl][1] + P[tl][1])) if ok else 0.0
                f["sxo_oi%d" % L] = (((P[t][2] - P[tl][2]) / P[tl][2] - (C[t][2] - C[tl][2]) / C[tl][2])
                                     if ok and C[tl][2] and P[tl][2] else 0.0)
            # stocks: bullish / weak state
            wsum = 0.0; agg = defaultdict(float); sec = defaultdict(float); rets = []; nb = 0; cnt = defaultdict(int)
            t5, t15, t30 = ts[max(0, j - 5)], ts[max(0, j - 15)], ts[max(0, j - 30)]
            for n in names:
                Cs, Ps = ST[(n, "CALL")], ST.get((n, "PUT"), {})
                if t not in Cs or t15 not in Cs or not Cs[t][4] or not Cs[t15][4]:
                    continue
                w = W[n]; s = Cs[t][4]; s15 = Cs[t15][4]; s5 = Cs[t5][4] if t5 in Cs and Cs[t5][4] else s15
                s30 = Cs[t30][4] if t30 in Cs and Cs[t30][4] else s15
                pc = prev_close_stock.get((n, d)); op = st_open.get(n)
                score = 0
                score += 1 if op and s > op else -1
                score += 1 if pc and s > pc else -1
                score += 1 if s > s15 else -1
                score += 1 if s > s30 else -1
                cnt["bull"] += score >= 3; cnt["weak"] += score <= -3; nb += 1; wsum += w
                agg["w_score"] += w * score
                r_pc = 100 * (s - pc) / pc if pc else 0.0
                agg["w_vs_prev"] += w * r_pc; agg["w_r5"] += w * 100 * (s - s5) / s5; agg["w_r15"] += w * 100 * (s - s15) / s15
                rets.append(r_pc)
                sec[next((k for k, v in SECTOR.items() if n in v), "OTHER")] += w / 100 * r_pc
                if t in Ps and t15 in Ps and Cs[t][0] == Cs[t15][0] and Ps[t][0] == Ps[t15][0] and Cs[t15][1] > 0 and Ps[t15][1] > 0:
                    agg["w_opt_cmp"] += w * 100 * ((Cs[t][1] - Cs[t15][1]) / Cs[t15][1] - (Ps[t][1] - Ps[t15][1]) / Ps[t15][1])
                    agg["w_opt_strad"] += w * 100 * ((Cs[t][1] + Ps[t][1]) / (Cs[t15][1] + Ps[t15][1]) - 1)
                    if Cs[t15][2] and Ps[t15][2]:
                        agg["w_opt_oi"] += w * ((Ps[t][2] - Ps[t15][2]) / Ps[t15][2] - (Cs[t][2] - Cs[t15][2]) / Cs[t15][2])
                cv = sum(Cs[x][3] for x in range(t15 + 60, t + 1, 60) if x in Cs)
                pv = sum(Ps[x][3] for x in range(t15 + 60, t + 1, 60) if x in Ps)
                if cv + pv:
                    agg["w_opt_vol"] += w * (cv - pv) / (cv + pv)
            if nb < 20:
                continue
            for k in ("w_score", "w_vs_prev", "w_r5", "w_r15", "w_opt_cmp", "w_opt_strad", "w_opt_oi", "w_opt_vol"):
                f[k] = agg[k] / wsum
            f["n_bullish"] = cnt["bull"] / nb; f["n_weak"] = cnt["weak"] / nb
            f["breadth_prev"] = sum(1 for r in rets if r > 0) / nb - 0.5
            f["dispersion"] = float(np.std(rets))
            ab = sorted((abs(v) for v in sec.values()), reverse=True)
            for k in ("IT", "BANKS", "FIN", "ENERGY", "OTHER"):
                f["sec_" + k] = sec.get(k, 0.0)
            f["gap_basket_sx"] = f["w_vs_prev"] - f["sx_vs_prev"]
            if cols is None:
                cols = list(f)
            # targets from t+1 (delayed entry), index and tradeable
            e = j + 1; tgt = {}
            for H in HORIZONS:
                x = i_close if H == "close" else min(e + H, i_close)
                if x <= e:
                    continue
                te, tx = ts[e], ts[x]
                opt = {}
                for side, sg in (("CALL", 1), ("PUT", -1)):
                    M = SX[("SENSEX", side)]
                    if te in M and tx in M:
                        opt[side] = M[tx][1] + (0.0 if M[tx][0] == M[te][0] else 0.5 * (M[tx][0] - M[te][0]) * sg) - M[te][1]
                tgt[H] = (cl[x] - cl[e], opt.get("CALL"), opt.get("PUT"))
            rows.append([f[k] for k in cols]); meta.append((d, tgt))
    return cols, np.array(rows, float), meta


def walk_forward(X, meta, H, model_name, flip=None):
    from sklearn.ensemble import HistGradientBoostingRegressor
    months = sorted({m[0][:7] for m in meta})
    y = np.array([m[1][H][0] if H in m[1] else np.nan for m in meta], float)
    if flip is not None:
        y = y * np.array([-1 if flip[m[0]] else 1 for m in meta])
    mon = np.array([m[0][:7] for m in meta])
    pred = np.full(len(y), np.nan)
    for k, mo in enumerate(months):
        if k < 3:
            continue
        tr = (mon < mo) & ~np.isnan(y); te = mon == mo
        if model_name == "ridge":
            mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
            A = (X[tr] - mu) / sd; lam = 10.0
            beta = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ (y[tr] - y[tr].mean()))
            pred[te] = ((X[te] - mu) / sd) @ beta + y[tr].mean()
        else:
            mdl = HistGradientBoostingRegressor(max_iter=150, learning_rate=0.05, max_leaf_nodes=15,
                                                min_samples_leaf=300, l2_regularization=1.0, random_state=0)
            mdl.fit(X[tr], y[tr]); pred[te] = mdl.predict(X[te])
    return pred, y


def score(pred, y, meta, H, half, top=0.1):
    ok = ~np.isnan(pred) & ~np.isnan(y)
    res = {}
    for hn, sel in (("H1", np.array([m[0] < half for m in meta])), ("H2", np.array([m[0] >= half for m in meta]))):
        m = ok & sel
        if m.sum() < 50:
            continue
        p, t = pred[m], y[m]
        thr = np.percentile(np.abs(p), 100 * (1 - top))
        s = np.abs(p) >= thr
        idx = np.where(m)[0][s]
        idx_dir = np.sign(pred[idx])
        opt = []
        for k, sg in zip(idx, idx_dir):
            tg = meta[k][1][H]
            v = tg[1] if sg > 0 else tg[2]
            if v is not None:
                opt.append(v - COST_OPT)
        fut = (np.sign(p[s]) * t[s]) - COST_FUT
        res[hn] = dict(n=int(m.sum()), acc=100 * np.mean(np.sign(p) == np.sign(t)), corr=float(np.corrcoef(p, t)[0, 1]),
                       top_n=int(s.sum()), top_pts=float((np.sign(p[s]) * t[s]).mean()),
                       opt=float(np.mean(opt)) if opt else float("nan"), fut=float(fut.mean()))
    return res


def main():
    cols, X, meta = build(sys.argv[1], sys.argv[2], sys.argv[3])
    days = sorted({m[0] for m in meta}); half = days[len(days) // 2]
    print("decision points %d (every 5 min), features %d, days %d, halves split at %s" % (len(X), len(cols), len(days), half))
    print("Walk-forward (monthly retrain on past months only), entry at t+1. Top 10% most confident predictions traded.")
    print("%-6s %-8s | %-58s | %-58s" % ("H", "model", "first half (Jan-Apr 2026 tested)", "second half (Apr-Oct 2026)"))
    for H in HORIZONS:
        for mn in ("ridge", "boosting"):
            pred, y = walk_forward(X, meta, H, mn)
            r = score(pred, y, meta, H, half)
            line = "%-6s %-8s" % (H, mn)
            for hn in ("H1", "H2"):
                if hn in r:
                    q = r[hn]
                    line += " | right %4.1f%% corr %+.3f top10%%: SENSEX %+6.1f pts, option net %+6.1f, future net %+6.1f" % (
                        q["acc"], q["corr"], q["top_pts"], q["opt"], q["fut"])
            print(line)
    print("\nChance baseline (each day's outcome randomly sign-flipped), boosting, 3 runs per horizon:")
    random.seed(5)
    for H in HORIZONS:
        accs = []
        for _ in range(3):
            flip = {d: random.random() < 0.5 for d in days}
            pred, y = walk_forward(X, meta, H, "boosting", flip)
            r = score(pred, y, meta, H, half)
            accs.append("%.1f%%/%.1f%%" % (r.get("H1", {}).get("acc", float("nan")), r.get("H2", {}).get("acc", float("nan"))))
        print("  H %-6s sign right H1/H2: %s" % (H, ", ".join(accs)))
    print("\nOption net = buying the index ATM CE/PE at t+1 and selling at t+1+H (real premiums) minus %g pts; "
          "future net = index move minus %g pts. Index points per unit; multiply by the lot size for Rs." % (COST_OPT, COST_FUT))


if __name__ == "__main__":
    main()
