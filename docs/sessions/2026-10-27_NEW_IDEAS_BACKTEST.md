# New ideas on the 1-year SENSEX 1-min data (27 Oct 2026; `tools/backtest_new_ideas.py`)

Index-only; option P&L = 0.5 × index move; NO theta, IV or charges. Split: first half | second half (premium pts/strike).

| Idea | Rule | slip 0 | slip 2 | slip 4 |
|---|---|---|---|---|
| A. Opening range (09:16–09:30) breakout, 1/day, 241 trades | +15 / −10, 30 min | +40 / +175 | −102 / +45 | −244 / −85 |
| | +20 / −10, 30 min | +50 / +210 | −108 / +64 | −266 / −82 |
| B. Fade a failed FRESH_EXTREME breakout, 774 trades | +15 / −10, 30 min | +270 / +90 | −204 / −336 | −678 / −762 |
| | +20 / −10, 30 min | +400 / +500 | −124 / +52 | −648 / −396 |
| | +6 / −11, 10 min | −327 / −367 | | −987 / −963 |

Only wide-target versions are positive in both halves, and only with ≤ 1 pt stop slippage. A 30-minute hold also
loses theta (live ATM theta ≈ −95 pts/day, roughly −7 pts per 30 min), which the test ignores, and the target/stop
pairs were chosen from 4 tried. Conclusion: no robust edge; nothing to take live.

## Grok's failed-breakout fade (27 Oct): non-extreme levels with ≥ 3 touches, no Thursday, 1-min close back through
the trigger within 5 min while the break extended < 10 pts; fade it; stop 5 pts beyond the failed extreme; 10-min hold.
790 signals. Negative even before slippage/charges in both halves (best TP +8: −145 / −136 pts; after 4-pt slippage and
₹70/trade: −₹43,878 / −₹48,965 for 1 lot). Rejected.

## Grok's combined suggestions (27 Oct; `tools/backtest_combined_rules.py`)
6 entry sets (calm ≤10/12/15, + index-vs-open trend, + time window, + opening-30-min range < 0.7 %) × 6 exits
(+6/−11, +4/−8, close-back, abort if < 8–10 pts in 2–3 min) × slippage 2/4; 1 strike, 1 lot, ₹70/trade.
0 of 72 combinations positive after costs in both halves. Best: calm ≤10 + close-back exit, slip 4: +67 / +42 pts
(positive in points) but −₹5,665 / −₹5,245 after charges — about +0.6 pts (₹12) per trade vs ₹70 charges.
