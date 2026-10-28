#!/usr/bin/env python3
"""Midday SENSEX momentum (move vs previous close at T, follow to 15:25) on 3.4 years of SENSEX minute data
built from the spot field of rolling ATM option files plus the 1-year index CSV. Local files only; no orders.
Usage: python3 tools/test_midday_momentum_3y.py HISTORY_DIR [HISTORY_DIR ...] sensex_1min.csv"""
import json, glob, datetime, csv, sys
from collections import defaultdict
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
sx = defaultdict(dict)
for d in sys.argv[1:-1]:
    for f in glob.glob(d + "/OPT_1_ATM_CALL_*.json"):
        p = json.load(open(f))["payload"].get("ce") or {}
        for t, s in zip(p.get("timestamp") or [], p.get("spot") or []):
            dt = datetime.datetime.fromtimestamp(t, IST); sx[dt.date().isoformat()][dt.strftime("%H:%M")] = s
for r in csv.DictReader(open(sys.argv[-1])):                 # the 1-year index file (Oct 2025 - Oct 2026)
    dt = datetime.datetime.fromtimestamp(int(r["Epoch"]), IST); sx[dt.date().isoformat()][dt.strftime("%H:%M")] = float(r["Close"])
def at(m, hm):
    ks = [k for k in m if k <= hm]; return m[max(ks)] if ks else None
days = sorted(d for d in sx if len(sx[d]) > 300)
print("days %d from %s to %s" % (len(days), days[0], days[-1]))
periods = [("2023-24", "0000", "2024-10-27"), ("2024-25", "2024-10-27", "2025-10-27"), ("2025-26 (stock test year)", "2025-10-27", "9999")]
for T in ("12:00", "12:30", "13:30"):
    for X in (0.3, 0.6):
        line = "SENSEX move vs prev close > %.1f%% at %s -> follow to 15:25 |" % (X, T)
        for name, a, b in periods:
            v = []
            for i, d in enumerate(days[1:], 1):
                if not (a <= d < b):
                    continue
                pc = at(sx[days[i - 1]], "15:30"); s0 = at(sx[d], T); s1 = at(sx[d], "15:25")
                if not pc or not s0 or not s1:
                    continue
                ch = 100 * (s0 - pc) / pc
                if abs(ch) > X:
                    v.append((s1 - s0) * (1 if ch > 0 else -1))
            if v:
                line += " %s: n %d win %.0f%% avg %+.1f pts |" % (name, len(v), 100 * sum(x > 0 for x in v) / len(v), sum(v) / len(v))
        print(line)
