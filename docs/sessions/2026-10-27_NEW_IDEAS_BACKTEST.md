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
