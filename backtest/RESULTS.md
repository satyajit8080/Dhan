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

## Pre-registered round: H1a and H2 (1 Nov 2026)

Rules in `PREREGISTRATION.md`, committed (64fc218) before the code
(`server/scripts/backtest_prereg.ts`) was written. Corrected costs: half-spread
0.6% (1.0% on 0DTE) per fill, stress 1.0% (1.5%), real BSE charges with STT by
date, flat 15:10 on expiry days. Full year, 27 Oct 2025 – 29 Oct 2026.
Results in `results/prereg/`.

| Test | Trades | Win % | Net (base) | Per trade | Net (stress) | Passes |
|---|---|---|---|---|---|---|
| Baseline naked ORB, corrected | 264 | 45.5 | −₹43,821 | −₹166 | −₹59,818 | — |
| H1a: expiry-day ORB, only if Q ≤ 1.00 | 3 | 0 | −₹3,074 | −₹1,025 | −₹3,169 | **No** |
| H1a control: ungated, same eligible days | 32 | 31.3 | +₹4,659 | +₹146 | +₹3,473 | No (fails best-month, top-5, random control) |
| H2: same signals as a debit spread | 264 | 22.0 | −₹1,09,226 | −₹414 | −₹1,29,661 | **No** |

- **H1a mechanism, wrong sign:** Spearman(Q, day P&L) = +0.37 over 27
  eligible days (p one-sided 0.97). Cheap-straddle days did *worse*; the
  most expensive tercile did best. Only 2 days met the gate.
- **H2 paired:** the spread did worse than the naked buy on the same signal
  by ₹248 per signal (t = −1.82); −₹86,276 even without its top 5 days.
- **Stop rule triggered:** the baseline loses more than ₹50 per trade and
  neither hypothesis passes. Per the pre-registration, intraday SENSEX option-
  buying research stops here.

### Flaw found in the registered expiry detector

The registered rule (ATM straddle time value ≤ 50 pts at the last bar) found
only 36 expiry days: on real expiry days the ITM leg is priced off the
settlement price, not the last index print, so its "time value" reads 54–89
pts. The verdicts above are as registered. A labelled post-hoc re-run
(`EXPIRY_RULE=otm`: also an expiry day if the cheaper ATM leg closes at ≤ ₹1,
`results/prereg_robustness/`) finds 53 expiry days, differing from Thursdays
only on 24 Dec 2025, 14 Jan, 25 Mar and 27 May 2026 — the known shifts. It
changes no verdict: baseline −₹168/trade; H1a 7 trades −₹4,105, mechanism
r = +0.27 (p 0.96); H2 −₹1,09,630.

### Flaw in the random-entry control

The control draws entry minutes from the whole 09:44–13:29 window, including
minutes *before* the real signal, while keeping the signal's side. It therefore
knows which way the day broke, which is why random entries averaged +₹46,670
against the baseline's −₹43,821. It is biased against every hypothesis; it did
not decide any verdict here, since all of them already failed rule 1. A future
control should draw only from minutes at or after the real signal.

### Variant budget

9 of 20 trials used (7 earlier + H1a + H2). The robustness re-run is a check
of the data rule, not a new trial.

## Pre-registered H3: hedged expiry-day iron fly (1 Nov 2026)

Rules in `PREREGISTRATION.md` (commit c65d51c, before the code,
`server/scripts/backtest_h3.ts`). Sell ATM CE + PE at 09:45 on expiry days,
buy wings at ATM ± the straddle price, exit 15:10 or at a loss of 1× credit.
1.0% half-spread per leg (stress 1.5%), 8 orders of real charges. Results in
`results/h3/`.

| | Value |
|---|---|
| Expiry days / traded | 53 / 46 (5 skipped: wing beyond our ±500 data band; 2: leg missing) |
| Win rate | **52.2%** |
| Average win / loss | +₹1,547 / −₹1,586 |
| Gross / net (base) | +₹11,760 / **+₹2,237** (₹49 per day) |
| Net (stress fills) | −₹1,665 |
| Worst day / best day | −₹3,477 (5 Mar 2026) / +₹3,770 (30 Apr 2026) |
| Max drawdown | −₹8,305 |
| Without best month (Feb 2026, +₹6,153) | −₹3,916 |
| Top 5 days | +₹16,845 (7.5× the net) |
| Random-entry control | p = 0.29 (random entry averaged −₹3,475) |

**Fails 4 of the 5 checks** (passes only "net > 0 at base fills"). The win rate
is about a coin flip, not the 65–80% often claimed for option selling: wings at
ATM ± straddle give back much of the premium. Costs (₹9,523 over 46 days) take
81% of the gross.

Notes:
- The stop never fired: with wings at ± the straddle, the maximum loss is
  below 1× the credit, so the registered stop could not trigger.
- The 5 skipped days are the highest-straddle days (wing > 500 pts), so the
  result excludes the most volatile expiries; the bias direction is unknown.

**Variant budget: 10 of 20 used.**

## H3T out-of-sample: NIFTY, 5 years (31 Oct 2026)

Frozen H3T (PREREGISTRATION.md, e6ea421): expiry-day iron fly, 30% target,
`server/scripts/backtest_h3x.ts`, data from `backtest/fetch_expiry.py`.
NIFTY lot 65, NSE charges, STT by date. 1 Nov 2021 – 30 Oct 2026.
Results in `results/h3t_nifty/` (and `h3t_nifty_spread013/`, post-hoc 0.13%).

| | Value |
|---|---|
| Expiry days / traded | 253 / 251 |
| Win rate | 52.2% (target hit 121, flat 129, stop 1) |
| Average win / loss | +₹1,188 / −₹2,170 |
| Net (1% half-spread) | **−₹1,04,789** (−₹417 per expiry) |
| Net (stress 1.5%) | −₹1,35,383 |
| Net (0.13% measured spread, post-hoc) | −₹64,098 (55% win) |
| Max drawdown | −₹1,06,761 |
| Years positive | **0 of 6** |

**Fails.** Every year lost money. The 84.8% SENSEX win rate does not
replicate: days that miss the 30% target are held to 15:10 and lose
twice what the winners make. Per COMPARISON.md step 5, the iron fly is
stopped. SENSEX (May 2023 – Oct 2025) run follows when its download ends.
