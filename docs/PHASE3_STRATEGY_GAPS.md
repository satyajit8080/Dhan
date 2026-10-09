# Phase 3 — Strategy Gaps (CE/PE signal readiness)

**Status: CE/PE signals are DISABLED, verified in source on 9 Oct 2026.**
- RULES.md §6: "**Direction thresholds are not configured.** Without them the
  server returns NO SIGNAL — never invent a CE/PE verdict."
- SKILL.md §4: what is not configured is "how far above the level counts as a
  break, what ADX counts as trending, what PCR counts as bullish, how many OI
  strikes must confirm… Until the user supplies those numbers, do not guess them."
- Code: no function anywhere chooses CE or PE. `get_analysis` returns levels,
  indicators, positioning, ranked strikes **for both sides** and trade plans
  "if that side were taken" (`client.ts`). Every tool description states "no
  directional verdict".

No values were chosen in this phase. Passing tests show that the calculations
are reproducible, **not** that any signal would be profitable.

## What still works without these decisions

The **refresh table** (RULES.md §5) is fully specified and does not depend on any
item below: ATM±2 CE/PE, LTP, projected premium at the breakout/breakdown level,
blank table on gate block / stale data / no-trade window. An automated scanner
can publish it today, subject to the port and the Cloud checks.

## Unresolved decisions

"Effect" = what the scanner must do until the decision is made.
"Evidence" = what would justify a value. That is your call: backtest, signal
log, or discretionary rule.

| # | Decision | Rule text it comes from | Effect on scanner today | Evidence needed to resolve |
|---|---|---|---|---|
| D1 | **Break distance**: how far beyond `breakoutAbove`/`breakdownBelow` a 1-min **close** must be (points, ATR fraction, or "any close beyond the buffered trigger") | RULES §6 "1-min close through a level"; SKILL §4 "how far above the level counts as a break" | Cannot detect a breakout event → NO SIGNAL | Distribution of post-break moves by break size in your signal log / 1-min history |
| D2 | **"Next bar holds"**: next bar's close beyond the level? its low/high? tolerance in points? | RULES §6 | Cannot confirm a break | Same data as D1: false-break rate under each definition |
| D3 | **Volume source**: SENSEX index bars carry no volume (`indicators.ts` vwapNote). Which series supplies "volume > 10-bar average" (front-month future? the option itself?), and does the average include the breakout bar? | RULES §6 | Volume condition is uncomputable on the index series | Your choice of series; check futures 1-min volume is populated (Dhan `/charts/intraday` with `FUTIDX`) |
| D4 | **Trend filter**: is ADX used? If so: threshold, period (code default 14), timeframe (1-min / 5-min), and whether +DI > −DI is required for CE (reverse for PE) | SKILL §4 "what ADX counts as trending" | Indicator computed, not used | Backtest of signals with and without the filter |
| D5 | **PCR**: OI-PCR or volume-PCR; strike range (whole chain or ±N strikes); bullish / bearish cut-offs; neutral band | SKILL §4 "what PCR counts as bullish" | PCR computed over the whole chain, not used | Historical PCR vs subsequent direction; or a stated discretionary rule |
| D6 | **OI confirmation**: how many strikes, which band around ATM, which buildup types count as confirmation (e.g. put short-buildup for CE), minimum OI change | SKILL §4 "how many OI strikes must confirm"; SKILL §1 example "put writing at…, call unwinding at…" | Buildup classified per leg, not used | Same as D5. Also confirm live chain responses carry `previous_oi` (Phase 1 open item) |
| D7 | **Combination logic**: must every confirmation pass (AND), or a score with a cut-off? If both a CE and a PE setup qualify, which wins? | SKILL §1 rule 6 "One signal only" | No way to produce a single verdict | Your decision |
| D8 | **Rejection scalp**: what a "wick rejecting a level" is (pierce then close back inside? minimum wick length?) and the tolerance | RULES §6 "2 consecutive 1-min wicks rejecting a level" | Rejection setups undetectable | Examples you consider valid / invalid, then codify |
| D9 | **Level quality for signals**: may `WEAK_LEVEL` (< 2 touches), a rolling-snapshot level (tier 2) or an exchange-range level (tier 3) trigger a *signal*, or only be displayed? 1-min vs 5-min levels when they disagree? | RULES §4–5; SKILL §0 tiers | Display only | Your decision; the signal log shows which tier each past signal used |
| D10 | **"No entry within 3 pts of a level"**: distance to the trigger, or to the next level? Interaction with the 5-pt buffer | RULES §6 | Filter not applied | Clarify wording |
| D11 | **Theta filter units**: "theta over 10 min < 1% of premium": calendar minutes (θ/day × 10/1440) or trading minutes (× 10/375)? | RULES §6; theta is per calendar day (RULES §2) | Filter not applied | Clarify; the two differ by ~3.8× |
| D12 | **IV filter**: define "chain ATM IV": CE IV, PE IV, or their mean at `atmStrike` | RULES §6 "IV ≤ chain ATM IV + 1 pt" | Filter not applied | Clarify |
| D13 | **Strike selection defaults**: are the code defaults your rules? Delta band 0.35–0.60, min grade B, max round-trip 1.2%, lots used for grading (`get_analysis` default 5, snapshot default 1) | `strikeSelect.ts` DEFAULTS; SKILL §3 Gate C ("A or B") | Code defaults apply but are not documented as rules | Approve or replace, then record in RULES.md |
| D14 | **Signal lifetime and de-duplication**: how long a signal stays valid, when the same level may re-fire, max alerts per day for an alert-only scanner | RULES §6 risk ("stop after 3 losing scalps") needs fills a scanner does not have | De-dup undefined; persistence also UNVERIFIED (checklist C8) | Your decision + Cloud persistence result |
| D15 | **Margin-based risk**: "max loss/trade 1% of available margin" needs the funds API (account data, not market data). Allowed for a read-only scanner, or out of scope? | RULES §6 | Not computed | Your decision (read-only fund access is still account access) |

## Resolution procedure (proposed, needs your approval)
1. Answer D1–D8 in writing. These are the minimum for any CE/PE output.
2. Record the answers in RULES.md §6 (your rules file; I will not edit it without approval).
3. Implement them as pure functions with tests that cite the RULES.md line.
4. Paper-log every would-be signal for 2–3 weeks (RULES.md §7 roadmap) before any alert is treated as tradeable.
