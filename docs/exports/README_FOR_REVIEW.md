# SENSEX breakout paper-trade data, 12–23 Oct 2026 (for external review)

Paper trading only (no real orders). Data: Dhan API, read-only. File: `breakouts_2026-10-12_to_23.csv`,
one row per breakout event per version. No account details are included.

## Setup
- Entry: SENSEX index crosses a trigger = support/resistance level ± 5 pts (rounded to 5). CE on a break above
  resistance, PE on a break below support. Buy 5 strikes (ATM ±2 of the last scan) at ASK; exits on BID.
- Exit: +6 option-premium pts target, −11 stop, 10-min time stop; prices polled every ~5 s (stops slip to −12…−16).
- No-trade windows: before 09:25, 11:30–13:00, after 14:50. Lot 20; break-even win rate 64.7 %.
- Versions: FIRST_TOUCH = every crossing; CONFIRMED_CLOSE = enter after a 1-min close beyond the trigger;
  FILTERED = day trend + touches < 3 + range ≥ 300 + strike ≤ 150 ITM; FRESH_LEVEL = touches < 3 only (live from 23 Oct).
- Days: 12, 13 (AM+PM runs), 16, 19 (partial), 21, 22, 23 Oct. 14–15 Oct logs missing.

## Columns
| Column | Meaning |
|---|---|
| day, time, variant, side, trigger | when, which version, CE/PE, trigger price |
| touches, fresh, source | earlier touches of the level (fresh = < 3), how the level was found |
| legs, tp, pnl, result | strikes traded, how many hit target, summed premium pts, WIN if pnl > 0 |
| heldS, mfe, mae | median seconds held; avg best / worst premium move (pts) |
| withDayTrend, futVsOpen | futures vs its day open, in trade direction (> 0 = with trend) |
| withVwap, futVsVwap | futures vs its VWAP, in trade direction |
| momIn3m | index move in the 3 min before the break, in trade direction (pts) |
| newSessionExtreme | trigger at a new day high (CE) / low (PE) |
| rangeSoFar | day high − low before the break (pts) |
| relVol | futures 1-min volume vs previous 10-min average |
| overshoot | index beyond trigger at entry (pts) |
| moneyness | avg strike distance from index in trade direction (negative = ITM) |
| spread | avg bid-ask spread at entry (pts) |
| idxExitVsTrigger, idxBestBeyond | index vs trigger at exit; best index move beyond trigger in the exit minute |

## Totals (FIRST_TOUCH, all breakouts)
- 102 breakouts, 51 won, −397 pts. Fresh levels (touches < 3): 30 breakouts, 67.3 % of trades won, +361 pts.
  Old levels (≥ 3 touches): −759 pts.
- Most losses are false breakouts: index goes ~7 pts past the trigger, then reverses.

## Question
Which entry filters, exit rules or strike choices would cut false breakouts without overfitting a sample this
small? Please name what you would forward-test, and how many trades are needed before trusting a result.
