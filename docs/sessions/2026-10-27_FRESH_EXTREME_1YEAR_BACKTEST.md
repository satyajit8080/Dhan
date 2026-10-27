# FRESH_EXTREME — 1-year index-only back-test (27 Oct 2026)

Data: user-supplied CSV `Sensex_1min_1year.csv`, SENSEX index 1-min bars 2025-10-27 → 2026-10-26, 247 days × 375 bars.
Validation: all 3,097 bars overlapping the Dhan 1-min index bars in our Cloud logs (12–26 Oct) match exactly (close
diff 0.00). Tool: `tools/backtest_fresh_extreme.py` (levels.derive_levels on 5-min candles from the 1-min bars,
scan.selected_levels, ±5 triggers, no-trade windows, once-per-session triggers; 09:15 bar dropped as Dhan starts 09:16).

Approximations (important):
- Option P&L from the index with delta 0.5 (+6 premium = +12 index, −11 = −22); no IV / theta / spread; nominal
  stop (live stops slip to about −15). Stop assumed first when a bar hits both.
- Level replica agrees with the live scanner's selected trigger/touches on 72.7 % of 3,970 live scan-sides
  (differences: Dhan 5-min candles and VWAP inputs are not in the CSV).

| Rule (FRESH_EXTREME, 1,051 breakouts) | Win % | Premium pts / strike | 1st half | 2nd half |
|---|---|---|---|---|
| +6 / −11 (current) | 61.5 (break-even 64.7) | **−579** | −811 | +232 |
| +6 / −8 | 56.6 | −78 | −420 | +342 |
| +10 / −11 | 53.8 | +303 | −526 | +829 |
| +15 / −11 | 46.1 | +1,040 | −347 | +1,387 |

All breakouts −1,501.5; fresh −591; old −910.5 (+6/−11). FRESH_EXTREME by month (+6/−11): positive only in
May, Jun, Sep, Oct 2026; 115 positive vs 124 negative days.

Conclusion: on a year of data the current rule does not show an edge; the 12–26 Oct result looks like a favourable
period. Larger targets are positive only in the second half and before slippage. Not a basis for real orders.
