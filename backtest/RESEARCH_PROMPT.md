# Research prompt: find the gaps in our SENSEX option-buying work

Paste everything below the line into a research model.

---

You are a quantitative trading researcher reviewing an Indian index-options
project. Be skeptical and specific. I want gaps, errors and testable
improvements, not encouragement. Cite sources for any factual claim about
markets, costs or regulation, and say clearly when something is your opinion.

## The project

"Bull50" is a read-only market-data system for **buying SENSEX weekly
options** (BSE, lot size 20) intraday, using the Dhan API. It never places
orders. All P&L below is per 1 lot.

**Pricing core.** The index LTP is not used as spot. Spot is the forward
recovered from put-call parity, `F = K + (C − P)/DF`, taken as the median
across strikes within ±1.5% of ATM. IV and Greeks come from Black-76 on that
forward; the vendor's IV and Greeks are ignored. A data-integrity gate blocks
everything if the parity spread is over 40 pts, the carry vs the listed future
is outside −5% to +15%, or the forward is more than 75 pts from the future.

**Levels.** Support and resistance come from clustering 1-min price action,
counting touches (at least 2) and rejecting spikes, with a ±5 pt buffer.

## Strategy 1: 1-minute scalper (v3.1 rules)

- **Setup A, breakout:**
  - a 1-min close beyond the level by ≥ max(0.15 × 5-min ATR14, 8 pts);
  - break bar body ≥ 40% of its range, volume above the 10-bar average;
  - the next bar holds;
  - 5-min ADX ≥ 23 (below 20 never), ±DI agree by ≥ 3, ADX not falling;
  - OI confirms, read as writer flow: for CE, put OI building at/below the
    level, call OI unwinding at/above it, PCR rising; PE is the mirror.
- **Setup B, rejection:** two 1-min wicks rejecting a level, taken only when
  5-min ADX < 25.
- **Entry:** on the retest or the second bar. No fresh entry within 3 pts of
  the level; a retest after a confirmed hold is exempt.
- **Exits:**
  - stop on a 1-min close back through the level ± clamp(0.05 × ATR, 2, 4) pts;
  - target the next level if ≥ 1R away, else 1.5R;
  - 10-min time stop; flat by 15:15.
- **Session limits:** no trading 09:15–09:25, 11:30–13:00 or after 14:50.
  Stop for the day after 3 losses. One trade per level per side per day.

## Strategy 2: 15-minute opening-range breakout

Rules were fixed before seeing any result:
- **Range:** the 09:15–09:29 high and low.
- **Entry:** a 15-min close outside the range buys an ATM CE or PE, with
  signals allowed until 13:30 and one trade per side per day.
- **Contract:** on expiry day, the expiring weekly (0DTE).
- **Exits:** stop at −25% of premium. At +25% the stop moves to breakeven, then
  trails 15% below the highest close. Flat at 15:15.

## How we backtested

- **Data:**
  - SENSEX 1-min index candles from the Dhan API, cross-checked against an
    independent 1-year CSV: 50,247 overlapping bars, zero mismatches;
  - Dhan's expired-weekly options history (`/charts/rollingoption`): real
    1-min OHLC, IV and OI for ATM−5..ATM+5, CE and PE.
- **Replay:** minute by minute with no lookahead. A decision is made only on a
  closed bar, and entry is at the option's next-minute open. The 1-min
  scalper reuses the production code (`decideDirection`); it is not
  re-implemented.
- **Costs:** 0.3% slippage each side, plus ₹50 per round trip for charges.
- **Periods:** 27 Oct 2025 – 29 Oct 2026, 252 sessions.

## Results

| Test | Trades | Win % | Gross | Net | PF |
|---|---|---|---|---|---|
| 1-min scalper, 1 year | 767 | 35.1 | +₹2,563 | −₹79,643 | 0.58 |
| Scalper, Apr–Oct, wider stop (0.5×ATR) | 435 | 36.8 | −₹10,734 | −₹56,613 | 0.59 |
| Scalper, Apr–Oct, wider stop, breakouts only | 245 | 39.6 | +₹7,902 | −₹18,217 | 0.79 |
| Scalper, Apr–Oct, strict (ADX ≥ 28, ≥ 15 pts, 20-min time stop) | 161 | 37.3 | −₹9,541 | −₹26,828 | 0.64 |
| ORB, picked on Apr–Oct 2026 | 139 | 46.8 | −₹11,289 | −₹24,207 | 0.82 |
| ORB, unseen Oct 2025 – Apr 2026 | 125 | 44.0 | +₹8,763 | −₹3,290 | 0.97 |
| ORB, full year | 264 | 45.5 | −₹2,526 | −₹27,497 | 0.89 |

