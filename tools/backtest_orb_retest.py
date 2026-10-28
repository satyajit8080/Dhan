#!/usr/bin/env python3
"""Opening-range breakout + retest + heavyweight confirmation (the external-review strategy), real ATM premiums.
Local files only; no orders.

  python3 tools/backtest_orb_retest.py INDEX_CSV OPTION_HISTORY_DIR STOCK_DATA_DIR {nifty|sensex}

Rules (as proposed):
  opening range   high / low of the 1-min bars 09:15-09:30 (09:16-09:30 when the 09:15 bar is absent)
  trigger         a 5-min candle CLOSES above the range high (CE) / below the range low (PE)
  retest          a later 5-min candle trades back to the broken level and CLOSES beyond it again -> entry at that
                  candle's close (the option's close at the same minute); no retest -> no trade
  heavyweights    >= 70 % of the top-10 basket's weight above its own average price since the open (VWAP stand-in:
                  no stock volume in the data), and the basket's weighted 15-min return on the trade side
  option          ATM CE / PE (real rolling premiums; strike rolls bridged with delta 0.5)
  expiry          only with >= 3 trading days to expiry (also reported for all days)
  exits           structural stop: the index trades through the retest candle's low (CE) / high (PE); target: the
                  index moves 2R beyond entry (R = entry - retest low); time stop: 3 five-min candles without a new
                  favourable index extreme; flat by 15:15
  limits          max 2 trades a day; stop after 2 losses; entries 09:35-14:30
Costs per round trip (premium pts): NIFTY 1.5 (lot 65), SENSEX 5 (lot 20). Halves split at the middle day.
The option-chain check (liquidity, nearby OI, IV) is not tested: only the ATM strike's history is available.
"""
import glob, json, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from backtest_v3_rules import expiry_days                   # noqa: E402

CFG = {"nifty": dict(cost=1.5, lot=65, weights="docs/exports/nifty_influence_weights.json"),
       "sensex": dict(cost=5.0, lot=20, weights=None)}
SENSEX_W = {"HDFCBANK": 13.84, "RELIANCE": 10.61, "ICICIBANK": 9.52, "LT": 6.28, "BHARTIARTL": 5.62, "SBIN": 5.21,
            "INFY": 4.97, "AXISBANK": 3.75, "M&M": 3.36, "KOTAKBANK": 3.33}


def load_spots(sdir, names):
    sp = defaultdict(dict)
    for n in names:
        for f in glob.glob(os.path.join(sdir, "%s_CALL_*.json" % n)):
            p = json.load(open(f)); leg = p.get("ce") if isinstance(p, dict) else None
            if leg and leg.get("timestamp"):
                for t, s in zip(leg["timestamp"], leg["spot"]):
                    if s:
                        sp[n][int(t)] = s
    return sp


def five(bars):
    out = []
    for k, x in enumerate(bars):
        bk = (x["t"].hour * 60 + x["t"].minute - 555) // 5
        if out and out[-1]["bk"] == bk:
            c = out[-1]; c["high"] = max(c["high"], x["high"]); c["low"] = min(c["low"], x["low"]); c["close"] = x["close"]; c["j"] = k
        else:
            out.append(dict(bk=bk, high=x["high"], low=x["low"], close=x["close"], j=k))
    return out


