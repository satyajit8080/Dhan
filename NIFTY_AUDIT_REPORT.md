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
- Deflated Sharpe with **27 variants tried** on this data (26 + the order-flow confirmation variant)
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

---

## 9. Order-flow integration (concepts from MrChartist/order-flow)

**Repository audit.** `github.com/MrChartist/order-flow` is a browser dashboard that reads futures build-up (price vs OI
change), sums OI over all listed months, marks the 15-minute "zone" that added the most OI, and leans sectors by
majority. Licence: **PolyForm Noncommercial 1.0.0** (Required Notice: Copyright (c) 2026 Mr. Chartist). No code was
copied; the ideas were re-implemented and the notice is kept in the source. It has **no trade-side data either**: its
"flow" is OI + price, not aggressor volume.

**Verified Dhan capabilities**

| Capability | Dhan provides | Used for |
|---|---|---|
| Futures 1m/5m bars with volume and OI (`/charts/intraday`, `oi=true`) | Yes, history | Build-up, all-months OI, BVC delta, relative volume, **back-testable** |
| WebSocket FULL packet: LTP, cumulative volume, OI, total buy/sell qty, 5-level depth | Yes, live only | Live quote-rule delta, book imbalance |
| `/marketfeed/quote` for NSE_EQ + NSE_FNO | Yes, live only | NIFTY 50 breadth (cloud), book (cloud) |
| Trade aggressor flag / tick-by-tick trades | **No** | → every delta/CVD is labelled **ESTIMATED** |
| Historical depth, historical constituent quotes | **No** | → book and breadth never back-tested |
| Redis | Not in the existing infrastructure | → in-memory state + SQLite (`orderflow_snapshots`) |

**What was integrated**

| Feature | Cloud `dhan_cloud_single/main.py` | VPS `main.py` |
|---|---|---|
| Futures OI build-up (15m / 60m / day), LONG_BUILD / SHORT_COVER / SHORT_BUILD / LONG_UNWIND | `buildup`, `of_window` | same library |
| All-months OI | `oi_all_months` (per-contract history) | near + next 2 futures subscribed on the existing WebSocket |
| Volume delta / CVD | BVC on 5m bars (**estimated**) | BVC + live quote-rule/tick-rule from packets (**estimated**, method shares shown) |
| VWAP, relative volume (same time of day, 10 sessions) | ✓ | ✓ |
| Order-book imbalance (total qty and 5-level weighted, spread) | from `/marketfeed/quote` each cycle | from FULL packets (already subscribed) |
| NIFTY 50 breadth + sector median/lean, leaders/laggards | one `/marketfeed/quote` call per cycle | 50 constituents in quote mode on the same WebSocket |
| Explainable signal | `orderflow_state`: 6–9 PASS/FAIL/n-a checks, OF_BULLISH/OF_BEARISH if ≥ 60 % pass and fresh positions not contradicted | same |
| Alerts | Telegram on change ("research signal, not a trade") | Telegram via existing `Alerter` |
| Paper trading | ledger records whether order flow agreed at entry; summary split agreed vs not | TECH signals store order-flow state in `signals.features` |
| Dashboard | Telegram card section | `orderflow.html` + `/api/orderflow` (manual REFRESH, history of last 30 snapshots) |
| Historical validation | audit **PHASE OF**: coverage, OF signals traded alone, forward hit rate vs unconditional, random-entry permutation, F3+F4 split by agreement, F3+F4+OF confirmation | — (uses the cloud audit) |
| Tests | self-test +5 checks (30/30) | `tests/test_orderflow.py` (12 tests); suite 52 passed |

No data pipeline was duplicated: the cloud reuses `intraday()` / `post()`; the VPS reuses `MarketFeed`, `MinuteBars`,
`Database`, `Alerter` and the health server, and loads the single order-flow library from `dhan_cloud_single/main.py`.

**Limitations that remain**
1. Delta/CVD are estimates (BVC, quote/tick rule). They can be wrong on any individual bar.
2. Breadth and book cannot be back-tested; the historical test covers price + OI + estimated volume only.
3. Futures history is ~2.5 months (the same period F3/F4 were selected on): PHASE OF is in-sample, no OOS yet.
4. Breadth is equal-weight; the constituent list is as of the Sep-2025 rebalance and must be updated each review.
5. VPS all-months OI starts at engine start (earlier minutes: near month only).
6. The "F3+F4 + order-flow confirmation" variant is reported but **not enabled** for paper or live; enabling it would be
   parameter selection on the same data. Mock-data run of PHASE OF showed no edge for order-flow signals alone.
