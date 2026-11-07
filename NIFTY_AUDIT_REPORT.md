# NIFTY Quant Engine: audit report

Scope: NIFTY only (index, front-month futures, options). Read-only research and paper trading; no order code exists.
The code changes are in `dhan_cloud_single/main.py` (Dhan Cloud), `main.py` (VPS engine) and `tests/`.

**Limitation of this report.** The audit could not be run on real NIFTY data from this session, because there is no
Dhan token here. The data-dependent numbers come from running `dhan_cloud_single/main.py` with
`RUN_AUDIT_ON_START = True`, which prints PHASE 1–10 in the Dhan Cloud log. Mathematics, engine correctness and
regression tests are verified offline below; the actual performance figures are pending that run.

---

## 1. Repository map: data to signal to ledger

| Layer | Dhan Cloud (`dhan_cloud_single/main.py`) | VPS (`main.py`) |
|---|---|---|
| Auth / REST | `post()`: an allow-list of 5 market-data endpoints with per-endpoint rate spacing | `DhanRest` (SDK data mixins), `assert_read_only` |
| Instruments | `all_futures()` / `nifty_futures()` from the instrument master | `InstrumentMaster` |
| Bars | `intraday()` (89-day chunks, de-duplicated), `stitch_front_month()`, `complete_5m()` | WebSocket `MarketFeed`, `MinuteBars`, `candles_from_payload` |
| Indicators | `compute()` (VWAP, ATR, Supertrend, ADX/DI, RSI, Bollinger, ER, Z, pivots) | `compute_indicators()`; parity is tested to 1e-7 |
| Options | `greeks76`, `implied_vol`, `option_forward`, `chain_analytics` | `black76_*`, `build_features` |
| Signals | `signal_at()`, `build_card()` (filters F3+F4) | `tech_signal_at`, `tech_snapshot` |
| Backtest | `backtest()` (execution scenarios, roll-adjusted series) | `backtest_tech`, `walk_forward_backtest` |
| Statistics | `summarize`, `bootstrap`, `mc_drawdown`, `deflated_sharpe`, `permutation_random_entries`, `bh_adjust`, `cusum_change_point`, `hurst` | `pattern_study`, `model_validation` |
| Paper ledger | `ledger_build()`: a deterministic replay from `LEDGER_START` | none (signals table) |
| UI / alerts | Telegram cards, daily summary | `index.html`, terminal dashboard, Telegram |

## 2. Mathematical verification (all tests pass)

| Item | Method | Result |
|---|---|---|
| VWAP (typical price, session reset), TR, Wilder ATR/RSI/ADX, Supertrend, Bollinger bandwidth, ER, Z, pivots | Closed-form hand calculations plus parity between both engines | Exact (≤1e-7) |
| Black-76 price | Hull reference 10.4506 | Matches |
| Delta, gamma, vega, theta, rho, **vanna, charm (new)** | Central finite differences, 12-point grid | Max relative error 2.7e-6 |
| Implied vol | Price → IV → price round trip | Error <1e-6 |
| Put-call parity | Residual `C − P − DF(F − K)` | 0 |
| Forward for options | `F_opt = F_fut · e^(−r(T_fut − T_opt))` (futures used as the forward, as specified) | Implemented |
| IV rank / percentile | `iv_rank`, `iv_rank_percentile` | Tested; they need IV history, which is VPS only |
| Hurst, variance ratio, autocorrelation, CUSUM change point | Random walk gives H ≈ 0.5 | Passes |
| Student-t p-value | t = 2.0, df = 30 → 0.0546 | Passes |
| MACD, Donchian, EMA | Not used by any rule, so not added (no proven incremental value) | — |

## 3. Backtest integrity: issues found and fixed

