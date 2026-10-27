# SENSEX options breakout study — full brief for review (27 Oct 2026)

Paper trading only (no real orders). Data from Dhan API (read-only). Lot 20; ₹ = premium points × 20.

## Files
1. `backtest_breakouts_1year.csv` — 1-year index-only back-test (27 Oct 2025–26 Oct 2026, 247 days, 5,125 breakouts):
   day, time, side, trigger, touches, fresh (touches<3), extreme (trigger at new day high/low), result (TP/SL/TIME),
   pts (option premium per strike, approximated with delta 0.5: +6 = +12 index pts, −11 = −22).
2. `live_breakouts_2026-10-12_to_27.csv` — every live paper breakout (326 rows, real option bid/ask): variant, time,
   side, trigger, touches, fresh, legs (5 strikes), pnl (sum of 5 strikes), result, day trend, VWAP, 3-min momentum,
   new extreme, range so far, relative volume, overshoot, moneyness, spread, index at exit vs trigger.
3. Raw 1-min SENSEX index CSV (1 year) — the user has it (Sensex_1min_1year.csv).

## Rules
Entry: index crosses a support/resistance trigger (level ± 5, rounded to 5); CE above resistance, PE below support;
buy 5 strikes (ATM ±2) at ASK. Exit on BID: +6 premium target, −11 stop, 10-min time stop (5-s polling; real stops
slip to −12…−16). No trades before 09:25, 11:30–13:00, after 14:50. Break-even win rate 64.7 % (≈71 % with slippage).

## Versions
FIRST_TOUCH (every crossing), CONFIRMED_CLOSE (after a 1-min close beyond), FILTERED (trend+touches+range+ITM),
FRESH_LEVEL (touches<3), FRESH_EXIT (FRESH_LEVEL + exit when a 1-min close goes back through the trigger),
FRESH_SL8 (stop −8), FRESH_EXTREME (fresh + new day high/low), FRESH_CALM (FRESH_EXTREME + not Thursday + index moved
≤15 pts into the break in the previous 3 minutes; added 27 Oct, no live trades yet).

## 1-year back-test (index-only)
- All breakouts −1,502; fresh −591; old (≥3 touches) −910; FRESH_EXTREME 1,051 trades, 61.5 % win, −579
  (−2,199 with 4-pt stop slippage). Only 4/13 months positive. Expiry Thursday: 31 of 46 days lost.
- Losers are false breakouts: median 20 index pts beyond the trigger (winners 62); 38 % never reached +12.
  Entry features of losers ≈ winners.
- Exit tests (early abort, close-back exit, wider stop) and new ideas (opening-range breakout, fading failed breaks):
  none positive in both halves after slippage.
- FRESH_CALM (Thursday skipped, 3-min move ≤15): 234 trades, 70.1 % win (H1 69.7 %, H2 70.4 %), +214 pts before
  slippage, −66 with slippage; avg +0.9 pts/trade (≈₹18/lot) < charges ≈ ₹70/trade. Aug 2026 worst (36 % win).

## Live paper (12–27 Oct; 14, 15, 20 Oct partly/not logged)
FIRST_TOUCH ≈ −541 pts, CONFIRMED_CLOSE ≈ −891, fresh levels ≈ +605 (partly rebuilt), FRESH_EXIT +270 (2 days),
FRESH_EXTREME +230 (1 live day) / +474 rebuilt (24 breakouts, 67 % win).

## 27 Oct losing trades
- 10:29 CE 73,470 (2 touches): index went 3 pts beyond, back below in 2 min (stop), later +47; mid-range (day high
  73,686), 44-pt run-up into the break. −35.4 pts (5 strikes).
- 14:11 CE 73,315 (2 touches): 9 pts beyond, then −124 in 5 min; mid-range (day high 73,763) on a choppy day.
  −72.9 pts. FRESH_EXIT cut it to about 0 for 3 strikes (close-back exit). FRESH_EXTREME/CALM skipped both.

## Current decision
No real money. Continue paper with FRESH_CALM (entry) and FRESH_EXIT (exit), FRESH_LEVEL as baseline; stop following
FIRST_TOUCH, CONFIRMED_CLOSE, FILTERED, FRESH_SL8.

## Questions
1. Do you agree with this decision? Anything in the data that contradicts it?
2. Using the 1-year CSV + raw 1-min data, test FRESH_CALM combined with the close-back exit on BOTH halves, with
   4-pt stop slippage and ₹70/trade charges. Is any version positive after costs?
3. Any other robust idea (positive in both halves after costs) worth a paper test? If none, say so.
