#!/usr/bin/env python3
"""Same midday-momentum rule traded with the SENSEX ATM option (real rolling premiums, 5-pt round-trip cost) and
with a future (22-pt cost), per year; expiry days optionally skipped. Local files only; no orders.
Usage: python3 tools/test_midday_momentum_3y_options.py HISTORY_DIR [HISTORY_DIR ...]"""
import json, glob, datetime, sys
from collections import defaultdict
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
leg = defaultdict(dict); sx = defaultdict(dict)
for d in sys.argv[1:]:
    for side in ("CALL", "PUT"):
        for f in glob.glob(d + "/OPT_1_ATM_%s_*.json" % side):
            p = json.load(open(f))["payload"].get("ce" if side == "CALL" else "pe") or {}
            for i, t in enumerate(p.get("timestamp") or []):
                dt = datetime.datetime.fromtimestamp(t, IST); day = dt.date().isoformat(); hm = dt.strftime("%H:%M")
                leg[(day, side)][hm] = (p["strike"][i], p["close"][i])
                if side == "CALL":
                    sx[day][hm] = p["spot"][i]
def at(m, hm):
    ks = [k for k in m if k <= hm]; return m[max(ks)] if ks else None
days = sorted(d for d in sx if len(sx[d]) > 300)
# expiry days (roll rule on the straddle): next day's opening straddle > 2x this day's last-hour straddle
def strad(d, hm):
    c, p = at(leg[(d, "CALL")], hm), at(leg[(d, "PUT")], hm)
    return c[1] + p[1] if c and p else None
exp = {a for a, b in zip(days, days[1:]) if strad(a, "15:00") and strad(b, "09:20") and strad(b, "09:20") > 2 * strad(a, "15:00")}
periods = [("2023-24", "0000", "2024-10-27"), ("2024-25", "2024-10-27", "2025-10-27"), ("2025-26", "2025-10-27", "9999")]
for T, X in (("12:30", 0.6), ("12:30", 0.3), ("13:30", 0.6), ("12:00", 0.6)):
    for skip_exp in (False, True):
        line = "%s > %.1f%%%s |" % (T, X, " (no expiry day)" if skip_exp else "")
        for name, a, b in periods:
            opt, fut = [], []
            for i, d in enumerate(days[1:], 1):
                if not (a <= d < b) or (skip_exp and d in exp):
                    continue
                pc = at(sx[days[i - 1]], "15:30"); s0 = at(sx[d], T); s1 = at(sx[d], "15:25")
                if not pc or not s0 or not s1:
                    continue
                ch = 100 * (s0 - pc) / pc
                if abs(ch) <= X:
                    continue
                side, sg = ("CALL", 1) if ch > 0 else ("PUT", -1)
                e = at(leg[(d, side)], T); x = at(leg[(d, side)], "15:25")
                if not e or not x:
                    continue
                exitp = x[1] + (0.0 if x[0] == e[0] else 0.5 * (x[0] - e[0]) * sg)   # strike roll: delta-0.5 bridge
                opt.append(exitp - e[1] - 5.0)                                         # 5 pts round-trip cost
                fut.append((s1 - s0) * sg - 22.0)                                      # futures cost 22 pts
            if opt:
                line += " %s: n %d option %+.1f pts (win %.0f%%), future %+.1f pts |" % (
                    name, len(opt), sum(opt) / len(opt), 100 * sum(o > 0 for o in opt) / len(opt), sum(fut) / len(fut))
        print(line)
print("Rs per lot (20): multiply pts by 20")
