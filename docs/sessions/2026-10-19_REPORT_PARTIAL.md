# Observation report — 2026-10-19

Program sensex-readonly-observer 7.2 · mode OBSERVE · Python 3.11.15 · scans 126 (09:45:41 → 11:50:42) · orders placed: **0** (no order code).
Statuses: OK 126

## 1. Data retrieval correctness

| Endpoint | Calls | OK | Retried | p50 ms | max ms | Envelope | Errors |
|---|---|---|---|---|---|---|---|
| `/charts/intraday` | 378 | 378 | 0 | 41.9 | 75.0 | bare 378 | — |
| `/marketfeed/quote` | 126 | 126 | 0 | 28.9 | 45.0 | wrapped 126 | — |
| `/optionchain` | 126 | 126 | 0 | 3027.1 | 3067.4 | wrapped 126 | — |
| `/optionchain/expirylist` | 126 | 126 | 0 | 29.2 | 92.7 | wrapped 126 | — |

Retry events: 0 · terminal request failures: 0.
Chain completeness: strikes min 190 / median 190 / max 190; legs two-sided min 357; legs without OI max 0; without previous OI max 0.
Freshness: chain↔futures skew p50 33 / max 50 ms; futures last-trade age max 487.6 s; last candle age p50 165.2 / max 285.8 s.
Warnings: none
Option expiry used: {'2026-10-22': 126} · futures contract: {'864571 exp 2026-10-29': 126}.

## 2. Calculation correctness

Replay of recorded scans through Python and TypeScript on identical data: PASS 9.
Gate: 126 evaluated, 0 blocked.
Futures − parity forward: p50 111.73 / min 5.20 / max 206.81 pts. Per-strike forward spread p50 4.53 / max 20.12 pts (limit 40).
Gate warnings: INDEX_CARRY ×53
Legs logged (±5 strikes around ATM): 2772; without computable IV: 0 (excluded from the table, PHASE4 P2 unchanged).
Our IV − vendor IV (diagnostic only; vendor IV is never used): p50 1.44 / p95 |Δ| 3.88 IV pts.

## 3. Strategy-rule effectiveness (existing rules only)

CE/PE direction: **NOT_CONFIGURED** in every scan (RULES.md §6, D1–D8). No signal was produced or evaluated.
Refresh-row statuses: NO_TRADE_WINDOW 210, OK 1050
Scans by session window: MORNING 105, MIDDAY (no-trade window 11:30-13:00) 21

| Level | Value | First published | First reached by index (scan time) |
|---|---|---|---|
| breakoutAbove | 74790.0 | 09:45:41 | not reached |
| breakdownBelow | 74605.0 | 09:45:41 | 09:55:41 |
| breakdownBelow | 74600.0 | 09:46:41 | 09:55:41 |
| breakoutAbove | 74615.0 | 09:55:41 | not reached |
| breakdownBelow | 74170.0 | 09:55:41 | not reached |
| breakoutAbove | 74610.0 | 09:58:41 | not reached |
| breakoutAbove | 74605.0 | 10:00:41 | not reached |
| breakoutAbove | 74540.0 | 10:01:41 | 10:23:41 |
| breakoutAbove | 74545.0 | 10:02:41 | 10:23:41 |
| breakoutAbove | 74550.0 | 10:03:41 | 10:30:42 |
| breakdownBelow | 74390.0 | 10:05:41 | 10:39:42 |
| breakoutAbove | 74535.0 | 10:13:41 | 10:23:41 |
| breakdownBelow | 74525.0 | 10:23:41 | 10:25:41 |
| breakoutAbove | 74810.0 | 10:29:42 | not reached |
| breakoutAbove | 74530.0 | 10:41:42 | not reached |
| breakoutAbove | 74400.0 | 10:45:42 | 10:52:42 |
| breakdownBelow | 74295.0 | 10:50:42 | 11:35:42 |
| breakoutAbove | 74525.0 | 10:52:42 | not reached |
| breakoutAbove | 74520.0 | 10:55:42 | not reached |
| breakoutAbove | 74515.0 | 10:57:42 | not reached |
| breakoutAbove | 74510.0 | 11:13:42 | not reached |
| breakdownBelow | 74285.0 | 11:36:42 | 11:37:42 |

Projected premium at trigger vs ask observed on the first scan after the index reached the trigger (an index scan can overshoot the trigger, so this is indicative, not a test):

