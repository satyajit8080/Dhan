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

## 15-min opening-range breakout (run 31 Oct 2026)

`server/scripts/backtest_orb.ts`. Rules fixed before any result: range =
09:15–09:29 high/low; a 15-min close outside it buys ATM CE/PE (until 13:30,
one per side per day); stop −25% of premium; at +25% the stop moves to
breakeven and trails 15% below the highest close; exit 15:15. Same costs.
Chosen on Apr–Oct 2026, then checked on Oct 2025 – Apr 2026 (unseen).

### Choosing on Apr–Oct 2026

| Variant | Trades | Win % | Gross | Net | PF |
|---|---|---|---|---|---|
| **15-min close, expiring contract on expiry day** | 139 | 46.8 | −₹11,289 | −₹24,207 | 0.82 |
| 1-min close, expiring contract | 159 | 48.4 | −₹18,568 | −₹33,466 | 0.78 |
| 15-min close, next weekly on expiry day | 138 | 47.1 | −₹41,261 | −₹55,724 | 0.67 |

Only slice in profit: expiry days of the first variant, 34 trades, +₹3,701.

### Unseen data, Oct 2025 – Apr 2026 (15-min, expiring)

| | Trades | Win % | Gross | Net | PF |
|---|---|---|---|---|---|
| Overall | 125 | 44.0 | +₹8,763 | −₹3,290 | 0.97 |
| Expiry days | 30 | 36.7 | −₹2,578 | **−₹4,665** | 0.73 |
| Non-expiry days | 95 | 46.3 | +₹11,341 | +₹1,375 | 1.01 |

The expiry-day edge did not hold on unseen months.

### Full year (15-min, expiring)

264 trades, 45.5% win, gross −₹2,526, **net −₹27,497**, PF 0.89, max DD
−₹39,427. 104 of 223 traded days green.

Expiry days alone: 52 days, 19 green, net −₹964 for the year — but
**+₹15,229 of that came in October 2026**; the other 11 months lost
₹16,193 together. The "expiry-day edge" is one high-volatility month, the same
month the public experiments ran in.

Verdict: closer to break-even than the 1-min scalper (PF 0.89 vs 0.58, far
fewer trades so costs bite less), but no durable edge.

## Audit check: options are priced at the pinned strike (1 Nov 2026)

An external audit flagged a possible bug: marking a held option to Dhan's
ATM-relative series would stitch strikes together and strip out directional
P&L. The replay indexes every option row by its absolute `strike` field and
looks up the strike chosen at entry, so it should be pinned. Delta-slope test
on the actual trades (option points gained vs index points moved in the
trade's direction):

| Run | Trades | Slope | R² |
|---|---|---|---|
| 1-min scalper, 1 year | 767 | 0.43 | 0.75 |
| ORB, 1 year | 264 | 0.50 | 0.90 |

Slopes near ATM delta (≈0.5) confirm the strike is pinned. A stitched series
would show a slope well below 0.5. The "no gross edge" conclusion stands.

Accepted from the audit: real costs are higher (₹114–166 per round trip at
₹400, not ₹105), so every net figure above is optimistic; the ORB loses less
because its cost is ~5–9% of the risk per trade, not because it trades less;
264 trades cannot separate break-even from a modest edge either way; the
Thursday expiry rule misses the shifted expiries (e.g. 14 Jan 2026).
