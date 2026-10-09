# Phase 6 Gap Analysis: Session `<YYYY-MM-DD>` (template)

Copy this file to `observation/<date>/GAP_ANALYSIS.md` and fill it in from
`REPORT.md`, `decode_report.json` and `replay.txt`. Keep the three categories
separate. Record facts; do not propose thresholds.

## Run identity

| Field | Value |
|---|---|
| Date / weekday | |
| Program version / build (multi or single) / Python | |
| Mode / schedule start / actual first scan / stop reason | |
| Strikes / futures contract (id, expiry) / option expiry used | |
| Scans total / by status | |
| `ordersPlaced` (must be 0) | |
| Decoder secret check | PASS / REFUSED |
| Config changes vs previous session | |

## 1. Data retrieval correctness

| Check | Observed | Expected | Gap? | Note |
|---|---|---|---|---|
| Endpoint success, `/optionchain/expirylist` | n / N | 100% | | |
| Endpoint success, `/optionchain` | | 100% | | |
| Endpoint success, `/marketfeed/quote` | | 100% | | |
| Endpoint success, `/charts/intraday` | | 100% | | |
| Retries (count, error classes, codes) | | rare | | |
| Envelope `/charts/intraday` | wrapped / bare | — | | Settles the Phase 5 open item |
| Envelope `/marketfeed/quote` | | — | | |
| Chain strikes min / median | | stable | | |
| Legs two-sided (min) | | — | | |
| Legs without OI / previous OI (max) | | 0 | | |
| Chain↔futures skew p50 / max (ms) | | < 3000 | | |
| Futures last-trade age max (s) | | < 15 | | |
| Last candle age p50 / max (s) | | ≤ 350 | | |
| `DUPLICATE_SNAPSHOT` count | | ~0 in session | | |
| `CANDLES_UNAVAILABLE` / `NO_CANDLES` count | | 0 | | |
| `ERROR` / `INVALID_DATA` scans (time, reason) | | 0 | | |
| Truncated log lines / incomplete records | | 0 | | |

## 2. Calculation correctness

| Check | Observed | Expected | Gap? | Note |
|---|---|---|---|---|
| Replay records decoded | | all | | |
| Python vs TypeScript replay | PASS n / FAIL m | 100% PASS | | Paste any FAIL lines verbatim |
| Gate evaluated / blocked | | — | | |
| Block reasons (grouped) with numbers | | explained | | |
| Futures − parity forward p50 / range (pts) | | within gate | | |
| Per-strike forward spread p50 / max (pts) | | < 40 | | |
| Gate warnings (codes) | | — | | |
| Legs without computable IV (count) | | — | | PHASE4 P2 unchanged; excluded, not displayed |
| Our IV − vendor IV p50 / p95 \|Δ\| | | diagnostic only | | Vendor IV is never used |

## 3. Strategy-rule effectiveness (existing rules only)

| Check | Observed | Note |
|---|---|---|
| CE/PE direction | NOT_CONFIGURED | D1–D8 undecided; no signal evaluated |
| Scans by session window | | Opening / morning / midday / afternoon / closing |
| Refresh-row statuses (OK / WEAK_LEVEL / NO_LEVEL / NO_TRADE_WINDOW / STALE) | | |
| Levels published (breakout/breakdown, first time) | | |
| Levels reached by the index (time) | | Scan granularity, not tick |
| Projected premium at trigger vs observed ask after crossing | | Indicative only; overshoot between scans |
| Excluded table legs (reason) | | Strike not in chain / no IV |
| Was `STRIKES` near `atmStrike` all session? | | Input to your ATM±2 decision (P1) |

## Gaps found

| # | Category (1/2/3) | Description | Evidence (scanId / time) | Severity | Proposed action (no threshold changes) |
|---|---|---|---|---|---|
| | | | | | |

## Decisions still required from you (unchanged unless you decide)

- D1–D8 CE/PE direction rules (RULES.md §6, PHASE3_STRATEGY_GAPS).
- PHASE4 P1: which strikes form ATM±2.
- PHASE4 P2: how to display legs without computable IV.
- Automatic futures lookup: needs the verified instrument-master header.