def run(days, opt, spots, W, cost, lot, half, need_dte, use_hw, exp):
    ds = sorted(days)
    nxt = {}
    for d in ds:
        e = next((x for x in sorted(exp) if x >= d), None)
        nxt[d] = None if e is None else sum(1 for x in ds if d <= x < e)
    h = [[], []]; outcomes = defaultdict(int)
    for d in ds:
        if need_dte and (nxt[d] is None or nxt[d] < 3):
            continue
        bars = days[d]
        orb = [x for x in bars if x["t"].strftime("%H:%M") <= "09:29"]
        if len(orb) < 10:
            continue
        hi, lo = max(x["high"] for x in orb), min(x["low"] for x in orb)
        c5 = five(bars); trades = losses = 0; broke = {"CE": False, "PE": False}; busy_until = -1
        tw = {n: [] for n in W}                               # running average price per stock (VWAP stand-in)
        for ci, c in enumerate(c5):
            j = c["j"]; hm = bars[j]["t"].strftime("%H:%M")
            if hm < "09:34":
                continue
            for side in ("CE", "PE"):
                lvl = hi if side == "CE" else lo; sg = 1 if side == "CE" else -1
                if not broke[side]:
                    if (c["close"] - lvl) * sg > 0:
                        broke[side] = True
                    continue
                touched = (c["low"] <= lvl) if side == "CE" else (c["high"] >= lvl)
                if not touched or (c["close"] - lvl) * sg <= 0:
                    continue
                if j <= busy_until or trades >= 2 or losses >= 2 or hm > "14:30":
                    continue
                t = bars[j]["timestampMs"] // 1000; t15 = bars[max(0, j - 15)]["timestampMs"] // 1000
                if use_hw:
                    wa = ws = r15 = 0.0
                    for n, w in W.items():
                        s = spots.get(n, {})
                        vals = [s[bars[k]["timestampMs"] // 1000] for k in range(0, j + 1) if bars[k]["timestampMs"] // 1000 in s]
                        if not vals or t not in s:
                            continue
                        ws += w
                        if (s[t] - sum(vals) / len(vals)) * sg > 0:
                            wa += w
                        if t15 in s:
                            r15 += w * (s[t] - s[t15]) / s[t15]
                    if ws == 0 or wa / ws < 0.7 or r15 * sg <= 0:
                        continue
                leg = opt.get((d, side)) or {}
                if t not in leg:
                    continue
                K = leg[t][0]; e_opt = leg[t][4]; e_ix = c["close"]
                stop_ix = c["low"] if side == "CE" else c["high"]
                R = abs(e_ix - stop_ix)
                if R <= 0:
                    continue
                tgt_ix = e_ix + sg * 2 * R
                best = e_ix; since = 0; res = "TIME"; k_exit = None
                for k in range(j + 1, len(bars)):
                    x = bars[k]
                    if x["t"].strftime("%H:%M") >= "15:15":
                        k_exit = k; res = "EOD"; break
                    if (x["low"] <= stop_ix) if sg > 0 else (x["high"] >= stop_ix):
                        k_exit = k; res = "STOP"; break
                    if (x["high"] >= tgt_ix) if sg > 0 else (x["low"] <= tgt_ix):
                        k_exit = k; res = "TARGET"; break
                    ext = x["high"] if sg > 0 else x["low"]
                    if (ext - best) * sg > 0:
                        best = ext; since = 0
                    else:
                        since += 1
                    if since >= 15:
                        k_exit = k; res = "TIME"; break
                if k_exit is None:
                    k_exit = len(bars) - 1
                tx = bars[k_exit]["timestampMs"] // 1000
                if tx not in leg:
                    continue
                k2 = leg[tx][0]
                exit_opt = leg[tx][4] + (0.0 if k2 == K else 0.5 * (k2 - K) * sg)
                pnl = exit_opt - e_opt - cost
                h[d >= half].append(pnl); outcomes[res] += 1
                trades += 1; losses += pnl < 0; busy_until = k_exit
    return h, outcomes


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); kind = sys.argv[4]; cfg = CFG[kind]
    W = json.load(open(cfg["weights"])) if cfg["weights"] else SENSEX_W
    W = dict(sorted(W.items(), key=lambda kv: -kv[1])[:10])
    spots = load_spots(sys.argv[3], W)
    exp = expiry_days(opt); ds = sorted(days); half = ds[len(ds) // 2]
    print("%s: days %d, expiry days %d, basket %s, cost %.1f pts, lot %d"
          % (kind.upper(), len(ds), len(exp), ",".join(W), cfg["cost"], cfg["lot"]))
    for name, dte, hw in (("price only (ORB + retest), all days", False, False),
                          ("price only, >= 3 days to expiry", True, False),
                          ("+ heavyweights, all days", False, True),
                          ("+ heavyweights, >= 3 days to expiry (as proposed)", True, True)):
        h, oc = run(days, opt, spots, W, cfg["cost"], cfg["lot"], half, dte, hw, exp)
        a = h[0] + h[1]
        if not a:
            print("%-52s no trades" % name); continue
        net = [sum(x) * cfg["lot"] for x in h]
        wins = [p for p in a if p > 0]; los = [p for p in a if p <= 0]
        print("%-52s | n %3d (H1 %3d / H2 %3d) | win %4.1f%% | avg win %+6.1f avg loss %+6.1f pts | exits %s | "
              "net Rs/lot H1 %+8.0f / H2 %+8.0f / total %+8.0f"
              % (name, len(a), len(h[0]), len(h[1]), 100 * len(wins) / len(a), sum(wins) / max(1, len(wins)),
                 sum(los) / max(1, len(los)), dict(oc), net[0], net[1], net[0] + net[1]))


if __name__ == "__main__":
    main()