| Row published | Strike | Side | Trigger | Projected | Observed at | Index then | Ask then |
|---|---|---|---|---|---|---|---|
| 10:05:41 | 74200 | PE | 74390 | 363.48 | 10:39:42 | 74383.8 | 341.45 |
| 10:05:41 | 74300 | PE | 74390 | 407.16 | 10:39:42 | 74383.8 | 385 |
| 10:05:41 | 74400 | PE | 74390 | 456.93 | 10:39:42 | 74383.8 | 432.85 |
| 10:05:41 | 74500 | PE | 74390 | 506.83 | 10:39:42 | 74383.8 | 483.1 |
| 10:05:41 | 74600 | PE | 74390 | 565.5 | 10:39:42 | 74383.8 | 541.6 |
| 10:20:41 | 74700 | PE | 74390 | 618.77 | 10:39:42 | 74383.8 | 602.4 |
| 10:31:42 | 74800 | PE | 74390 | 668.13 | 10:39:42 | 74383.8 | 666.15 |
| 10:45:42 | 74100 | CE | 74400 | 559.96 | 10:52:42 | 74407.09 | 559.4 |
| 10:45:42 | 74200 | CE | 74400 | 501.19 | 10:52:42 | 74407.09 | 499.3 |
| 10:45:42 | 74300 | CE | 74400 | 446.16 | 10:52:42 | 74407.09 | 443.95 |
| 10:45:42 | 74400 | CE | 74400 | 395.18 | 10:52:42 | 74407.09 | 393.55 |
| 10:45:42 | 74500 | CE | 74400 | 348.76 | 10:52:42 | 74407.09 | 347.15 |
| 10:50:42 | 74100 | PE | 74295 | 360.4 | 11:35:42 | 74285.48 | 344.75 |
| 10:50:42 | 74200 | PE | 74295 | 405.99 | 11:35:42 | 74285.48 | 391.15 |
| 10:50:42 | 74300 | PE | 74295 | 452.32 | 11:35:42 | 74285.48 | 439.85 |
| 10:50:42 | 74400 | PE | 74295 | 505.38 | 11:35:42 | 74285.48 | 493.75 |
| 10:50:42 | 74500 | PE | 74295 | 563.15 | 11:35:42 | 74285.48 | 552.65 |
| 10:52:42 | 74100 | PE | 74390 | 311.24 | 11:29:42 | 74388.81 | 309 |
| 11:28:42 | 74600 | PE | 74295 | 610.49 | 11:35:42 | 74285.48 | 614.35 |

Futures 1-min volume vs previous-10 average: p50 0.06 / p90 3.85 / max 52.63; scans with volume data 126/126. Futures LTP − session VWAP: p50 -8.8 / min -255.8 / max 239.7 pts.
1-min index closes beyond the breakout trigger: 6 scans (with the previous bar also beyond: 5); below the breakdown trigger: 2 (previous also: 2).

### PAPER trades [CONFIRMED_CLOSE] — take profit +6 / stop loss −11 premium points, time stop 10 min (no orders)

Trades 10: STOP_LOSS 5, TAKE_PROFIT 5. TP vs SL win rate 50.0% (break-even for 6/11 is 64.7%, before charges). Total -37.35 pts, average -3.74 pts per trade (entry at ask, exit at bid: spread already included).
Entry spread p50 1.10 / max 1.75 pts. Median time to TP 60 s, to SL 25 s.
Crossing events (each opens one paper trade per strike): 2 — mostly TP 1, mostly SL 1, mixed 0.
Realised stop average -14.52 (worst -22.00) vs nominal −11; realised take-profit average 7.05 vs nominal +6 (discrete polling).

| Id | Side | Strike | Trigger | Entry ask | Exit bid | P&L pts | Outcome | Held s | Best | Worst |
|---|---|---|---|---|---|---|---|---|---|---|
| C003 | PE | 74400 | 74390.0 | 497.1 | 484.15 | -12.95 | STOP_LOSS | 10.1 | 0.0 | -12.95 |
| C005 | PE | 74600 | 74390.0 | 612.25 | 590.25 | -22.0 | STOP_LOSS | 15.1 | 0.0 | -22.0 |
| C001 | PE | 74200 | 74390.0 | 390.25 | 378.75 | -11.5 | STOP_LOSS | 25.2 | 0.25 | -11.5 |
| C002 | PE | 74300 | 74390.0 | 437.85 | 425.3 | -12.55 | STOP_LOSS | 25.2 | 0.0 | -12.55 |
| C004 | PE | 74500 | 74390.0 | 545.6 | 532 | -13.6 | STOP_LOSS | 30.2 | 0.15 | -13.6 |
| C006 | CE | 74100 | 74400.0 | 558.75 | 565.05 | 6.3 | TAKE_PROFIT | 25.2 | 6.3 | -4.1 |
| C008 | CE | 74300 | 74400.0 | 441.75 | 447.95 | 6.2 | TAKE_PROFIT | 25.2 | 6.2 | -0.95 |
| C007 | CE | 74200 | 74400.0 | 499.2 | 508.05 | 8.85 | TAKE_PROFIT | 60.0 | 8.85 | -3.65 |
| C009 | CE | 74400 | 74400.0 | 392.45 | 399.95 | 7.5 | TAKE_PROFIT | 60.0 | 7.5 | -2.35 |
| C010 | CE | 74500 | 74400.0 | 345.7 | 352.1 | 6.4 | TAKE_PROFIT | 60.0 | 6.4 | -2.0 |

