#!/usr/bin/env python3
"""Run the SENSEX prediction tests on NIFTY 50 instead. Local files only; no orders.

  python3 tools/run_nifty_tests.py NIFTY_DIR STOCK_DATA_DIR OUT_DIR

NIFTY_DIR holds IDX_*.json (Dhan /charts/intraday, NIFTY 50 = NSE IDX_I id 13, 1-min) and OPT_1_ATM_{CALL,PUT}_*.json
(Dhan rolling nearest-weekly ATM options, NSE_FNO OPTIDX). STOCK_DATA_DIR holds the stock rolling-option files.
Steps:
  1. NIFTY 1-min CSV (Epoch,Timestamp,Open,High,Low,Close,Volume) in OUT_DIR.
  2. Each stock's influence on NIFTY estimated from the data: non-negative least squares of NIFTY 1-min % returns on
     the stocks' 1-min % returns (first half of the year only), scaled to sum to 100 -> OUT_DIR/nifty_weights.json.
     Avoids guessing index weights; also prints the top movers.
  3. Runs tools/predict_next_candle.py and tools/final_predict_sensex.py with those weights and NIFTY costs
     (lot 65: ~1.5 pts per ATM option round trip, ~13 pts per future round trip incl. 0.05 % futures STT).
"""
import csv, datetime, glob, json, os, subprocess, sys
from collections import defaultdict

import numpy as np

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ndir, sdir, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    bars = {}
    for f in glob.glob(os.path.join(ndir, "IDX_*.json")):
        p = json.load(open(f))
        for i, t in enumerate(p.get("timestamp") or []):
            bars[int(t)] = (p["open"][i], p["high"][i], p["low"][i], p["close"][i])
    csvp = os.path.join(out, "nifty_1min.csv")
    with open(csvp, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["Epoch", "Timestamp", "Open", "High", "Low", "Close", "Volume"])
        for t in sorted(bars):
            dt = datetime.datetime.fromtimestamp(t, IST)
            if "09:15" <= dt.strftime("%H:%M") <= "15:29":
                w.writerow([t, dt.strftime("%Y-%m-%d %H:%M:%S")] + list(bars[t]) + [""])
    days = sorted({datetime.datetime.fromtimestamp(t, IST).date().isoformat() for t in bars})
    print("NIFTY 1-min bars %d, days %d (%s .. %s) -> %s" % (len(bars), len(days), days[0], days[-1], csvp))
    # stock prices per minute
    spot = defaultdict(dict)
    for f in glob.glob(os.path.join(sdir, "*_CALL_*.json")):
        name = os.path.basename(f).rsplit("_", 2)[0]
        p = json.load(open(f)); leg = p.get("ce") if isinstance(p, dict) else None
        if leg and leg.get("timestamp"):
            for t, s in zip(leg["timestamp"], leg["spot"]):
                if s:
                    spot[name][int(t)] = s
    names = sorted(spot)
    half = days[len(days) // 2]
    ts = [t for t in sorted(bars) if datetime.datetime.fromtimestamp(t, IST).date().isoformat() < half]
    X, y = [], []
    for a, b in zip(ts, ts[1:]):
        if b - a != 60 or not all(a in spot[n] and b in spot[n] for n in names):
            continue
        y.append(100 * (bars[b][3] - bars[a][3]) / bars[a][3])
        X.append([100 * (spot[n][b] - spot[n][a]) / spot[n][a] for n in names])
    X, y = np.array(X), np.array(y)
    from sklearn.linear_model import LinearRegression
    reg = LinearRegression(positive=True, fit_intercept=False).fit(X, y)
    wts = {n: float(c) for n, c in zip(names, reg.coef_)}
    tot = sum(wts.values())
    wts = {n: round(100 * v / tot, 3) for n, v in sorted(wts.items(), key=lambda kv: -kv[1]) if v > 0}
    r2 = reg.score(X, y)
    json.dump(wts, open(os.path.join(out, "nifty_weights.json"), "w"), indent=1)
    print("estimated NIFTY influence (%% of the 1-min move, NNLS on %d minutes, R^2 %.3f), top 15:" % (len(y), r2))
    for n, v in list(wts.items())[:15]:
        print("  %-11s %5.2f %%" % (n, v))
    env = dict(os.environ, WEIGHTS_JSON=os.path.join(out, "nifty_weights.json"), COST_OPT="1.5", COST_FUT="13")
    py = sys.executable
    for script in ("predict_next_candle.py", "final_predict_sensex.py"):
        print("\n" + "=" * 20 + " " + script + " on NIFTY " + "=" * 20, flush=True)
        subprocess.run([py, os.path.join(HERE, script), csvp, ndir, sdir], env=env, check=False)


if __name__ == "__main__":
    main()
