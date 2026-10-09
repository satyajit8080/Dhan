#!/usr/bin/env python3
"""
Per-session observation report (Phase 6), from decoded Cloud events.

  python3 tools/summarize_observation.py observation/2026-10-12/events.jsonl \
      [--replay-output observation/2026-10-12/replay.txt] > observation/2026-10-12/REPORT.md

Three separate sections, never merged into one verdict:
  1. Data retrieval correctness   (endpoints, retries, completeness, freshness)
  2. Calculation correctness      (replay parity vs TypeScript, gate, IV coverage)
  3. Strategy-rule effectiveness  (what the existing rules did; CE/PE = NOT_CONFIGURED)
Facts only. Nothing here judges profitability or proposes thresholds.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict


def _q(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]


def _fmt(v, dp=1):
    return "—" if v is None else (("%." + str(dp) + "f") % v if isinstance(v, float) else str(v))


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def summarize(events, replay_text=None) -> str:
    scans = [e for e in events if e.get("event") == "scan"]
    runtime = next((e for e in events if e.get("event") == "runtime"), {})
    summ = [e for e in events if e.get("event") == "session_summary"]
    retries = [e for e in events if e.get("event") == "request_retry"]
    failures = [e for e in events if e.get("event") == "request_failed"]
    out = []
    w = out.append
    day = scans[0]["startedIst"][:10] if scans else "—"
    w("# Observation report — %s" % day)
    w("")
    w("Program %s %s · mode %s · Python %s · scans %d (%s → %s) · orders placed: **0** (no order code)."
      % (runtime.get("program", "?"), runtime.get("version", "?"), runtime.get("mode", "?"), runtime.get("python", "?"),
         len(scans), scans[0]["startedIst"][11:19] if scans else "—", scans[-1]["startedIst"][11:19] if scans else "—"))
    if summ:
        w("Stop reason: `%s`." % summ[-1].get("stopReason"))
    st = Counter(s["status"] for s in scans)
    w("Statuses: " + ", ".join("%s %d" % kv for kv in sorted(st.items())))
    w("")

    # ------------------------------------------------------------ 1. data
    w("## 1. Data retrieval correctness")
    w("")
    per = defaultdict(lambda: {"n": 0, "ok": 0, "retried": 0, "ms": [], "env": Counter(), "err": Counter()})
    for s in scans:
        for c in (s.get("observation") or {}).get("endpoints", []):
            d = per[c["endpoint"]]
            d["n"] += 1
            d["ok"] += bool(c.get("ok"))
            d["retried"] += (c.get("attempts") or 1) > 1
            d["ms"].append(c.get("durationMs"))
            d["env"][c.get("envelope") or "—"] += 1
            if not c.get("ok"):
                d["err"]["%s %s" % (c.get("error"), c.get("code") or "")] += 1
    w("| Endpoint | Calls | OK | Retried | p50 ms | max ms | Envelope | Errors |")
    w("|---|---|---|---|---|---|---|---|")
    for ep, d in sorted(per.items()):
        w("| `%s` | %d | %d | %d | %s | %s | %s | %s |" % (
            ep, d["n"], d["ok"], d["retried"], _fmt(_q(d["ms"], .5)), _fmt(_q(d["ms"], 1)),
            ", ".join("%s %d" % kv for kv in d["env"].items()), ", ".join("%s ×%d" % kv for kv in d["err"].items()) or "—"))
    w("")
    w("Retry events: %d · terminal request failures: %d." % (len(retries), len(failures)))
    obs = [s.get("observation") or {} for s in scans]
    cc = [o["chainCompleteness"] for o in obs if o.get("chainCompleteness")]
    if cc:
        w("Chain completeness: strikes min %d / median %s / max %d; legs two-sided min %d; legs without OI max %d; "
          "without previous OI max %d." % (
              min(c["strikes"] for c in cc), _fmt(statistics.median(c["strikes"] for c in cc), 0), max(c["strikes"] for c in cc),
              min(c["legsTwoSided"] for c in cc), max(c["legsWithoutOi"] for c in cc), max(c["legsWithoutPreviousOi"] for c in cc)))
    fr = [o.get("freshness") or {} for o in obs]
    w("Freshness: chain↔futures skew p50 %s / max %s ms; futures last-trade age max %s s; last candle age p50 %s / max %s s." % (
        _fmt(_q([f.get("chainFuturesSkewMs") for f in fr], .5), 0), _fmt(_q([f.get("chainFuturesSkewMs") for f in fr], 1), 0),
        _fmt(_q([f.get("futuresLastTradeAgeS") for f in fr], 1)), _fmt(_q([f.get("lastCandleAgeS") for f in fr], .5)),
        _fmt(_q([f.get("lastCandleAgeS") for f in fr], 1))))
    wc = Counter(x.split(":")[0] for s in scans for x in s.get("warnings", []))
    w("Warnings: " + (", ".join("%s ×%d" % kv for kv in wc.most_common()) or "none"))
    contracts = Counter("%s exp %s" % (s["futures"]["securityId"], s["futures"]["expiry"]) for s in scans if s.get("futures"))
    expiries = Counter(s.get("expiry") for s in scans if s.get("expiry"))
    w("Option expiry used: %s · futures contract: %s." % (dict(expiries) or "—", dict(contracts) or "—"))
    bad = [s for s in scans if s["status"] in ("ERROR", "INVALID_DATA", "AUTH_FAILED", "PLAN_MISSING", "CONFIG_BLOCKED")]
    for s in bad[:15]:
        w("- %s `%s` %s" % (s["startedIst"][11:19], s["status"], (s.get("reason") or "")[:200]))
    w("")

    # ------------------------------------------------------------ 2. calc
    w("## 2. Calculation correctness")
    w("")
    if replay_text:
        res = Counter(l.split()[1] for l in replay_text.splitlines() if l.startswith("scan-") and len(l.split()) > 1)
        w("Replay of recorded scans through Python and TypeScript on identical data: %s." %
          (", ".join("%s %d" % kv for kv in sorted(res.items())) or "no result lines found"))
    else:
        w("Replay parity: **not run** for this session (run the two replay commands in PHASE6_OBSERVATION_PLAN.md §4).")
    gates = [o["gate"] for o in obs if o.get("gate")]
    blocked = [g for g in gates if g.get("blocked")]
    w("Gate: %d evaluated, %d blocked." % (len(gates), len(blocked)))
    for r, n in Counter(r.split(" by ")[0][:90] for g in blocked for r in g.get("reasons", [])).most_common(8):
        w("- blocked ×%d: %s" % (n, r))
    ok = [g for g in gates if not g.get("blocked")]
    if ok:
        dv = [g.get("divergenceVsFuture") for g in ok]
        sp = [g.get("perStrikeSpread") for g in ok]
        w("Futures − parity forward: p50 %s / min %s / max %s pts. Per-strike forward spread p50 %s / max %s pts (limit 40)." % (
            _fmt(_q(dv, .5), 2), _fmt(_q(dv, 0), 2), _fmt(_q(dv, 1), 2), _fmt(_q(sp, .5), 2), _fmt(_q(sp, 1), 2)))
        wf = Counter(f["code"] for g in ok for f in g.get("findings", []) if f.get("severity") == "warn")
        w("Gate warnings: " + (", ".join("%s ×%d" % kv for kv in wf.items()) or "none"))
    legs = [l for o in obs for l in o.get("legs", [])]
    if legs:
        no_iv = sum(1 for l in legs if l.get("ivPct") is None)
        w("Legs logged (±5 strikes around ATM): %d; without computable IV: %d (excluded from the table, PHASE4 P2 unchanged)." % (len(legs), no_iv))
        vd = [l["ivPct"] - l["vendorIvPct"] for l in legs if l.get("ivPct") is not None and l.get("vendorIvPct") is not None]
        if vd:
            w("Our IV − vendor IV (diagnostic only; vendor IV is never used): p50 %s / p95 |Δ| %s IV pts." % (
                _fmt(_q(vd, .5), 2), _fmt(_q([abs(x) for x in vd], .95), 2)))
    exc = Counter(x.split(" excluded: ")[1] for o in obs for x in o.get("skipped", []) if " excluded: " in x)
    if exc:
        w("Excluded table legs: " + ", ".join("%s ×%d" % kv for kv in exc.items()))
    w("")

    # ------------------------------------------------------------ 3. rules
    w("## 3. Strategy-rule effectiveness (existing rules only)")
    w("")
    w("CE/PE direction: **NOT_CONFIGURED** in every scan (RULES.md §6, D1–D8). No signal was produced or evaluated.")
    rows = Counter(r["status"] for o in obs for r in o.get("refreshRows", []))
    w("Refresh-row statuses: " + (", ".join("%s %d" % kv for kv in sorted(rows.items())) or "none (blank tables)"))
    sess = Counter(s.get("session") for s in scans)
    w("Scans by session window: " + ", ".join("%s %d" % kv for kv in sess.items()))
    # level crossings: first time the index reached each published level
    timeline = [(s["startedIst"][11:19], o.get("prices") or {}, o.get("levels") or {}, o)
                for s, o in zip(scans, obs) if o.get("prices")]
    first = {}
    for t, p, lv, _ in timeline:
        for k in ("breakoutAbove", "breakdownBelow"):
            v = lv.get(k)
            if v is not None and (k, v) not in first:
                first[(k, v)] = {"published": t, "hit": None, "indexAtHit": None}
    for (k, v), info in first.items():
        for t, p, _, _ in timeline:
            if t < info["published"] or p.get("indexLtp") is None:
                continue
            if (k == "breakoutAbove" and p["indexLtp"] >= v) or (k == "breakdownBelow" and p["indexLtp"] <= v):
                info.update(hit=t, indexAtHit=p["indexLtp"])
                break
    if first:
        w("")
        w("| Level | Value | First published | First reached by index (scan time) |")
        w("|---|---|---|---|")
        for (k, v), info in sorted(first.items(), key=lambda kv: kv[1]["published"]):
            w("| %s | %s | %s | %s |" % (k, v, info["published"], info["hit"] or "not reached"))
    # projected premium at trigger vs premium observed on the first scan at/after the crossing
    proj = []
    for i, (t, p, lv, o) in enumerate(timeline):
        for r in o.get("refreshRows", []):
            if r.get("status") != "OK" or r.get("trigger") is None or r.get("projectedPremium") is None:
                continue
            side_up = r["side"] == "CE"
            for t2, p2, _, o2 in timeline[i + 1:]:
                ix = p2.get("indexLtp")
                if ix is None or (side_up and ix < r["trigger"]) or (not side_up and ix > r["trigger"]):
                    continue
                leg = next((l for l in o2.get("legs", []) if l["strike"] == r["strike"] and l["type"] == r["side"]), None)
                if leg and leg.get("ask"):
                    proj.append((t, r["strike"], r["side"], r["trigger"], r["projectedPremium"], t2, ix, leg["ask"]))
                break
    if proj:
        seen, uniq = set(), []
        for x in proj:
            if (x[1], x[2], x[3]) not in seen:
                seen.add((x[1], x[2], x[3]))
                uniq.append(x)
        w("")
        w("Projected premium at trigger vs ask observed on the first scan after the index reached the trigger "
          "(an index scan can overshoot the trigger, so this is indicative, not a test):")
        w("")
        w("| Row published | Strike | Side | Trigger | Projected | Observed at | Index then | Ask then |")
        w("|---|---|---|---|---|---|---|---|")
        for x in uniq[:25]:
            w("| %s | %s | %s | %s | %s | %s | %s | %s |" % x)
    w("")
    w("_Generated by tools/summarize_observation.py from Cloud logs. Facts only; no thresholds proposed._")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("events")
    ap.add_argument("--replay-output")
    a = ap.parse_args(argv)
    replay = open(a.replay_output, encoding="utf-8").read() if a.replay_output else None
    sys.stdout.write(summarize(load(a.events), replay))
    return 0


if __name__ == "__main__":
    sys.exit(main())
