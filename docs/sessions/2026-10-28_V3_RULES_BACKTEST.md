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

## Variant search (28 Oct; `tools/backtest_v3_variants.py`)
2,731 v3 signals (all levels, expiry days kept and flagged). Grid: ATM option (real premium, ₹70 + 1.5 pts) or SENSEX
future (index pts, ₹400 + 2 pts) × 4–5 stops (close-back, fixed) × 6–8 targets (k × risk, fixed) × hold 5/10/20/30 min ×
16 filters (side, fresh/old, morning/afternoon, with/against the move since the open, first signal of the day,
expiry day, day before expiry, new extreme, combinations). Selection on the first half only, check on the second.

- **4,096 combinations: 0 positive after costs in the first half, 0 in the second half, 0 in both.**
- Best first-half pick (first signal of the day, option, close-back stop, +10 target): 68 % wins, −₹12,952 H1, −₹16,487 H2.
- Raw follow-through (average index move after entry, no costs): first half −6.0 pts at 10 min / −11.6 at 20 min
  (breakouts reversed); second half +2.9 / +4.9 (small continuation). The direction is not stable between halves, so
  neither following nor fading the breakout is reliable, and both are far below the ~4–5 pt option or ~22 pt futures
  round-trip cost.

Conclusion: no variant of v3 tested here makes money after costs in either half. Not a basis for real orders.
