# Project summary: Dhan Quant engine and NIFTY strategy (as of 07 Nov 2026)

## Goal
A read-only research and signal system for Indian index derivatives (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, BANKEX) on the DhanHQ v2 API.
It never places orders. It produces signals, Telegram alerts and paper trades only.

## What was built (repo `satyajit8080/Dhan`, branch `claude/focused-tesla-21k8tu`)
1. **`main.py`, the full engine for an Ubuntu VPS, about 6,000 lines.**
   - Data: live WebSocket plus option-chain polling, SQLite storage.
   - Pricing: own Black-76 IV and Greeks.
   - Option-chain analysis: CE/PE buildup classification by price and OI.
   - Scoring: 0–100 evidence scores and a regime classifier.
   - Breakout event recorder: saves the market state 15/10/5 min before and 5/15/30 min after each move.
   - Statistics: pattern validation (Wilson CI, Benjamini-Hochberg FDR), walk-forward backtest with costs, look-ahead and leakage checks.
   - Outputs: Telegram alerts, terminal dashboard, health endpoint, systemd units, `index.html` signal page with a manual refresh button.
   - Tests: 57-item self-test and a pytest suite.
2. **`dhan_cloud_single/main.py`, a single file for Dhan Cloud (cloud.dhanhq.co).**
   - Dhan Cloud limits: Python 3.11, no files, no ports, and `os.environ` is blocked by its scanner.
   - Credentials come from the Env Variables tab as `{{DHAN_CLIENT_ID}}` and `{{DHAN_ACCESS_TOKEN}}` (optionally `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). The token expires daily.
   - It runs a self-test, can download 6 months of data in memory and backtest it, and runs a live loop on every closed 5-minute bar.

## Strategy (5-minute bars of the front-month future)
**Indicators, computed exactly:**
- VWAP from typical price, reset each session
- ATR(14), Wilder smoothing
- Supertrend(10,3)
- ADX/DI(14)
- RSI(14), Wilder smoothing
- Bollinger(20,2) bandwidth
- Efficiency Ratio(10)
- Z-score(20)
- Floor pivots plus previous-day H/L/C
- PCR by volume and by OI

**Setups:**
- **Trend filter:** trend trades only if ADX > 25 and ER > 0.3.
- **Trend pullback:** price above VWAP, Supertrend long, DI+ > DI−, RSI pulled back to 40–55 and turning up. SHORT is the mirror.
- **Mean reversion:** only if ADX < 20 and |z| > 2. Fade toward VWAP.
- **Squeeze breakout:** Bollinger bandwidth at a 20-bar low, then a close beyond the band in the Supertrend direction.

**Risk and execution:**
- Skip entries within 0.2×ATR of R1/R2/S1/S2/PDH/PDL unless the bar closes through the level.
- Stop = 2×ATR. Target = 4×ATR, or VWAP for mean reversion.
- Lots = risk ₹ ÷ (stop × lot size).
- Entries 09:30–15:10, exit by 15:20.
- Fill at the next bar's open, so there is no look-ahead.
- Charges included: brokerage, STT, exchange fee, SEBI fee, stamp duty, GST (about ₹440 per round trip on one NIFTY lot, ≈ 6.9 points).

## Data limits (Dhan)
- Intraday futures history exists only for contracts still trading, so it covers about 3 months (26 Aug – 6 Nov 2026).
- Index history covers 6 months, but index bars have no volume, so no VWAP. On index data only the squeeze breakout can fire.
- There is no historical option-chain data.

## Backtest results (1 lot, net of charges)
**Futures, 26 Aug – 6 Nov:**

| Index | Trades | Win % | Net ₹ | Profit factor |
|---|---|---|---|---|
| NIFTY | 76 | 50% | +46,912 | 1.39 |
| BANKNIFTY | 89 | 37% | −50,318 | 0.75 |
| FINNIFTY | 33 | 33% | −13,684 | 0.75 |
| SENSEX | 87 | 37% | −17,167 | 0.84 |
| BANKEX | 6 | 67% | +10,115 | too few trades |

- Total across all five: −₹24,143.
- Charges were about ₹1.28 lakh, more than the total gross profit.
- Main loss reasons: false signals (85 trades, −₹2.47 lakh); a weak move then the 2×ATR stop (40); was up 1R or more, then hit the stop (22).
- The trend pullback setup lost on 4 of 5 indices.
- NIFTY's p-value is 0.24, so its profit is not statistically significant.

**Tight fixed stops and targets (10/10, 15/15, 10/20, 15/30 points) were much worse on every index.** Charges of 7–19 points per trade wipe out such small targets.

**BANKEX over 6 months of index data:** 138 trades, 35% wins, −₹90,327. The strategy does not work on BANKEX.

**Opening-range breakout** (rule from github.com/Jakeschincariol/day-trader-skill) on NIFTY: profitable from August to November, but lost money from May to August (−₹43k to −₹78k depending on version). It depends on the market regime and was rejected.

## NIFTY filter tests
The filters were chosen from the Aug–Nov futures trades. They were then checked on NIFTY index data for May–Aug, which was not used to choose them.

| Filter | May–Aug (unseen) | Aug–Nov | Full 6 months | Max drawdown |
|---|---|---|---|---|
| Current rules | 40%, −₹2,780 | 49%, +₹28,375 | +₹25,595, PF 1.12 | −₹66,657 |
| F3: entries 09:30–13:00 only | 49%, +₹22,409 | 50%, +₹32,364 | +₹54,773, PF 1.38 | −₹32,401 |
| F4: squeeze close ≥ 2 bps beyond band | 47%, +₹29,142 | 44%, +₹14,606 | +₹43,748 | −₹30,309 |
| **F3 + F4 (chosen)** | **55%, +₹28,727, PF 1.62** | 42%, +₹12,791 | +₹41,518, PF 1.40 | **−₹19,266** |

Rejected:
- F1, max 1 trade per day: looked strong on futures but added nothing on unseen data.
- F2, stop after the first loss: no improvement.
- F5, move stop to breakeven at +1R: made results worse.

On NIFTY futures, F3+F4 gave 47 trades, 51% wins, +₹59,696, PF 1.78.

Sample sizes are small (33 and 31 trades per period). The edge is not proven.

## Current live setup (Dhan Cloud), deployed 07 Nov 2026
- `LIVE_INDICES = ["NIFTY"]`. Filters F3 + F4 are on.
- Paper trading on 1 futures lot, with Telegram messages on entry and exit, plus a daily summary at 15:25.
- The paper ledger is kept in memory only, so it resets when the strategy restarts.
- `RUN_BACKTEST_ON_START = False`.
- Plan: paper trade for 2–4 weeks before using real money. Renew the Dhan token every morning.

## Open ideas, not yet validated
- Cap the squeeze break at ≤ 10 bps (0.02–0.10% beyond the band); this needs testing on unseen data.
- Kronos (a candle-forecasting model) as a direction filter; it needs PyTorch on a VPS and cannot run on Dhan Cloud.
- Collect more futures history over time. Re-validate every rule on new data before trusting it.