| # | Issue | Location | Financial impact | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | **STT understated.** The engine used 0.02% on futures and 0.10% on options; Budget 2026 raised these to **0.05% / 0.15% from 01-Apr-2026** | `CHARGES` (cloud), `CHARGES` and `option_costs_bps` (VPS), `dhan_cloud/rules.py` | One NIFTY futures round trip costs about **₹929 instead of about ₹440** at 24,000; every reported P&L was overstated by about ₹490 per trade | Charges by date (`charges(..., trade_date)`) | `test_charges_2026_vs_2025`, self-test |
| 2 | **Futures traded against INDEX pivots.** R1/R2/S1/S2/PDH/PDL came from the index daily H/L/C, but entries were futures prices (50–200 pt basis) | `backtest()` / `backtest_tech()` level filter | The "skip within 0.2·ATR of a level" rule fired at the wrong prices | `prior_session_levels()` from the traded series (`levels="series"`) | `test_series_levels_parity` |
| 3 | **Stop filled at the stop price after a gap through it** | `backtest()` exit logic | Losses understated on gap bars | `gap=True`: fill at the bar open minus slippage (`SL_GAP`) | `test_gap_through_stop_fills_at_open` |
| 4 | **No slippage** | Entries, stops, time exits | Optimistic | Scenarios: optimistic / legacy / realistic (1 pt) / stress (3 pt plus a 0.5 pt target trade-through) | `test_both_touched_never_favourable_in_realistic`, self-test ordering |
| 5 | **Roll jump in the stitched futures series** distorted ATR, Supertrend and Bollinger around expiries | `stitch_front_month()` | False signals near rolls | Additive back-adjustment (`adj`) for indicators; charges use raw prices | Self-test "Roll back-adjustment", "Back-adjustment identical signals" |
| 6 | **A forming or gappy 1m bucket could enter the live 5m strategy** | Live `closed()` / `resample()` | Signals on incomplete candles | `complete_5m()` requires the bucket's last minute to exist and be closed | `test_complete_candle_guard_drops_gappy_bucket` |
| 7 | **Paper ledger lost on restart; Telegram duplicates possible** | Old `paper_enter/paper_update` (in memory) | No auditable track record | Deterministic replay ledger with sha1 trade ids, lifecycle states, and no re-sends after a restart | `test_paper_ledger_survives_restart_without_duplicates` |
| 8 | Both stop and target touched in one bar | Engine | Already pessimistic (stop first); now explicit per scenario | `both="sl"` except in the optimistic scenario | ✓ |
| 9 | Look-ahead / leakage | Engine | None found: signal on the closed bar i, fill at the open of i+1, levels from the previous session | — | Self-test "No look-ahead" |
| 10 | Old NIFTY results were the index proxy with futures lot and charges, and **VWAP was undefined** there (index has no volume) | Earlier audit runs | Only squeeze trades were possible; the trend and mean-reversion rules were never tested out of sample | Labelled clearly as INDEX proxy vs FUT in every report line | — |

**Reproduction.** `EXEC["legacy"]` with `levels="index_daily"` and `charge_year=2025` reproduces the old engine exactly:
identical trades and identical net on 12 test series. PHASE 3a of the audit log checks the reported ₹25,595 /
₹54,773 / ₹43,748 / ₹41,518 against the real data and prints MATCH or DIFFERENT. PHASE 3b then applies fixes 1, 2,
5 and 4 one at a time so the P&L impact of each fix is visible.

## 4. F3 + F4 investigation (produced in the Dhan Cloud log)

For BASELINE, F3, F4 and F3+F4, on OOS May–Aug (index), IS Aug–Nov (index) and IS Aug–Nov (futures), the audit prints:
- n, win rate with Wilson 95% CI, net, expectancy, average win and loss, PF, max drawdown, longest losing run
- daily Sharpe and Sortino (annualised), t-statistic and p-value
- bootstrap 95% CI of total P&L and P(total ≤ 0)
- Monte Carlo max drawdown (median, p95, p99)
- Deflated Sharpe with **26 variants tried** on this data
- a random-entry permutation test (same trade count, window, direction mix, exits and costs)
- Benjamini-Hochberg adjusted OOS p-values

