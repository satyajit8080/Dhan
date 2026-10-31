# Backtest results: v3.1 direction rules (run 30–31 Oct 2026)

SENSEX weekly options, 1 lot (20), real Dhan expired-option prices, minute
replay of `decideDirection` with no lookahead. Costs: 0.3% slippage each side
+ ₹50 per round trip. Full assumptions are in `server/scripts/backtest.ts`.
Per-run `summary.json` and `trades.csv` are in `results/`.

## Verdict: not profitable

| Run | Period | Trades | Win % | Gross / lot | Net / lot | Profit factor | Max DD |
|---|---|---|---|---|---|---|---|
| 1 month | 29 Sep – 29 Oct 2026 | 68 | 39.7 | −₹1,581 | **−₹8,957** | 0.59 | −₹10,668 |
| 6 months | 15 Apr – 29 Oct 2026 | 428 | 36.7 | +₹792 | **−₹44,221** | 0.58 | −₹49,204 |
| 1 year | 27 Oct 2025 – 29 Oct 2026 | 767 | 35.1 | +₹2,563 | **−₹79,643** | 0.58 | −₹79,713 |

Gross P&L is about zero over a year: the signals have no edge before costs,
and costs (~₹105/trade) make every month but April 2026 negative.

### Full year by setup

| Setup | Trades | Win % | Gross | Net | PF |
|---|---|---|---|---|---|
| Breakout, second bar | 253 | 39.1 | +₹5,958 | −₹22,006 | 0.72 |
| Breakout, retest | 66 | 28.8 | −₹2,863 | −₹10,144 | 0.42 |
| Rejection | 448 | 33.7 | −₹532 | −₹47,493 | 0.48 |

Stops: 402 trades, −₹1,65,356. Targets: 294 trades, +₹92,921.

## Variants tested (15 Apr – 29 Oct 2026)

| Variant | Trades | Win % | Gross | Net | PF |
|---|---|---|---|---|---|
| Baseline v3.1 | 428 | 36.7 | +₹792 | −₹44,221 | 0.58 |
| Wide stop (0.5×ATR, 10–60 pts) | 435 | 36.8 | −₹10,734 | −₹56,613 | 0.59 |
| Wide stop, breakouts only | 245 | 39.6 | +₹7,902 | −₹18,217 | 0.79 |
| Strict breakout (ADX ≥ 28, ≥ 15 pts, 20-min time stop) | 161 | 37.3 | −₹9,541 | −₹26,828 | 0.64 |

None is profitable. The best ("wide stop, breakouts only") still loses after
costs, and was picked from four tries on the same data, so its edge is not
yet evidence of anything.

## Live paper log, 30 Oct 2026 (Dhan Cloud observer v8.3)

Bid/ask fills, older trigger rules. Every variant lost: FIRST_TOUCH 120 trades,
35.8% TP vs 64.7% break-even, −830 pts; CONFIRMED_CLOSE 100 trades, 42%,
−470 pts. Same pattern as the backtest: win rate far below break-even.

## Known limits

- Oct–Dec 2025 index bars carry no volume, so no breakouts fire there.
- No order-book history: liquidity grade and quoted spread not replayed.
- OI/PCR from ATM±5 strikes, not the full chain.
- Fills at 1-min bar prices; live fills will be somewhat worse.