## What we concluded

1. Before costs, the 1-min scalper is roughly break-even over 767 trades:
   the signals predict nothing over a 1–10 minute hold. About ₹105 of costs
   per trade turns that into a large loss.
2. The scalper's stop was too tight. 402 of 767 trades ended at the stop,
   most of them within 2 minutes. Widening the stop did not help.
3. Retest entries were the worst entry type, the reverse of what we expected.
4. Every weekday lost after costs. Thursday (expiry) was not special for the
   scalper.
5. ORB comes closer to break-even (PF 0.89), mainly because it trades less.
   - Its expiry-day edge in Apr–Oct (+₹3,701 on 34 trades) failed on unseen
     months (−₹4,665).
   - Over the whole year, all of the expiry-day profit came from October
     2026: +₹15,229, while the other 11 months lost ₹16,193 together.
6. A live paper log from 30 Oct (real bid/ask, older trigger rules) lost in
   every variant. For example: 120 trades, 35.8% wins against a 64.7%
   break-even rate, −830 pts.
7. Viral posts from Oct 2026 show 4 green days trading the same instruments.
   Our losing scalper also had a 4-day green streak this year.

## Known gaps in our own work (please extend this list)

- **No order-book history.** Liquidity grade and quoted spread were not
  replayed; slippage is a flat 0.3% per side, whatever the premium, time or
  expiry.
- **Charges are a rough ₹50 per round trip.** They are not the real BSE
  schedule (STT on sell-side premium, exchange fees, SEBI fee, stamp duty,
  GST, brokerage).
- **Volume is missing for Oct–Dec 2025**, so scalper breakouts could not fire
  in those months.
- **OI and PCR cover only ATM±5 strikes**, not the full chain. The history is
  ATM-relative, so a strike the market leaves behind drops out of the data.
- **Fills use 1-min bar prices.** Scalper targets fill at the bar close when
  the underlying touches the target, and stops fill at the next minute's
  open.
- **Expiry day is assumed to be Thursday** (or the trading day before a
  Thursday holiday). This was not checked against the actual contract list.
- **Level touch counts reach 60–140.** That suggests clustering over two
  prior sessions is too loose.
- **Multiple testing.** Four scalper variants and three ORB variants were
  tried on the same Apr–Oct data. Only the ORB had a truly unseen check.
- **No volatility filter.** No India VIX regime, no IV-percentile filter, no
  IV-crush model on the break.
- **Buying only.** We tested no option selling, debit or credit spreads, or
  hedged structures.
- **One instrument.** SENSEX only, not NIFTY or BANKNIFTY.

## What I want from you

1. **Audit the method.** Find likely errors, biases or lookahead in the setup
   above that could make the results too pessimistic or too optimistic. Rank
   them by how much they could change the conclusion.
2. **Get the real costs.** Give the current cost of one round trip in SENSEX
   weekly options at the BSE (all statutory charges, with sources). Give
   realistic slippage for ATM SENSEX weeklies, intraday and on expiry day,
   from any published data. Then tell me whether ₹105 per trade at a ~₹400
   premium is too high or too low.
3. **Summarise the evidence.** What does published evidence say about
   intraday option *buying* edges in Indian index options? Include SEBI's
   studies on retail F&O profitability. Say what that implies for a target
   like ₹10k/day on ₹1 lakh.
4. **Propose hypotheses we can test.** Give 5–8, each with:
   - exact entry, exit and sizing rules, with every threshold fixed in advance;
   - the economic reason it might work: who is on the other side, and why
     they would accept losing;
   - the data it needs, and whether Dhan's API provides it;
   - how it would most likely fail;
   - a walk-forward split and the minimum trade count needed to tell an edge
     from noise.

   Cover at least these:
   - a volatility-regime filter (India VIX or IV percentile) on expiry-day
     breakouts;
   - defined-risk structures (debit spreads) instead of naked buys;
   - option *selling* with hedges, as a comparison;
   - longer holds (30–90 min) on 15-min signals;
   - NIFTY vs SENSEX.
5. **Guard against overfitting.** Recommend a concrete protocol for a small
   team using one year of 1-min data: walk-forward windows, a
   deflated-Sharpe-style adjustment or a reality check, and how many variants
   we may try before the result means nothing.
6. **Make a go/no-go call.** Based on everything above, should we keep
   researching intraday SENSEX option buying, switch approach, or stop?
   Justify the answer with the numbers.

Format: an audit table first, then cost figures with sources, then the
hypotheses as a numbered list with rules in code blocks, then the protocol,
then the go/no-go call. No hype, no guarantees.