**Filter attribution.** For each filter: the stats of the trades it removes, and a random-removal permutation test.
Covered: F3 vs base, F4 vs base, F4 given F3, F3 given F4, and the OOS-only case.

**Regime stability**, by:
- realized-vol tercile
- month
- direction × setup
- weekday (Tuesday = NIFTY weekly expiry; holiday shifts not modelled)
- opening gap
- ADX bucket
- exit type

**Execution.** All four scenarios, plus a slippage sweep of 0 / 0.5 / 1 / 2 / 3 / 5 pts. It reports cost per trade in rupees and points, and the break-even slippage.

**Automatic verdict.** All five gates must pass:
1. OOS net > 0
2. OOS bootstrap P(total ≤ 0) < 0.05
3. Permutation p < 0.05
4. At least 100 OOS trades
5. Stress-scenario net > 0

If any gate fails, the log prints **"EDGE NOT ESTABLISHED – paper only"**.

Expected direction, stated before the run: the earlier F3+F4 index result of +₹41,518 came from 64 trades. Fix 1 alone
removes roughly 64 × ₹490 ≈ ₹31,000. Fewer than 100 OOS trades already fails gate 4 by construction. **Expect "EDGE
NOT ESTABLISHED"** until much more paper and real futures history exists.

## 5. Advanced intelligence: status

| Item | Status |
|---|---|
| ATM IV (own, on the futures forward) vs vendor IV, 2-strike skew, parity residual, straddle, max pain, PCR (volume and OI), OI change top strikes, dealer-GEX sign-convention estimate, gamma concentration and peak, vanna, charm | **Live** on every signal card |
| IV percentile / rank | Function present; needs stored IV history (VPS `vol_metrics`) |
| Realized vol, GARCH, HMM, Hurst, autocorrelation, variance ratio, CUSUM change point | VPS engine plus a cloud audit PHASE 2b |
| Volume profile, order-flow imbalance | VPS has WebSocket depth; Dhan historical bars carry no tick data → not back-testable, so not used as a rule |
| ML: logistic walk-forward | In the audit (AUC, Brier vs naive, precision, recall, expectancy of selected trades). Research only, never traded |
| Kronos / XGBoost / RandomForest | Not runnable in Dhan Cloud (no PyTorch or sklearn; 1 vCPU). VPS only, same chronological split, after the logistic baseline |

## 6. Paper ledger

- **Rebuild:** a replay of the frozen rules (`LIVE_FILTER`, `EXEC_LIVE="realistic"`) over Dhan data from `LEDGER_START`, rebuilt every 5 minutes and after every restart.
- **Trade ids:** `sha1(NIFTY | entry ts | setup | side)[:12]`. The same data gives the same ledger, with no duplicates.
- **States and logging:** OPEN → CLOSED(SL / SL_GAP / TARGET / EOD), with full execution lines (prices, charges, net) in the log and on Telegram.
- **Restarts:** events older than the process start are not re-sent.
- **Daily summary** at 15:25: today, since start, max drawdown.

## 7. Dashboard

- **Cloud:** Telegram card per signal (indicators, levels, options analytics, Greeks incl. vanna and charm, strike, spread check, charges) and the daily paper summary.
- **VPS:** `index.html` and `/api/signal` (existing).
- **Not done:** backtest-vs-paper comparison and drawdown panels are not built yet (see the roadmap).

## 8. Prioritised roadmap

1. Run the audit on Dhan Cloud and archive the log; this produces the real performance numbers.
2. Paper trade F3+F4 unchanged for at least 3 months. Compare paper vs realistic backtest each week; any gap means an execution modelling error.
3. Keep downloading futures daily (VPS `download`) so the futures-only sample grows to ≥ 300 trades.
4. Add backtest-vs-paper, drawdown and data-quality panels to `index.html` (VPS).
5. Only after gates 1–5 pass on new data: consider 1-lot live trading with a hard daily loss cap. Never enable automatic orders.
