#!/usr/bin/env python3
"""Index-only back-test of FRESH_EXTREME on 1-min SENSEX candles (CSV: Epoch,Timestamp,Open,High,Low,Close,...).

  python3 tools/backtest_fresh_extreme.py candles.csv [--from 2025-10-27] [--to 2026-10-26] [--csv out.csv]

Replays the scanner once a minute with the same level logic (levels.derive_levels on 5-min candles built from the
1-min bars so far, scan.selected_levels / refresh_table.pick_level, triggers ±5 rounded to 5, no-trade windows),
then checks each following 1-min bar for a crossing of an armed trigger. FRESH_EXTREME = selected level has < 3
touches (and >= 2, else the row is WEAK_LEVEL and never armed) and the trigger is a new session high (CE) / low (PE).
Option P&L is APPROXIMATED from the index with delta 0.5: target +6 premium = +12 index pts, stop -11 = -22, 10-min
time stop; in a bar that reaches both, the stop is assumed first (conservative). No option prices are used.
"""
import argparse, csv, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import refresh_table as rt                                  # noqa: E402
from sensex.levels import derive_levels                     # noqa: E402
from sensex.scan import selected_levels                     # noqa: E402

DELTA, TP, SL, HOLD_MIN = 0.5, 6.0, 11.0, 10


def load(path):
    days = defaultdict(list)
    for r in csv.DictReader(open(path)):
        ts = datetime.strptime(r["Timestamp"], "%Y-%m-%d %H:%M:%S")
        if ts.hour == 9 and ts.minute == 15:
            continue                                            # Dhan's 1-min index series starts at 09:16
        days[ts.date().isoformat()].append({"t": ts, "timestampMs": int(r["Epoch"]) * 1000, "open": float(r["Open"]),
                                            "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])})
    return {d: sorted(b, key=lambda x: x["t"]) for d, b in days.items()}


def five_min(bars):
    out = []
    for b in bars:
        k = (b["t"].hour * 60 + b["t"].minute - 555) // 5          # buckets from 09:15
        if out and out[-1]["k"] == k:
            c = out[-1]
            c["high"], c["low"], c["close"] = max(c["high"], b["high"]), min(c["low"], b["low"]), b["close"]
        else:
            out.append({"k": k, "timestampMs": b["timestampMs"], "open": b["open"], "high": b["high"], "low": b["low"],
                        "close": b["close"], "volume": 0})
    return out


def day_events(bars):
    events, fired, arms = [], set(), {}
    for i in range(len(bars) - 1):
        now = bars[i]["t"]
        # scan after bar i closes: arm the OK rows (level >= 2 touches, outside the no-trade windows)
        arms = {}
        c5 = five_min(bars[:i + 1])
        if len(c5) >= 2 and not rt.in_skip_window(bars[i + 1]["t"]):
            spot = bars[i]["close"]
            lv = derive_levels(c5, spot, confirmation_buffer=5, min_touches=2, round_to=5)
            scan = {"status": "OK", "candlesMs": c5[-1]["timestampMs"], "candleRefPrice": spot,
                    "resistances": [{"price": l["price"], "touches": l["touches"], "sources": list(l["sources"])} for l in lv["allResistance"]],
                    "supports": [{"price": l["price"], "touches": l["touches"], "sources": list(l["sources"])} for l in lv["allSupport"]]}
            for side, s in selected_levels(scan).items():
                if not s["weak"] and (side, s["trigger"]) not in fired:
                    arms[(side, s["trigger"])] = s
        hi = max(b["high"] for b in bars[:i + 1]); lo = min(b["low"] for b in bars[:i + 1])
        nb, prev = bars[i + 1], bars[i]["close"]
        if rt.in_skip_window(nb["t"]):
            continue
        for (side, trig), s in arms.items():
            up = side == "CE"
            if not ((prev < trig <= nb["high"]) if up else (prev > trig >= nb["low"])):
                continue
            fired.add((side, trig))
            fresh = s["touches"] < 3
            extreme = trig >= hi if up else trig <= lo
            entry = max(trig, nb["open"]) if up else min(trig, nb["open"])
            events.append(dict(time=nb["t"].strftime("%H:%M"), side=side, trigger=trig, touches=s["touches"],
                               fresh=fresh, extreme=extreme, **outcome(bars, i + 1, entry, up)))
    return events


def outcome(bars, j, entry, up):
    sg = 1 if up else -1
    tgt, stp = TP / DELTA, SL / DELTA
    last = entry
    for k in range(j, min(len(bars), j + HOLD_MIN + 1)):
        b = bars[k]
        adverse = (entry - b["low"]) if up else (b["high"] - entry)
        favour = (b["high"] - entry) if up else (entry - b["low"])
        if k == j:                                              # entry bar: only moves after the crossing are unknown
            adverse = (entry - b["close"]) * 1 if up else (b["close"] - entry)
            favour = max(0.0, (b["close"] - entry) * sg)
        if adverse >= stp:
            return {"result": "SL", "pts": -SL}
        if favour >= tgt:
            return {"result": "TP", "pts": TP}
        last = b["close"]
    return {"result": "TIME", "pts": round(DELTA * (last - entry) * sg, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvfile")
    ap.add_argument("--from", dest="d0")
    ap.add_argument("--to", dest="d1")
    ap.add_argument("--csv")
    a = ap.parse_args()
    days = load(a.csvfile)
    rows = []
    for d in sorted(days):
        if (a.d0 and d < a.d0) or (a.d1 and d > a.d1):
            continue
        for e in day_events(days[d]):
            rows.append(dict(day=d, **e))
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    for name, sel in (("ALL breakouts", lambda r: True), ("FRESH (2 touches)", lambda r: r["fresh"]),
                      ("FRESH_EXTREME", lambda r: r["fresh"] and r["extreme"]), ("OLD levels", lambda r: not r["fresh"])):
        g = [r for r in rows if sel(r)]
        if not g:
            continue
        n = len(g); w = sum(r["pts"] > 0 for r in g); p = sum(r["pts"] for r in g)
        print("%-18s breakouts %5d  win %5.1f%%  premium pts/strike %+8.1f  per breakout %+.2f" % (name, n, 100 * w / n, p, p / n))
    fx = [r for r in rows if r["fresh"] and r["extreme"]]
    by_month = defaultdict(list)
    for r in fx:
        by_month[r["day"][:7]].append(r["pts"])
    print("FRESH_EXTREME by month:", "  ".join("%s %+.0f(%d)" % (m, sum(v), len(v)) for m, v in sorted(by_month.items())))
    dd = defaultdict(float)
    for r in fx:
        dd[r["day"]] += r["pts"]
    print("FRESH_EXTREME days with trades %d, positive %d, negative %d" % (len(dd), sum(v > 0 for v in dd.values()), sum(v < 0 for v in dd.values())))


if __name__ == "__main__":
    main()
