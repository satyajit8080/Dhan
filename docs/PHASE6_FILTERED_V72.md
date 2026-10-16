# FILTERED paper variant — v7.2 fixes and definitions

Paper only. No order code, no rule change (RULES.md untouched). FIRST_TOUCH and CONFIRMED_CLOSE behaviour is
unchanged (differential check: 300 randomised runs, 67,899 events, 0 differences; old vs new everyday bundle on the
end-to-end scenario: identical paper events and identical HTTP calls).

## Defects in v7.1 and fixes
| # | v7.1 defect (verified) | v7.2 fix |
|---|---|---|
| 1 | Touch count taken from `derive_levels`' nearest resistance/support, while the trigger comes from `refresh_table.pick_level` (nearest level with ≥ 2 touches, VWAP-only excluded). On 13 Oct PM 22 and 16 Oct 33 scan-sides the two differed; the count was then missing and the entry was skipped. | `scan.selected_levels()` repeats the refresh table's choice with the same function, inputs, buffer and rounding (`refresh_table._round_to`); `run_refresh` now passes those values explicitly (equal to the defaults; a test pins it). A trigger is matched only to the level that produced it; otherwise MISSING (no entry). |
| 2 | Day trend used the previous scan's futures LTP with no age limit (last-trade age at fetch p50 39–52 s, p90 92–310 s). | Age = crossing time − futures **last-trade time** (`last_trade_time`, not the fetch time). > 120 s → STALE; missing → MISSING; non-numeric or > 5 s in the future → INVALID. All fail closed. |
| 3 | Overshoot < 20 pts never rejected anything in FILTERED (0 of 51 crossings: the entry is the crossing poll). | Removed from the FILTERED decision; logged as a diagnostic. |
| 4 | Filter failures logged as one combined `paper_skip`; per-filter results and missing data not distinguishable. | One `filter_check` event per crossing with every filter evaluated independently. |

## Definitions (thresholds unchanged, not tuned from results)
- **dayTrend:** listed-futures LTP vs its day open (`ohlc.open` of the futures quote), from the latest scan. CE needs LTP > open, PE needs LTP < open; equal = FAIL.
- **levelTouches:** touches of the level `refresh_table.pick_level` selected for this trigger. ≥ 3 = FAIL.
- **sessionRange:** high − low of today's 1-min SENSEX index bars up to the latest scan. < 300 = FAIL; ≥ 300 = PASS.
- **strike ITM:** `(SENSEX LTP at the crossing poll − strike)` for CE, reversed for PE. > 150 = that strike is not entered; 150 exactly is allowed.

## Choices to review
- **120 s futures-age limit** (two scan intervals). On the 4 logged sessions, about 14 of 51 crossings (27 %) would have been STALE (age at crossing p50 ≈ 72 s, p90 ≈ 306 s). SENSEX futures trade thinly; a lower limit rejects more.
- **SENSEX vs its 09:15 open** (diagnostic only, `diagnostics.spotVsOpen0915`). It uses the polled index (≤ 5 s old) and has no futures liquidity issue, but it measures the cash index instead of the traded futures, and it is MISSING if the 09:15 bar is absent. Logged on every crossing so the two definitions can be compared; it does not change the decision.
- **CONFIRMED_CLOSE overshoot** (`confirm_max_overshoot_pts`, default off). Measured at the entry poll after the confirming minute. On the logged sessions, entries ≥ 20 pts beyond the trigger did not do worse (25 trades, −3.3 pts) than < 20 (100 trades, −318.2), so there is no evidence to enable it.

## Forward test
Each filter can be evaluated alone from `filter_check` events and the matching FIRST_TOUCH trades (same crossing,
same fills). Fix the thresholds before the test; count crossing events, not trades. Four in-sample sessions are not
evidence of an edge.
