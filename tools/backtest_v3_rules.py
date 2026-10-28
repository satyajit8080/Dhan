#!/usr/bin/env python3
"""Scalping rules v3 (bull50-dhan sensex-scalp) on 1 year of SENSEX 1-min bars + REAL expired ATM option premiums.

  python3 tools/backtest_v3_rules.py candles.csv HISTORY_DIR

Levels: the same replica as backtest_fresh_extreme.py (5-min candles, >= 2 touches, trigger = level +/- 5, rounded 5).
v3 entry: a 1-min close beyond the trigger (crossing bar or next 2), the NEXT 1-min candle also closes beyond it
("holds"); enter at that candle's close (ATM option close of the same minute). The v3 volume filter is NOT tested:
the index file has no volume and the rolling option files carry empty volume arrays.
Filters: no-trade windows; theta filter approximated as "not on expiry day" (ATM 10-min decay > 1 % there);
spread and IV filters pass for the ATM strike and are not modelled. Exit: stop = 1-min index close back through
the level; target = index moves 1.5 x the stop distance (entry - level); time stop 10 min. At most 3 losing trades a
day. P&L in real option premium (rolling ATM; strike changes adjusted with delta 0.5). Costs: slippage per trade
and Rs 70 per trade, lot 20. Reads local files only; no orders, no API calls.
"""
import argparse, datetime, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
from backtest_real_options import load_options              # noqa: E402

HOLD, LOT, CHARGES = 10, 20, 70.0


def hm(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%H:%M")


def expiry_days(opt):
    """Expiry = the next day's opening ATM straddle is more than 2x this day's last-hour straddle (roll rule)."""
    st = {}
    for (d, side), leg in opt.items():
        for t, (k, o, h, l, c) in leg.items():
            st.setdefault(d, {}).setdefault(t, {})[side] = c
    days = sorted(st)
    out = set()
    for a, nxt in zip(days, days[1:]):
        last = [v["CE"] + v["PE"] for t, v in st[a].items() if len(v) == 2 and hm(t) >= "14:30"]
        first = [v["CE"] + v["PE"] for t, v in sorted(st[nxt].items()) if len(v) == 2][:5]
        if last and first and sum(first) / len(first) > 2 * (sum(last) / len(last)):
            out.add(a)
    return out


def signals(days, exp):
    sig = []
    for d in sorted(days):
        if d in exp:
            continue
        bars = days[d]; idx = {x["t"].strftime("%H:%M"): k for k, x in enumerate(bars)}
        for e in b.day_events(bars):                        # every armed level (>= 2 touches), fresh or old
            j = idx[e["time"]]; up = e["side"] == "CE"; sg = 1 if up else -1
            trig = e["trigger"]; lvl = trig - 5 * sg
            k = next((k for k in range(j, min(len(bars), j + 3)) if (bars[k]["close"] - trig) * sg > 0), None)
            if k is None or k + 1 >= len(bars) or (bars[k + 1]["close"] - trig) * sg <= 0:
                continue
            q = k + 1                                       # entry candle (the hold)
            if b.rt.in_skip_window(bars[q]["t"]):
                continue
            sig.append(dict(day=d, q=q, side=e["side"], lvl=lvl, ient=bars[q]["close"], touches=e["touches"]))
    return sig


def trade(days, opt, s, slip, RR=1.5):
    bars = days[s["day"]]; leg = opt.get((s["day"], s["side"]))
    t0 = bars[s["q"]]["timestampMs"] // 1000
    if not leg or t0 not in leg:
        return None
    K = leg[t0][0]; cs = 1 if s["side"] == "CE" else -1; sg = cs

    def px(t):
        k2, o, h, l, c = leg[t]
        sh = 0.0 if k2 == K else 0.5 * (k2 - K) * cs
        return o + sh, h + sh, l + sh, c + sh

    entry = px(t0)[3]
    risk = (s["ient"] - s["lvl"]) * sg
    tgt = s["ient"] + sg * RR * risk
    last = entry; res = "TIME"
    for k in range(s["q"] + 1, min(len(bars), s["q"] + HOLD + 1)):
        t = bars[k]["timestampMs"] // 1000
        if t not in leg:
            continue
        o, h, l, c = px(t); x = bars[k]
        if ((x["high"] >= tgt) if sg > 0 else (x["low"] <= tgt)):          # index reached target: option at most its high
            last = min(h, entry + 0.5 * RR * risk); res = "TP"; break
        if (x["close"] - s["lvl"]) * sg < 0:                                # 1-min close back through the level
            last = c; res = "SL"; break
        last = c
    return dict(pts=round(last - entry - slip, 2), res=res, risk=risk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile"); ap.add_argument("hdir")
    a = ap.parse_args()
    days = b.load(a.csvfile)
    opt = load_options(a.hdir, 1)
    exp = expiry_days(opt)
    ds = sorted(days); half = ds[len(ds) // 2]
    print("index days %d, expiry days skipped %d, halves split at %s" % (len(ds), len(exp), half))
    sig = signals(days, exp)
    for rr in (1.0, 1.5, 2.0):
        for slip in (0.0, 1.5, 3.0):
            h = [[], []]; perday = {}; res = {"TP": 0, "SL": 0, "TIME": 0}
            for s in sig:
                if perday.get(s["day"], 0) >= 3:                          # 3 losing scalps -> stop for the day
                    continue
                r = trade(days, opt, s, slip, rr)
                if r is None:
                    continue
                res[r["res"]] += 1
                if r["pts"] < 0:
                    perday[s["day"]] = perday.get(s["day"], 0) + 1
                h[s["day"] >= half].append(r["pts"])
            allp = h[0] + h[1]; n = len(allp)
            if not n:
                continue
            net = [sum(p * LOT for p in x) - CHARGES * len(x) for x in h]
            wins = [p for p in allp if p > 0]; loss = [p for p in allp if p <= 0]
            print("v3 target %.1fx stop, slip %.1f | n %4d (H1 %3d / H2 %3d) TP %d SL %d TIME %d | win %4.1f%% avg win %+.1f avg loss %+.1f | "
                  "pts H1 %+7.1f / H2 %+7.1f | net 1 lot Rs H1 %+8.0f / H2 %+8.0f / total %+8.0f"
                  % (rr, slip, n, len(h[0]), len(h[1]), res["TP"], res["SL"], res["TIME"],
                     100 * len(wins) / n, sum(wins) / max(1, len(wins)), sum(loss) / max(1, len(loss)),
                     sum(h[0]), sum(h[1]), net[0], net[1], net[0] + net[1]))


if __name__ == "__main__":
    main()
