#!/usr/bin/env python3
"""Re-test breakout signals on REAL expired-option premiums (Dhan rolling ATM, nearest weekly = expiry number 1).

  python3 tools/backtest_real_options.py candles.csv HISTORY_DIR [--series 1] [--csv out.csv]

Signals come from the 1-min index file exactly as in backtest_fresh_extreme.py / backtest_break_retest.py:
  FRESH_EXTREME  fresh level (2 touches) whose trigger is a new session high/low
  FRESH_CALM     FRESH_EXTREME, not Thursday, index moved <= 15 pts into the break over the 3 prior closes
  BREAK_RETEST   fresh break -> 1-min close beyond -> retest within 5 min (level holds) -> resume entry
Trade: ATM CALL for CE / ATM PUT for PE at the rolling-ATM strike of the entry minute, +6 / -11 premium,
10-min time stop; when one bar reaches both, the stop counts (conservative). Two entry prices are reported:
  close  = option close of the entry minute (up to one minute late)
  cross  = option open of the entry minute moved by 0.5 x (index entry - index open) (the paper tracker's moment)
The rolling series changes strike when spot moves; for those minutes the held strike's price is estimated from
the new ATM price with delta 0.5 (counted as 'adjusted bars'). Costs: slippage on stops, Rs 70 per trade, lot 20.
No orders, no API calls: this reads local files only.
"""
import argparse, csv, datetime, json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402

TP, SL, HOLD, LOT, CHARGES = 6.0, 11.0, 10, 20, 70.0


def load_options(hdir, series):
    """{(day, 'CE'|'PE'): {epoch_minute_s: (strike, o, h, l, c)}}"""
    out = {}
    for f in Path(hdir).glob("OPT_%d_ATM_*.json" % series):
        rec = json.load(open(f))
        side = "CE" if rec["series"][2] == "CALL" else "PE"
        leg = rec["payload"].get(side.lower()) or {}
        ts = leg.get("timestamp") or []
        if not ts:
            continue
        out[(rec["from"], side)] = {t: (leg["strike"][i], leg["open"][i], leg["high"][i], leg["low"][i], leg["close"][i])
                                    for i, t in enumerate(ts)}
    return out


def fresh_extreme_signals(days):
    sig = []
    for d in sorted(days):
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        thu = datetime.date.fromisoformat(d).weekday() == 3
        for e in b.day_events(bars):
            if not (e["fresh"] and e["extreme"]):
                continue
            j = idx[e["time"]]; up = e["side"] == "CE"; sg = 1 if up else -1
            entry = max(e["trigger"], bars[j]["open"]) if up else min(e["trigger"], bars[j]["open"])
            mom3 = (bars[j - 1]["close"] - bars[j - 4]["close"]) * sg if j >= 4 else None
            calm = (not thu) and mom3 is not None and mom3 <= 15
            sig.append(dict(day=d, j=j, side=e["side"], ientry=entry, rule="FRESH_EXTREME"))
            if calm:
                sig.append(dict(day=d, j=j, side=e["side"], ientry=entry, rule="FRESH_CALM"))
    return sig


def break_retest_signals(days):
    sig = []
    for d in sorted(days):
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        for e in b.day_events(bars):
            if not e["fresh"]:
                continue
            j = idx[e["time"]]; up = e["side"] == "CE"; sg = 1 if up else -1; trig = e["trigger"]; lvl = trig - 5 * sg
            k = next((k for k in range(j, min(len(bars), j + 3)) if (bars[k]["close"] - trig) * sg > 0), None)
            if k is None:
                continue
            r = None
            for q in range(k + 1, min(len(bars), k + 6)):
                x = bars[q]
                if (x["close"] - lvl) * sg < 0:
                    break
                if (x["low"] <= trig) if up else (x["high"] >= trig):
                    r = q; break
            if r is None:
                continue
            ent = None
            for q in range(r + 1, min(len(bars), r + 4)):
                x = bars[q]
                if (x["close"] - lvl) * sg < 0:
                    break
                if (x["high"] > bars[r]["high"]) if up else (x["low"] < bars[r]["low"]):
                    ent = (q, bars[r]["high"] if up else bars[r]["low"]); break
            if ent is None or b.rt.in_skip_window(bars[ent[0]]["t"]):
                continue
            sig.append(dict(day=d, j=ent[0], side=e["side"], ientry=ent[1], rule="BREAK_RETEST"))
    return sig


