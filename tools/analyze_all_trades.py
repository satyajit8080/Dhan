#!/usr/bin/env python3
"""Cross-day analysis of PAPER trades from decoded Cloud events (facts only, no thresholds proposed).

  python3 tools/analyze_all_trades.py LABEL=path/events.jsonl [LABEL=path/events.jsonl ...] [--csv out.csv]

Unit = crossing event (one per day, variant, entry minute, side, trigger; up to 5 strikes). Entry-time features use
only data available before the entry (scan observation, closed 1-min bars). Exit-time facts (why it won/lost) use the
1-min index bar of the exit minute.
"""
import argparse, csv, json, statistics as st
from collections import defaultdict
from datetime import datetime, timedelta


def addm(t, m):
    return (datetime.strptime(t, "%H:%M") + timedelta(minutes=m)).strftime("%H:%M")


def load(label, path):
    ev = [json.loads(l) for l in open(path, encoding="utf-8")]
    scans = [e for e in ev if e["event"] == "scan"]
    bars = {}
    for s in scans:
        for t, o, h, l, c in (s["observation"].get("oneMinuteIndex") or {}).get("lastBars", []):
            bars[t] = (o, h, l, c)
    ent = {e["paperId"]: e for e in ev if e["event"] == "paper_entry"}
    groups = defaultdict(list)
    for x in ev:
        if x["event"] == "paper_exit":
            e = ent[x["paperId"]]
            groups[(x.get("variant", "FIRST_TOUCH"), e["ts"], x["side"], x["trigger"])].append((e, x))
    crossings = defaultdict(list)          # trigger -> entry times (first-touch variant), for retest counting
    for (var, ts, side, trig) in groups:
        if var == "FIRST_TOUCH":
            crossings[(side, trig)].append(ts)
    rows = []
    for (var, ts, side, trig), legs in sorted(groups.items(), key=lambda k: k[0][1]):
        up = side == "CE"
        sgn = 1 if up else -1
        t = ts[11:16]
        sc = None
        for s in scans:
            if s["startedIst"] <= ts:
                sc = s
        ob = sc["observation"]
        pr = ob.get("prices") or {}
        ff = ob.get("futuresFlow") or {}
        fo = pr.get("futuresOhlc") or {}
        # session range so far from closed bars before the entry minute
        prior = [b for k, b in bars.items() if k < t]
        hi = max(b[1] for b in prior) if prior else None
        lo = min(b[2] for b in prior) if prior else None
        last3 = [bars.get(addm(t, -i)) for i in (3, 2, 1)]
        mom = (last3[2][3] - last3[0][0]) * sgn if all(last3) else None      # 3-min move into the level, in side terms
        fut_day = (pr.get("futuresLtp") - fo["open"]) * sgn if pr.get("futuresLtp") and fo.get("open") else None
        vw = ff.get("futLtpMinusVwap")
        lv = ob.get("levels") or {}
        cands = lv.get("resistances" if up else "supports", [])
        ref = trig - 5 if up else trig + 5
        L = min(cands, key=lambda x: abs(x["price"] - ref)) if cands else None
        retest = sum(1 for x in crossings[(side, trig)] if x < ts) if var == "FIRST_TOUCH" else \
            sum(1 for x in crossings[(side, trig)] if x[11:16] < addm(t, -1))
        idx_cross = legs[0][0].get("indexAtCross")
        pnl = sum(x["pnlPts"] or 0 for _, x in legs)
        tp = sum(x["outcome"] == "TAKE_PROFIT" for _, x in legs)
        held = st.median(x["heldS"] for _, x in legs)
        # what the index did by the exit minute (median exit)
        exit_t = (datetime.strptime(ts[11:19], "%H:%M:%S") + timedelta(seconds=held)).strftime("%H:%M")
        eb = bars.get(exit_t)
        idx_exit_vs_trig = (eb[3] - trig) * sgn if eb else None
        ext = None
        if eb:
            ext = (eb[1] - trig) if up else (trig - eb[2])                     # best index excursion beyond trigger
        mny = [((x["strike"] - (idx_cross or trig)) * sgn) for _, x in legs]
        rows.append(dict(
            day=label, variant=var, time=t, side=side, trigger=trig, legs=len(legs), tp=tp, pnl=round(pnl, 2),
            win=pnl > 0, heldS=round(held), mfe=round(st.mean(x["maxFavourablePts"] for _, x in legs), 2),
            mae=round(st.mean(x["maxAdversePts"] for _, x in legs), 2),
            # entry-time features
            withDayTrend=None if fut_day is None else fut_day > 0, futVsOpen=fut_day,
            withVwap=None if vw is None else (vw * sgn) > 0, futVsVwap=vw,
            momIn3m=None if mom is None else round(mom, 1),
            newSessionExtreme=None if hi is None else ((trig >= hi) if up else (trig <= lo)),
            rangeSoFar=None if hi is None else round(hi - lo, 1),
            retest=retest, touches=L and L["touches"], source=L and "+".join(L["sources"]),
            relVol=ff.get("relVolumeVsPrev10"), overshoot=None if idx_cross is None else round((idx_cross - trig) * sgn, 1),
            spread=round(st.mean(e["spreadPts"] for e, _ in legs), 2), moneyness=round(st.mean(mny)),
            # exit-time facts
            idxExitVsTrigger=None if idx_exit_vs_trig is None else round(idx_exit_vs_trig, 1),
            idxBestBeyond=None if ext is None else round(ext, 1)))
    return rows


