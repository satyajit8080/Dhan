#!/usr/bin/env python3
"""Back-test of the SENSEX-chat pick (28 Oct 14:21, 73300 PE "breakout below 73,245"): nearest strong level + stock
breadth + direction vs previous close, on 1 year of SENSEX 1-min bars, real expired ATM premiums and 30 stocks.

  python3 tools/backtest_nearest_level.py candles.csv HISTORY_DIR STOCK_DATA_DIR

Rules, as described by the chat:
  levels     today's 5-min candles, >= 2 touches (same replica as backtest_fresh_extreme.py), trigger = level -/+ 5
  side       only the side whose armed trigger is NEARER to the price at the previous minute
  breadth    PE needs < 50 % of the 30 stocks above their previous close, CE needs > 50 %
  direction  PE needs SENSEX below its previous close, CE above
  entry      ATM PE / CE at the crossing of that trigger ('cross' entry, real premiums), no-trade windows respected
  exit       +6 / -11 premium with a 10-min time stop (refresh rules); also +10 / -10 15 min and +20 / -10 30 min
Variants: all levels or strong levels only (>= 4 touches); filters on/off. Halves split at 28 Apr 2026.
Costs: slippage 0 or 4 pts on stops, Rs 70 per trade, lot 20. Local files only; no orders.
"""
import datetime, glob, json, os, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402
from sensex.levels import derive_levels                     # noqa: E402
from sensex.scan import selected_levels                     # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


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


def events(days, spots):
    """Every armed-trigger crossing with: nearer-side flag, breadth, direction vs previous close, touches."""
    ds = sorted(days); out = []
    names = sorted(spots)
    for di, d in enumerate(ds):
        if di == 0:
            continue
        bars = days[d]; pbars = days[ds[di - 1]]
        pcs = pbars[-1]["close"]
        pts = [x["timestampMs"] // 1000 for x in pbars]
        st_pc = {n: next((spots[n][t] for t in reversed(pts) if t in spots[n]), None) for n in names}
        fired = set()
        for i in range(len(bars) - 1):
            c5 = b.five_min(bars[:i + 1])
            if len(c5) < 2 or b.rt.in_skip_window(bars[i + 1]["t"]):
                continue
            spot = bars[i]["close"]
            lv = derive_levels(c5, spot, confirmation_buffer=5, min_touches=2, round_to=5)
            scan = {"status": "OK", "candlesMs": c5[-1]["timestampMs"], "candleRefPrice": spot,
                    "resistances": [{"price": l["price"], "touches": l["touches"], "sources": list(l["sources"])} for l in lv["allResistance"]],
                    "supports": [{"price": l["price"], "touches": l["touches"], "sources": list(l["sources"])} for l in lv["allSupport"]]}
            sel = {s: v for s, v in selected_levels(scan).items() if not v["weak"]}
            if not sel:
                continue
            dist = {s: abs(v["trigger"] - spot) for s, v in sel.items()}
            near = min(dist, key=dist.get)
            nb = bars[i + 1]; prev = spot; t = nb["timestampMs"] // 1000
            for side, s in sel.items():
                trig = s["trigger"]; up = side == "CE"
                if (side, trig) in fired or not ((prev < trig <= nb["high"]) if up else (prev > trig >= nb["low"])):
                    continue
                fired.add((side, trig))
                above = n = 0
                for nm in names:
                    v = spots[nm].get(t) or spots[nm].get(t - 60)
                    if v and st_pc.get(nm):
                        n += 1; above += v > st_pc[nm]
                entry = max(trig, nb["open"]) if up else min(trig, nb["open"])
                out.append(dict(day=d, j=i + 1, side=side, ientry=entry, rule="NEAR", touches=s["touches"],
                                nearer=(side == near), breadth=(above / n if n else None),
                                below_prev=(spot < pcs)))
    return out


def trade(days, opt, s, slip, tp, sl, hold):
    ro.TP, ro.SL, ro.HOLD = tp, sl, hold
    return ro.trade(days, opt, s, "cross", slip)


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); spots = load_spots(sys.argv[3])
    ds = sorted(days); half = ds[len(ds) // 2]
    ev = events(days, spots)
    print("days %d, crossings %d, stocks %d, halves split at %s" % (len(ds), len(ev), len(spots), half))

    def chat_rule(e, strong):
        ok = e["nearer"] and e["breadth"] is not None
        ok = ok and ((e["breadth"] < 0.5 and e["below_prev"]) if e["side"] == "PE" else (e["breadth"] > 0.5 and not e["below_prev"]))
        return ok and (e["touches"] >= 4 if strong else True)
    sets = {
        "SENSEX-chat rule (nearer + breadth + direction)": lambda e: chat_rule(e, False),
        "SENSEX-chat rule, strong levels (>= 4 touches)": lambda e: chat_rule(e, True),
        "SENSEX-chat rule, PE only": lambda e: chat_rule(e, False) and e["side"] == "PE",
        "nearer side only (no breadth/direction)": lambda e: e["nearer"],
        "all crossings (FIRST_TOUCH-like)": lambda e: True,
    }
    print("break-even win rate: +6/-11 64.7 %, +10/-10 50 %, +20/-10 33.3 % (before costs)\n")
    for name, f in sets.items():
        g = [e for e in ev if f(e)]
        for tp, sl, hold in ((6, 11, 10), (10, 10, 15), (20, 10, 30)):
            for slip in (0, 4):
                h = [[], []]
                for e in g:
                    r = trade(days, opt, e, slip, tp, sl, hold)
                    if r is not None:
                        h[e["day"] >= half].append(r["pts"])
                a = h[0] + h[1]
                if not a:
                    continue
                net = [sum(p * ro.LOT for p in x) - ro.CHARGES * len(x) for x in h]
                print("%-50s +%d/-%d %2dm slip %d | n %4d (H1 %3d / H2 %3d) win %4.1f%% | net 1 lot Rs H1 %+8.0f / H2 %+8.0f / total %+8.0f"
                      % (name, tp, sl, hold, slip, len(a), len(h[0]), len(h[1]), 100 * sum(p > 0 for p in a) / len(a),
                         net[0], net[1], net[0] + net[1]))


if __name__ == "__main__":
    main()