def trade(days, opt, s, mode, slip):
    bars = days[s["day"]]; leg = opt.get((s["day"], s["side"]))
    if not leg:
        return None
    t0 = bars[s["j"]]["timestampMs"] // 1000
    if t0 not in leg:
        return None
    K = leg[t0][0]; adj = 0
    cs = 1 if s["side"] == "CE" else -1                                  # call price falls with strike, put rises

    def px(t):
        nonlocal adj
        k2, o, h, l, c = leg[t]
        if k2 == K:
            return o, h, l, c
        adj += 1
        sh = 0.5 * (k2 - K) * cs
        return o + sh, h + sh, l + sh, c + sh

    o, h, l, c = px(t0)
    sg = 1 if s["side"] == "CE" else -1
    entry = c if mode == "close" else max(0.05, o + 0.5 * (s["ientry"] - bars[s["j"]]["open"]) * sg)
    last = entry
    start = s["j"] + (1 if mode == "close" else 0)
    for m, k in enumerate(range(start, min(len(bars), s["j"] + HOLD + 1))):
        t = bars[k]["timestampMs"] // 1000
        if t not in leg:
            continue
        o, h, l, c = px(t)
        if mode == "cross" and m == 0:                                    # entry minute: only its close is known
            lo, hi = min(entry, c), c
        else:
            lo, hi = l, h
        if entry - lo >= SL:
            return dict(pts=-SL - slip, res="SL", adj=adj, prem=entry)
        if hi - entry >= TP:
            return dict(pts=TP, res="TP", adj=adj, prem=entry)
        last = c
    return dict(pts=round(last - entry, 2), res="TIME", adj=adj, prem=entry)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile"); ap.add_argument("hdir")
    ap.add_argument("--series", type=int, default=1)
    ap.add_argument("--csv")
    a = ap.parse_args()
    days = b.load(a.csvfile)
    opt = load_options(a.hdir, a.series)
    sig = fresh_extreme_signals(days) + break_retest_signals(days)
    ds = sorted(days); half = ds[len(ds) // 2]
    print("index days %d, option days %d, signals %d, halves split at %s" % (len(ds), len({d for d, _ in opt}), len(sig), half))
    rows = []
    for rule in ("FRESH_EXTREME", "FRESH_CALM", "BREAK_RETEST"):
        g = [s for s in sig if s["rule"] == rule]
        for mode in ("cross", "close"):
            for slip in (0, 4):
                h = [[], []]; miss = 0; adj = 0
                for s in g:
                    r = trade(days, opt, s, mode, slip)
                    if r is None:
                        miss += 1; continue
                    adj += r["adj"] > 0
                    h[s["day"] >= half].append(r["pts"])
                    if slip == 4 and mode == "cross":
                        rows.append(dict(rule=rule, day=s["day"], time=days[s["day"]][s["j"]]["t"].strftime("%H:%M"),
                                         side=s["side"], premium=round(r["prem"], 2), result=r["res"], pts=r["pts"]))
                allp = h[0] + h[1]; n = len(allp)
                if not n:
                    continue
                win = 100 * sum(p > 0 for p in allp) / n
                net = [sum(p * LOT for p in x) - CHARGES * len(x) for x in h]
                print("%-13s entry=%-5s slip%d | n %4d (H1 %3d / H2 %3d, no data %d, strike-adjusted %d) | win %4.1f%% | "
                      "pts %+7.1f (H1 %+6.1f / H2 %+6.1f) | net 1 lot Rs %+8.0f (H1 %+7.0f / H2 %+7.0f)"
                      % (rule, mode, slip, n, len(h[0]), len(h[1]), miss, adj, win, sum(allp), sum(h[0]), sum(h[1]),
                         net[0] + net[1], net[0], net[1]))
    if a.csv and rows:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    main()
