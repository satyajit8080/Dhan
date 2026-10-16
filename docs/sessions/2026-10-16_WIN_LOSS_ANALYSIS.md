# Win / loss analysis of all PAPER trades, 12–16 Oct 2026 (4 logged sessions)

Source: decoded Cloud logs 12 Oct, 13 Oct 09:32–10:20, 13 Oct 11:05–15:30, 16 Oct. Tool: `tools/analyze_all_trades.py`.
Exit rule under study: +6 / −11 premium points, 10-min time stop (user, 9 Oct). Zero orders; paper only.
**All findings are in-sample (76 events, 4 sessions). Nothing here changes RULES.md; any filter must be tested forward.**

## Totals
- 380 paper trades, 76 crossing events (FIRST_TOUCH 51 / CONFIRMED_CLOSE 25). TP 178 / SL 202 = 46.8 % (break-even 64.7 %).
- Total **−823.2 premium pts** (FIRST_TOUCH −501.8 on 255 trades; CONFIRMED_CLOSE −321.5 on 125).

## Why we won (32 events)
- The index **kept going**: in 29 of 32 the index was still beyond the trigger at exit (median +25 pts); best move beyond
  the trigger in the exit minute median **29 pts**. Target hit fast (median 31 s), little heat (MAE −2.6).

## Why we lost (44 events)
1. **False breakout — 31 of 44 (−1,750.8 pts in those events).** The index went only ~7.6 pts past the trigger (median)
   and came back through it. The level held; the breakout failed.
2. **Late entry, then a normal pullback — 13 of 44 (−690.0).** The index stayed beyond the trigger but premium fell 11+.
   Mostly CONFIRMED_CLOSE entries made 20–30 index pts after the trigger (chasing); a pullback hit the stop.
3. **Stop slippage:** 202 stops cost **395.3 pts beyond −11** (median 1.4, p90 4.6 per stop), because exits are checked
   every 5 s. Even with exact −11 stops the total would be about −428: slippage is not the main cause.

## What separated wins from losses (entry-time facts; per-day sign in brackets)
| Factor | YES | NO | Consistent across days? |
|---|---|---|---|
| **Against the day trend** (futures below its open for CE / above for PE) | 21 ev, **4 wins, −679.8** | 55 ev, 28 wins, −143.5 | YES side negative every day it occurred (12 Oct −11, 13 PM −533, 16 Oct −136) |
| Level touched ≥ 3 times before | 54 ev, −904.7 | 17 ev, +112.5 | mostly (12 Oct both negative) |
| Session range so far < 300 pts (narrow / sideways) | 45 ev, −774.8 | 31 ev, −48.5 | mostly (13 AM opposite) |
| Futures 1-min volume ≥ previous-10 average | 24 ev, −636.3 | 52 ev, −187.0 | 3 of 4 days |
| Strike ITM by > 150 pts (per trade) | 124 tr, −565.7, 37 % win | OTM > 150: 79 tr, +28.9, 61 % win | ITM>150 negative 3 of 4 days |
| CE vs PE | CE 42 ev, +90.8 | PE 34 ev, −914.0 | follows the day's direction (12 Oct down day: PE still lost) |

Combinations (in-sample, found after looking — overfitting risk):
- With day trend **and** relVol < 1: 37 ev, 23 wins, **+428.1** (12 Oct −206, 13 AM +94, 13 PM +510, 16 Oct +29).
- With day trend **and** touches < 3: 18 ev, 11 wins, +232.9 (12 Oct −51).
- No filter or exit pair tested was positive on every day.

## Candidate improvements (for the user to decide; paper variants only, forward-tested)
1. Skip crossings **against the day trend** (futures vs its own open) — the clearest, simplest separator.
2. Skip levels already touched **≥ 3 times** and quiet/narrow sessions (range so far < 300 pts).
3. Prefer **ATM/OTM** strikes over deep ITM (> 150 pts ITM).
4. Do not enter if the index is already **≥ 20 pts past** the trigger (late entry).
5. Count the stop slippage in expectations: realised stop ≈ −12.4 with 5-s checks.
Thresholds above (3 touches, 300 pts, 150 pts, 20 pts) are the analysis cut points, not decided rules (D1–D9 open).
