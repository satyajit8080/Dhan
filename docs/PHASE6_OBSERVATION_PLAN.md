# Phase 6: One-Week Read-Only Observation Plan

**Zero orders.** The observer has no order, modify, cancel, exit, position or
kill-switch code. Its HTTP client refuses anything outside 8 read-only
endpoints before opening a socket. CE/PE signals stay **NOT_CONFIGURED**.
ATM±2 selection, handling of legs without IV and the D1–D8 rules stay
unchanged until you decide them.

## 1. Preconditions (all must hold before day 1)

| # | Precondition | Evidence |
|---|---|---|
| 1 | P1 runtime probe passed in Cloud | `PROBE python=`, `reach_dhan_api=HTTP …`, `done=` lines |
| 2 | P2 decided the build (multi or single) | `PROBE2 sibling_import=` |
| 3 | P3: environment variable readable | `env_marker_present=True` |
| 4 | **V1 validation run passed in Cloud** | `"event": "selftest", "result": "PASS"` in the Cloud log |
| 5 | V3 live check: one scan, decoded, TS replay PASS | `replay.txt` shows `PASS` |
| 6 | Futures contract verified by you | Id and expiry checked in Dhan web or the instrument master |
| 7 | `HOLIDAYS` list covers the week | Your exchange-holiday source |

If any precondition fails, the observation does not start (deployment doc §6).

## 2. The week

- **Duration:** 5 consecutive trading sessions. A listed holiday is skipped,
  not counted.
- **Schedule:** Monday to Friday, start 09:16 IST. The program stops itself at
  15:30 and logs `session_summary`.
- **Strikes:** keep the same `STRIKES` all day. Change them only between
  sessions, if `atmStrike` drifts far away, and note the change in that day's report.
- **Futures:** the same contract all week, unless it expires.

## 3. Daily routine

| When (IST) | Who | Action |
|---|---|---|
| Before 09:15 | You | web.dhan.co → My Profile → Access DhanHQ APIs → new token → paste it **only** into the Cloud variable `DHAN_ACCESS_TOKEN` |
| 09:16–09:20 | You | Open the run's live log. Expect `runtime`, then `token_status` with `hours_left` about 24, then the first `scan`. Before 09:25 its `session` is `OPENING (no-trade window to 09:25)`, which is normal |
| During session | Nobody | No intervention. Do not change config mid-session |
| After 15:30 | You | Confirm `session_summary` (`ordersPlaced: 0`). Download the log → `observation/<date>/cloud_log.txt` |
| Evening | You (or me, from the decoded files you share) | Run decode → TS replay → Python replay → `REPORT.md` (deployment doc §5), then fill in the gap template |

Share only the decoded `events.jsonl`, `decode_report.json`, `replay.txt` and
`REPORT.md`. The decoder has already refused any log containing a token-shaped
string. Never share the token or the raw Cloud UI.

## 4. Stop conditions (stop the schedule the same day)

- Any log line suggesting an order, position or account-changing path. This is
  impossible by construction; if it happens, it is a severity-1 defect.
- `AUTH_FAILED` that persists after regenerating the token, or `PLAN_MISSING`.
- `decode_cloud_log.py` refuses a log (a token-shaped string reached the log).
- The Cloud cost estimate exceeds what you accept.

## 5. What each day's report separates

| Category | Question | Primary evidence |
|---|---|---|
| **1. Data retrieval** | Did Dhan return complete, fresh, well-formed data, and did the client handle failures correctly? | Per-endpoint success/retry/duration and envelope shape (settles the `/charts` and `/marketfeed` envelope question); chain completeness; skew and ages; warnings; errors |
| **2. Calculation** | Given that data, did Python compute exactly what the TypeScript plugin computes? | TS/Python replay on recorded scans (bit-level comparator, tolerance 1e-9); gate numbers; IV coverage; our IV vs vendor IV (diagnostic only) |
| **3. Strategy rules** (incl. PAPER 6/11) | What did the existing rules do with correct data, and how would TP +6 / SL −11 have fared on trigger crossings? | Gate decisions; level availability and crossings; row-status distribution by session window; projected vs observed premium at trigger (indicative only); CE/PE **NOT_CONFIGURED** |

A failure in category 1 is never counted against category 2 or 3. A category 3
observation is never turned into a threshold here. Thresholds are your
decision (D1–D8).

## 6. Acceptance criteria for the week

| Criterion | Target |
|---|---|
| Orders placed | **0** (every `session_summary`) |
| Secrets in logs | **0** (decoder secret check PASS every day) |
| Sessions with a complete log and `session_summary` | 5 / 5 |
| Scans with endpoint outcome logged | 100% |
| Replay records decoded | 100% complete (else lower `RECORD_CHUNK`) |
| Python vs TypeScript replay | **100% PASS** on every recorded scan |
| `ERROR` / `INVALID_DATA` scans | Each one explained in the gap analysis (category 1) |
| `BLOCKED` scans | Each one explained (gate reason plus numbers) |

## 7. Open items the week settles (or not)

| Item | Settled by |
|---|---|
| `/charts/intraday` and `/marketfeed/quote` envelope (wrapped or bare) | `envelope` in endpoint outcomes |
| Whether the Dhan body needs `dhanClientId` | Success of the first live call |
| `previous_oi` present in live chains | `legsWithoutPreviousOi` |
| Cloud log streaming, line limits, scheduling accuracy | Log timestamps and `decode_report.json` |
| ATM±2, legs without IV, D1–D8, automatic futures lookup | **Not settled by observation.** They need your decision or the verified instrument-master header |
