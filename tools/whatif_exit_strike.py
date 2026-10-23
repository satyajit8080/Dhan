#!/usr/bin/env python3
"""Offline what-if (python3 tools/whatif_exit_strike.py LABEL=events.jsonl ...) on recorded paper trades (FIRST_TOUCH legs). No look-ahead:
 A = as recorded; B = also exit at the first 1-min index close back through the trigger (bar assumed to close
 60 s after its label = conservative), at the last recorded bid at or before that time;
 C = only the strike nearest the index at the crossing; D = B + C."""
import json, sys, collections
from datetime import datetime, timedelta
sys.path.insert(0, "engine")
import refresh_table as rt
res = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0, 0.0]))
for label, path in (a.split("=", 1) for a in sys.argv[1:]):
    ev = [json.loads(l) for l in open(path)]
    scans = [e for e in ev if e["event"] == "scan"]
    bars = {}
    for s in scans:
        for t, o, h, l, c in (s["observation"].get("oneMinuteIndex") or {}).get("lastBars", []): bars[t] = c
    ent = {e["paperId"]: e for e in ev if e["event"] == "paper_entry"}
    g = collections.OrderedDict()
    for x in ev:
        if x["event"] == "paper_exit" and x.get("variant", "FIRST_TOUCH") == "FIRST_TOUCH":
            g.setdefault((ent[x["paperId"]]["ts"], x["side"], x["trigger"]), []).append(x)
    for (ts, side, trig), legs in g.items():
        sc = None
        for s in scans:
            if s["startedIst"] <= ts and (s["observation"].get("levels") or {}).get("resistances") is not None: sc = s
        up = side == "CE"; sg = 1 if up else -1
        cands = [l for l in sc["observation"]["levels"]["resistances" if up else "supports"] if set(l["sources"]) != {"vwap"}
                 and rt._round_to(l["price"] + (5 if up else -5), 5.0) == trig]
        cands.sort(key=lambda l: (l["touches"] < 2, abs(l["price"] - trig)))
        grp = "fresh" if cands and cands[0]["touches"] < 3 else "old"
        t0 = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        # first 1-min close back through the trigger after entry: seconds after entry when known
        back = None
        for k in sorted(bars):
            close_at = datetime.combine(t0.date(), datetime.strptime(k, "%H:%M").time()) + timedelta(seconds=60)
            if close_at <= t0 or close_at > t0 + timedelta(seconds=600): continue
            if (bars[k] - trig) * sg < 0:
                back = (close_at - t0).total_seconds(); break
        idx = ent[legs[0]["paperId"]]["indexAtCross"]
        near = min(legs, key=lambda x: abs(x["strike"] - idx))
        for x in legs:
            e = ent[x["paperId"]]
            a = x["pnlPts"]; win_a = x["outcome"] == "TAKE_PROFIT"
            b, win_b = a, win_a
            if back is not None and back < x["heldS"]:
                bid = e["entryBid"]
                for s, p in x["path"]:
                    if s <= back: bid = p
                b = round(bid - e["entryAsk"], 2); win_b = b > 0
            for name, pnl, w, use in (("A recorded", a, win_a, True), ("B exit on close back", b, win_b, True),
                                      ("C nearest strike", a, win_a, x is near), ("D B+C", b, win_b, x is near)):
                if not use: continue
                for key in (grp, "all", grp + " " + label):
                    r = res[key][name]; r[0] += 1; r[1] += w; r[2] += pnl
for key in ["fresh", "old", "all"] + sorted(k for k in res if " " in k and k.startswith("fresh")):
    print(key)
    for name, (n, w, p) in sorted(res[key].items()):
        print("   %-22s trades %4d win%% %5.1f pts %8.1f  per trade %+.2f" % (name, n, 100 * w / n, p, p / n))
