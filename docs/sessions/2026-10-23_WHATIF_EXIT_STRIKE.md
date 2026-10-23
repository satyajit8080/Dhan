# What-if: early exit and nearest strike (offline, recorded paper trades 12–23 Oct)

Tool: `tools/whatif_exit_strike.py`. FIRST_TOUCH legs; "fresh" = selected level < 3 touches. No look-ahead: a 1-min
bar is assumed to close 60 s after its label; the exit uses the last recorded bid at or before that time.
Limits: price paths end at the recorded exit, so larger targets cannot be tested; "nearest strike" can only pick
from the 5 recorded strikes (ATM±2 of the last scan).

| Fresh levels (150 trades) | Win % | Points | Per trade |
|---|---|---|---|
| A as recorded (+6/−11) | 67.3 | +361.4 | +2.41 |
| B + exit at 1-min close back through trigger | 64.0 | +432.6 | +2.88 |
| C nearest strike only (30 trades) | 66.7 | +59.6 | +1.99 |
| D B + C (30 trades) | 66.7 | +93.9 | +3.13 |

B per fresh day vs A: 12 Oct −4.4, 13 AM +17.8, 13 PM +43.4, 21 Oct 0, 22 Oct −51.1, 23 Oct +65.4.
All levels: A −0.78 / B −0.48 / C −0.78 / D −0.26 per trade. In-sample; not a rule; RULES.md unchanged.
