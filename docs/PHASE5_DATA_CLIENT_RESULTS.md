# Phase 5 — Local Read-Only Dhan Integration: Results

Date 9 Oct 2026 · Python 3.11.17 / 3.12 / 3.13 · Node 22.22 ·
statuses PASS / FAIL / UNVERIFIED / BLOCKED.
**No live Dhan call was made** (this environment cannot reach Dhan, and no
credentials were used). Everything below is deterministic or MOCK.

## Summary

| Item | Status |
|---|---|
| Read-only client (allow-list, retries, rate limits, errors, redaction) | PASS (tests) |
| Validation (chain, quote, candles, expiries, duplicates) | PASS (tests) |
| Scanner + CLI (statuses, loop, interval, 15:30 stop, shutdown) | PASS (tests) |
| Stage A: Python scanner vs TypeScript plugin client on identical HTTP bodies | **PASS**: 185/185 numbers bit-identical, 137 non-numeric exact, tables identical at 5 session times × 3 scenarios |
| Record → TS replay → Python replay tooling (for live parity) | PASS on mock recordings; tamper detected |
| Endpoint request shapes | VERIFIED against the official SDK source |
| Response envelope of `/charts`, `/marketfeed` | **UNVERIFIED** (both shapes handled and logged) |
| Instrument-master parser / automatic futures lookup | Logic PASS on a SYNTHETIC format; real format **BLOCKED** (header not provided) |
| Dhan Cloud compatibility | **BLOCKED** (probe output not provided) |
| Live parity with the plugin | **UNVERIFIED**: no live comparison has been performed |

## Stage A: deterministic comparison with the existing TypeScript plugin

- **Method:** `server/test/parity/stageA.ts` drives the **real** `Bull50DhanClient`
  (transport envelope parsing, default expiry rule, futures registry,
  single-timestamp rule, gate, `getCandles`). It uses a stubbed `fetch` serving
  fixed Dhan-shaped bodies, with `Date.now` shifted to the scenario's market time.
- **What is recorded:** the bodies, the TS receipt times and the refresh-table
  inputs from `get_market_snapshot` + `get_candles` + `compute_levels` defaults.
- **Python side:** `test_scanner.StageAParity` replays the same bytes at the same
  receipt times through `DhanClient` → validation → engine → `refresh_table`.
- **Drift test:** `server/test/stage-a.test.ts` recomputes the committed expected
  values from the committed bodies and receipts with TS functions, and re-runs
  the live TS client path (status, call order, expiry).

| Scenario | Data | Result |
|---|---|---|
| `gm18Sep` | GM 18-Sep chain (real prices), synthetic futures quote with the registry's SEP id, real 18-Sep 5-min candles | PASS, identical |
| `s21Sep` | 21-Sep sample legs (real quotes), synthetic futures quote, real 21-Sep candles to 10:33 | PASS, identical |
| `gmFuturesDiverge` | futures 75200 → gate | PASS: both BLOCKED with identical reasons; blank table |

Call order is identical in both:
`/optionchain/expirylist → /optionchain → /marketfeed/quote → /charts/intraday`.
Chosen expiry is identical (2026-09-24, from the "next weekly after today" rule).
Bare-envelope bodies give the same Python result as wrapped ones.

Phase 4 parity is unchanged: 10,461 numbers, 10,405 bit-identical, max
relative 2.0e-12.

## Tests

| Command | Result |
|---|---|
| `cd engine && python3.X -B -m unittest discover -s tests -t tests` | **101 tests OK** on 3.11, 3.12, 3.13 |
| Same, with `socket.connect`/`getaddrinfo` patched to raise | **101/101 OK**: no test touches the network |
| `cd engine && python3.X -m unittest test_refresh_table` | OK on each |
| `cd engine && python3 tests/parity_report.py` | Phase 4 statistics unchanged |
| `cd tools && python3.11 -m unittest test_dhan_cloud_probe` | OK |
| `cd engine && python3 scan_local.py --mock` | OK, labelled MOCK |
| `cd server && npm run typecheck && npm run build` | PASS |
| `cd server && npx vitest run` | **282/282 PASS** (+6 Stage A) |
| Record 3 mock scans → `replay-recorded.ts` → `tests/replay_recorded.py` | 3/3 PASS. Recordings contain no token, client id or headers. A 0.01 change to one TS value → FAIL (detected) |