### PAPER trades [FIRST_TOUCH] — take profit +6 / stop loss −11 premium points, time stop 10 min (no orders)

Trades 10: STOP_LOSS 6, TAKE_PROFIT 4. TP vs SL win rate 40.0% (break-even for 6/11 is 64.7%, before charges). Total -31.55 pts, average -3.16 pts per trade (entry at ask, exit at bid: spread already included).
Entry spread p50 1.10 / max 1.70 pts. Median time to TP 55 s, to SL 20 s.
Crossing events (each opens one paper trade per strike): 2 — mostly TP 0, mostly SL 2, mixed 0.
Realised stop average -12.60 (worst -14.05) vs nominal −11; realised take-profit average 11.01 vs nominal +6 (discrete polling).

| Id | Side | Strike | Trigger | Entry ask | Exit bid | P&L pts | Outcome | Held s | Best | Worst |
|---|---|---|---|---|---|---|---|---|---|---|
| P002 | PE | 74300 | 74390.0 | 394.15 | 382.9 | -11.25 | STOP_LOSS | 19.7 | 0.0 | -11.25 |
| P004 | PE | 74500 | 74390.0 | 494.95 | 482.1 | -12.85 | STOP_LOSS | 19.7 | 0.0 | -12.85 |
| P005 | PE | 74600 | 74390.0 | 552.05 | 538 | -14.05 | STOP_LOSS | 19.7 | 0.0 | -14.05 |
| P001 | PE | 74200 | 74390.0 | 349.4 | 364 | 14.6 | TAKE_PROFIT | 54.9 | 14.6 | -10.0 |
| P003 | PE | 74400 | 74390.0 | 440.55 | 456.1 | 15.55 | TAKE_PROFIT | 54.9 | 15.55 | -9.25 |
| P006 | CE | 74100 | 74400.0 | 562.7 | 569.55 | 6.85 | TAKE_PROFIT | 5.0 | 6.85 | 0.0 |
| P007 | CE | 74200 | 74400.0 | 501.8 | 508.85 | 7.05 | TAKE_PROFIT | 5.0 | 7.05 | 0.0 |
| P008 | CE | 74300 | 74400.0 | 448 | 434.5 | -13.5 | STOP_LOSS | 109.9 | 4.5 | -13.5 |
| P009 | CE | 74400 | 74400.0 | 396.55 | 384.95 | -11.6 | STOP_LOSS | 130.1 | 4.55 | -11.6 |
| P010 | CE | 74500 | 74400.0 | 349 | 336.65 | -12.35 | STOP_LOSS | 135.1 | 4.6 | -12.35 |

### FILTERED diagnostics — one `filter_check` per crossing (experiment; not a rule)

Crossings checked 2: ENTER 0, REJECT 2. Results per filter (MISSING / INVALID / STALE = input data not usable, not a filter failure):

| Filter | PASS | FAIL | MISSING | INVALID | STALE |
|---|---|---|---|---|---|
| dayTrend | 1 | 0 | 0 | 0 | 1 |
| levelTouches | 0 | 2 | 0 | 0 | 0 |
| sessionRange | 2 | 0 | 0 | 0 | 0 |
| strike ITM (per strike) | 7 | 3 | — | — | — |

Diagnostic only — SENSEX vs its 09:15 open: PASS 0, FAIL 0, MISSING 2. Overshoot at the crossing p50 2.08 / max 3.36 pts.
Futures last-trade age at the crossing p50 87.9 / max 224.9 s.

_Generated by tools/summarize_observation.py from Cloud logs. Facts only; no thresholds proposed._
