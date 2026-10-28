#!/usr/bin/env python3
"""Backtest of github.com/MrSpideyNihal/agents-trade signal_engine.py (confirmed day-high/low momentum break, buy option).
Real expired nearest-weekly ATM premiums (rolling, strike rolls bridged with delta 0.5). Local files only; no orders.

  python3 tools/backtest_agents_trade.py INDEX_CSV OPTION_HISTORY_DIR {sensex|nifty}

Their rule on 1-min bars: the bar makes a new day high (low), the index is >= CHG_MIN (0.4 %) above (below) the previous
day's close, and momentum is positive (close above the close 2 minutes earlier ~ their 4 x 30 s window) -> buy the ATM
CE (PE). Their hard rules: max 2 trades a day, one position at a time, 4-min re-alert cooldown. Exits by risk profile
(option premium %): low +30/-20, medium +45/-25 (default), high +80/-35 (high also uses 2 strikes OTM: not tested, ATM
only); flat at 15:15. Entry at the signal minute's option close (lag 0) or the next minute's (lag 1). Stop is checked
before target on a bar that hits both. Costs: NIFTY 1.5 pts (lot 65), SENSEX 5 pts (lot 20).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_fresh_extreme as b                          # noqa: E402
import backtest_real_options as ro                          # noqa: E402

CFG = {"sensex": dict(cost=5.0, lot=20), "nifty": dict(cost=1.5, lot=65)}
PROFILES = {"low": (0.30, 0.20), "medium": (0.45, 0.25), "high": (0.80, 0.35)}


def run(days, opt, tp, sl, lag, chg_min, max_trades, cost):
    ds = sorted(days); out = []
    for di in range(1, len(ds)):
        d = ds[di]; bars = days[d]; prev = days[ds[di - 1]][-1]["close"]
        hi = bars[0]["high"]; lo = bars[0]["low"]; n = 0; free = 0
        for j in range(1, len(bars) - 2):
            x = bars[j]; hm = x["t"].strftime("%H:%M")
            nh, nl = x["high"] > hi, x["low"] < lo
            hi = max(hi, x["high"]); lo = min(lo, x["low"])
            if j < free or n >= max_trades or hm < "09:16" or hm > "15:00":
                continue
            chg = 100 * (x["close"] - prev) / prev; mom = x["close"] - bars[max(0, j - 2)]["close"]
            side = "CE" if nh and chg >= chg_min and mom > 0 else ("PE" if nl and chg <= -chg_min and mom < 0 else None)
            if not side:
                continue
            leg = opt.get((d, side)) or {}; j0 = j + lag; t0 = bars[j0]["timestampMs"] // 1000
            if t0 not in leg:
                continue
            K = leg[t0][0]; e = leg[t0][4]; cs = 1 if side == "CE" else -1; res = None; k = j0
            for k in range(j0 + 1, len(bars)):
                t = bars[k]["timestampMs"] // 1000
                if t not in leg:
                    continue
                k2, o, h, l, c = leg[t]; sh = 0.0 if k2 == K else 0.5 * (k2 - K) * cs
                if l + sh <= e * (1 - sl):
                    res = -sl * e; break
                if h + sh >= e * (1 + tp):
                    res = tp * e; break
                if bars[k]["t"].strftime("%H:%M") >= "15:15":
                    res = c + sh - e; break
            if res is None:
                continue
            out.append((d, res - cost, res)); n += 1; free = k + 4
    return out


def main():
    days = b.load(sys.argv[1]); opt = ro.load_options(sys.argv[2], 1); cfg = CFG[sys.argv[3]]
    ds = sorted(days); half = ds[len(ds) // 2]
    print("%s: days %d, halves split at %s, cost %.1f pts, lot %d" % (sys.argv[3].upper(), len(ds), half, cfg["cost"], cfg["lot"]))
    for chg in (0.4, 0.2, 0.6):
        for mx in (2, 99):
            for prof, (tp, sl) in PROFILES.items():
                for lag in (0, 1):
                    o = run(days, opt, tp, sl, lag, chg, mx, cfg["cost"])
                    if not o:
                        continue
                    h = [[p for d, p, g in o if (d >= half) == s] for s in (0, 1)]
                    print("  move>=%.1f%% max %-2s/day %-6s (+%d%%/-%d%%) lag %d | n %4d | win %4.1f%% (gross %4.1f%%) | "
                          "Rs/lot H1 %+8.0f / H2 %+8.0f / total %+8.0f"
                          % (chg, mx if mx < 99 else "all", prof, tp * 100, sl * 100, lag, len(o),
                             100 * sum(p > 0 for _, p, _ in o) / len(o), 100 * sum(g > 0 for _, _, g in o) / len(o),
                             sum(h[0]) * cfg["lot"], sum(h[1]) * cfg["lot"], sum(p for _, p, _ in o) * cfg["lot"]))


if __name__ == "__main__":
    main()