New Python tests by area:
- **client (19):** allow-list on 18 mutating paths with zero requests; request
  shapes and headers; envelopes; 429 with `Retry-After`; bounded throttling;
  network/502/`DH-908` retried; 7 non-retryable classes; rate limits; three
  credential-leak tests.
- **instruments/session/validation (18):** synthetic-format parsing, malformed
  rows, header mismatch, mapping must be explicit and verified, expiry-day
  selection, expired, missing, duplicate, far-future, explicit contract; session
  boundaries 09:15 / 15:29:59 / 15:30, weekend, holiday file, UTC conversion,
  naive clock refused; validation codes.
- **scanner (24):** Stage A ×3, all statuses, candle outage, stale candles,
  duplicate snapshot, expiry-day next weekly plus exact request bodies, loop
  interval/max scans/15:30 stop/no start outside session/Ctrl-C mid-wait/auth
  stop, CLI (mock, missing credentials, expired token), recorder.

**Mutation checks** (one-line edits to the code, full suite run each time):

| Mutation | Result |
|---|---|
| payload key `Expiry` → `expiry` | caught |
| expired futures allowed | caught |
| chain-pairs check disabled | caught |
| scan min touches 2 → 3 | caught |
| `next_after` → `nearest` | survived at first → expiry-day test added → caught |
| candle window ends 15:25 | survived at first → request-body test added → caught |
| redaction registration removed | survived at first → non-JWT echo test added → caught |
| `/orders` added to the allow-list | survives: **equivalent**, the forbidden-fragment check still refuses it |
| raw quote passed on validation failure | survives: **equivalent**, the gate still blocks with `NO_FUTURES_QUOTE` |

## Defects found and fixed during this phase

- **Loop timing:** the loop measured elapsed time with the real monotonic clock
  instead of the injected session clock, so the interval drifted and a scan
  could start just before the stop time. It now uses one clock (test
  `test_stops_at_session_end`).
- **Candle outage:** failing the whole scan on a candle error contradicted SKILL.md;
  the scan now degrades to a warning plus the RULES.md §5 blank table.

## BLOCKED / UNVERIFIED and exactly what unblocks it

| Item | Status | Exact input needed |
|---|---|---|
| Automatic futures lookup | BLOCKED | Output of `curl -s https://images.dhan.co/api-data/api-scrip-master.csv \| head -2`, and of `… \| grep -i sensex \| grep -i fut \| head -3` |
| Response envelopes for `/charts/intraday`, `/marketfeed/quote` | UNVERIFIED | One `--once --record` run; the log's `envelopes` field shows the shape |
| Whether Dhan needs `dhanClientId` in JSON bodies | UNVERIFIED | Same smoke test (success means not required) |
| `previous_oi` in live chains | UNVERIFIED | Same recording |
| Live parity with the plugin | UNVERIFIED | Stage B below over several sessions |
| Dhan Cloud | BLOCKED | Phase 3 checklist C1–C16 output |
| ATM±2 rule, no-IV row display | BLOCKED (your decision) | PHASE4_PORT_PLAN P1, P2 |
| CE/PE signals | BLOCKED (your decision) | D1–D8 |

## Stage B: optional local live smoke test (read-only, you run it)

Run during market hours, after setting up credentials securely (design doc §7).
Each command makes only read-only calls.

```bash
cd engine
python3 scan_local.py --profile-check                       # 1 GET /profile; prints field names only
python3 scan_local.py --once --record ../scan-records \
    --strikes <5 strikes around the current ATM> \
    --futures-security-id <id> --futures-expiry YYYY-MM-DD   # one scan, raw bodies saved
cd ../server && npx tsx scripts/replay-recorded.ts ../scan-records
cd ../engine && python3 tests/replay_recorded.py ../scan-records   # PASS/FAIL vs TypeScript, same live data
```

- The futures id must be a currently listed SENSEX future, verified in Dhan web
  or the instrument master. Example: the plugin registry's OCT contract
  `864571`, expiry `2026-10-29` (REPORTED in RULES.md §8, not re-verified here).
  The scanner refuses an expired one.
- **Over several sessions:** run `--interval 60 --record ../scan-records` for a
  few minutes a day (Ctrl-C to stop), then replay as above. Each `scan-NNN` gets
  PASS or FAIL against the TS functions on the identical live data. To compare
  with the plugin's own `refresh`, ask the plugin within the same minute; values
  differ when the snapshot time differs, so judge structure (levels, statuses),
  not exact premiums.
- Send back the replay output and the `envelopes`/`warnings` fields from
  `scan-logs/*.jsonl` (they hold no secrets).
