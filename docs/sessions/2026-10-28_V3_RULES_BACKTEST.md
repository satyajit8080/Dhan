# Scalping rules v3 — 1-year back-test on real option premiums (28 Oct 2026)

Tool: `tools/backtest_v3_rules.py` (local files only, no orders). Data: SENSEX 1-min index 27 Oct 2025 – 26 Oct 2026
(247 days) + real expired nearest-weekly ATM CALL/PUT 1-min premiums (Dhan rolling option). Halves split 28 Apr 2026.

Rules tested (as written in the bull50-dhan v3 description):
- Levels: today's 5-min candles, >= 2 touches, trigger = level ± 5 rounded to 5 (same replica as earlier tests).
- Entry: a 1-min close beyond the trigger, the next 1-min candle also closes beyond it; enter at that close, ATM option.
- Filters: no-trade windows; theta filter approximated as "skip expiry day" (52 days); spread/IV pass at ATM.
- Exit: stop = 1-min close back through the level; target = 1.5 × stop distance (1.0× and 2.0× also run); 10-min time stop.
- Risk: stop for the day after 3 losing trades. Costs: slippage per trade and ₹70 per trade, lot 20.
- **Not tested:** the volume filter (index file has no volume; rolling option files have empty volume arrays) and
  "target = next level".

| Target | Slippage | Trades | Win % | Avg win / loss (pts) | Net 1 lot H1 / H2 |
|---|---|---|---|---|---|
| 1.0× | 0 | 1,298 | 58.4 | +13.8 / −21.2 | −₹55,159 / −₹56,064 |
| **1.5× (v3)** | 0 | 1,133 | 50.8 | +18.8 / −20.9 | −₹56,769 / −₹39,025 |
| **1.5× (v3)** | 1.5 | 1,093 | 48.6 | +18.1 / −21.7 | −₹73,302 / −₹54,241 |
| 1.5× (v3) | 3.0 | 1,073 | 47.2 | +17.2 / −22.5 | −₹86,448 / −₹69,450 |
| 2.0× | 0 | 1,067 | 47.0 | +22.1 / −20.7 | −₹55,198 / −₹31,875 |

Why it loses: the stop is a 1-min close back through the level, so the real premium loss is often larger than the
planned risk (avg loss ≈ −21 pts against avg win ≈ +19), and ATM weekly premiums lose time value during the hold.
Only the 2.0×, zero-slippage second half is positive in points (+317) and still negative after charges.

Conclusion: v3 as written has no edge on a year of real option prices, in either half, at any target tested.
No real orders; no strategy change. RULES.md unchanged.
