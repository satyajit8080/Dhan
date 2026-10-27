# Real option premium back-test (expired SENSEX weekly options), 27 Oct 2026

**Data:** Dhan Cloud history run `execution_20261027_1947`, `/charts/rollingoption`, 1-min, 27 Oct 2025 – 26 Oct 2026.
- `history_done`: 1,460 windows, 371,428 bars, 0 refused days, orders NONE.
- 247 trading days, each with ATM CALL and ATM PUT for expiry numbers 1 and 2.
- Five-day windows were refused (DH-905); the one-day retry fetched every day.
- **Expiry number 1 is the nearest weekly.** On expiry Thursdays it closes at ₹0.05–0.10 (e.g. 27 Nov 2025, 8 Jan 2026, 14 May 2026). The option `spot` matches the 1-min index file exactly.

**Method:** `tools/backtest_real_options.py`, which reads local files only and places no orders.
- **Signals:** taken from the same 1-min index replay as the earlier tests: FRESH_EXTREME, FRESH_CALM (not Thursday, 3-min move ≤ 15) and BREAK_RETEST.
- **Instrument:** nearest-weekly ATM CALL for CE breakouts and ATM PUT for PE breakouts, at the strike that is ATM in the entry minute.
- **Exits:** +6 / −11 premium with a 10-min time stop. If one bar reaches both, the stop counts.
- **Strike changes:** when the rolling ATM moves to another strike, the held strike's price is estimated with delta 0.5. This affects about 12–20% of trades.
- **Entry prices tested:** "cross" estimates the moment the trigger is crossed; "close" uses the close of the entry minute.
- **Halves:** H1 is before 28 Apr 2026 and H2 is from 28 Apr 2026.
- **Net figures:** 1 lot (20) minus ₹70 charges per trade.

| Rule | Trades | Win % | Pts (no slip) | Pts (slip 4) | Net 1 lot (slip 4) H1 / H2 |
|---|---|---|---|---|---|
| FRESH_EXTREME | 1,051 | 49.0 | −2,812 | −4,956 | −₹99,015 / −₹73,670 |
| FRESH_CALM | 234 | 54.7 | −398 | −822 | −₹15,470 / −₹17,350 |
| BREAK_RETEST | 274 | 56.2 | −396 | −876 | −₹18,670 / −₹18,030 |

("cross" entry. With "close" entry the results are within a few points of these, still negative in both halves.)

**Checks:**
- The median 1-min ATM option range is 13.3 premium points, against 22 for the index, which is consistent with delta ≈ 0.5. So bad wicks are not the cause.
- Exiting on 1-min closes only, FRESH_CALM still wins 50.9% for −251 pts.

**Comparison with the index-only (delta 0.5) model:** FRESH_CALM had a 70.1% win rate and +214 pts there. With real premiums it falls to 54.7% and −398 pts.
- The delta model was too optimistic.
- Real ATM weekly premiums lose time value and IV during the 10-minute hold, and swing within the minute. A move that reached +12 index points often did not reach +6 premium before −11.

**Conclusion:** on real expired-option prices, none of the three rules makes money in either half, even before slippage and charges. This confirms the earlier decision: no real money. Paper tracking can continue for observation only. No strategy change is recommended from this test.

Per-trade rows (cross entry, slip 4): `docs/exports/real_options_trades_1year.csv`.