def split(rows, name, f, days):
    def fmt(g):
        return "%3d ev %3d win %8.1f" % (len(g), sum(r["win"] for r in g), sum(r["pnl"] for r in g))
    yes = [r for r in rows if f(r) is True]
    no = [r for r in rows if f(r) is False]
    per_day = "  ".join("%s %+.0f/%+.0f" % (d, sum(r["pnl"] for r in yes if r["day"] == d),
                                            sum(r["pnl"] for r in no if r["day"] == d)) for d in days)
    print("%-34s YES %s | NO %s | per day YES/NO: %s" % (name, fmt(yes), fmt(no), per_day))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--csv")
    a = ap.parse_args()
    rows, days = [], []
    for item in a.inputs:
        label, path = item.split("=", 1)
        days.append(label)
        rows += load(label, path)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    for var in ("FIRST_TOUCH", "CONFIRMED_CLOSE", None):
        R = [r for r in rows if var is None or r["variant"] == var]
        print("\n=== %s: %d events, %d trades, pnl %.1f ===" % (var or "BOTH", len(R), sum(r["legs"] for r in R),
                                                             sum(r["pnl"] for r in R)))
        split(R, "with day trend (fut vs open)", lambda r: r["withDayTrend"], days)
        split(R, "with futures VWAP", lambda r: r["withVwap"], days)
        split(R, "3-min momentum into level > 0", lambda r: None if r["momIn3m"] is None else r["momIn3m"] > 0, days)
        split(R, "trigger = new session extreme", lambda r: r["newSessionExtreme"], days)
        split(R, "first crossing of this level", lambda r: r["retest"] == 0, days)
        split(R, "level touches >= 3", lambda r: None if r["touches"] is None else r["touches"] >= 3, days)
        split(R, "relVol >= 1", lambda r: None if r["relVol"] is None else r["relVol"] >= 1, days)
        split(R, "overshoot at entry <= 5 pts", lambda r: None if r["overshoot"] is None else r["overshoot"] <= 5, days)
        split(R, "range so far >= 300 pts", lambda r: None if r["rangeSoFar"] is None else r["rangeSoFar"] >= 300, days)
        split(R, "CE", lambda r: r["side"] == "CE", days)
        split(R, "entry before 12:00", lambda r: r["time"] < "12:00", days)
    # why: exit-time facts
    print("\n=== WHY (all events) ===")
    for lab, g in (("WIN events", [r for r in rows if r["win"]]), ("LOSS events", [r for r in rows if not r["win"]])):
        ie = [r["idxExitVsTrigger"] for r in g if r["idxExitVsTrigger"] is not None]
        be = [r["idxBestBeyond"] for r in g if r["idxBestBeyond"] is not None]
        print("%s: %d | index at exit beyond trigger: %d of %d (median %+.1f pts) | best index excursion beyond "
              "trigger in exit minute median %.1f | median hold %ss | MFE %.2f MAE %.2f" % (
                  lab, len(g), sum(x > 0 for x in ie), len(ie), st.median(ie) if ie else 0, st.median(be) if be else 0,
                  st.median(r["heldS"] for r in g), st.mean(r["mfe"] for r in g), st.mean(r["mae"] for r in g)))
    loss = [r for r in rows if not r["win"] and r["idxExitVsTrigger"] is not None]
    rev = [r for r in loss if r["idxExitVsTrigger"] <= 0]
    held = [r for r in loss if r["idxExitVsTrigger"] > 0]
    print("LOSS split: index fell back through trigger %d (pnl %.1f) | index still beyond trigger %d (pnl %.1f)" % (
        len(rev), sum(r["pnl"] for r in rev), len(held), sum(r["pnl"] for r in held)))
    mon = defaultdict(list)
    for r in rows:
        mon["ITM" if r["moneyness"] < -50 else "OTM" if r["moneyness"] > 50 else "ATM"].append(r["pnl"])
    print("moneyness (event avg):", {k: (len(v), round(sum(v), 1)) for k, v in mon.items()})


if __name__ == "__main__":
    main()
