# Phase 4 — Parity Results (Python vs TypeScript)

Date 9 Oct 2026 · Python 3.11.17, 3.12, 3.13 · Node 22.22 · statuses PASS / FAIL / UNVERIFIED / BLOCKED.

## Method

- **Expected values come only from TypeScript.** `server/test/parity/builder.ts`
  calls the existing TS functions. `server/test/parity-fixtures.test.ts`
  regenerates the fixtures on every `npm test` and fails if a committed file
  differs; editing one digit by hand made it fail (verified).
- **Same inputs to both.** Raw Dhan-shaped JSON, candles and parameters are
  stored inside each fixture case. Python reads them back from there and never
  rebuilds them.
- **Determinism.** Fixed clocks, fixed fetch ids, no network, no credentials.
  Non-finite numbers and `undefined` are encoded explicitly
  (`{"$num":"NaN"}`, `{"$undefined":true}`) so JSON cannot hide them.
- **Independent anchor.** Python also reproduces the manually validated
  18-Sep golden master (`server/test/fixtures.ts`): forward 74616.5745,
  spread 12.907, CE 74500 IV 10.5167%, Δ 0.54824, θ −33.0798/day, vega 37.838.

## Tolerances

| Value kind | Rule |
|---|---|
| Strings (all messages, codes, reasons, dates), booleans, null, undefined | **Exact** |
| Dict keys, list lengths, ordering | **Exact** |
| Errors | Same TS error name, same message, same `reasons`, **exact** |
| Strikes, expiries, timestamps, counts, touches, grades, safety decisions | **Exact** (they are integers/strings; any difference fails) |
| Other numbers | `|py − ts| ≤ 1e-9 + 1e-9 × |ts|`; NaN = NaN, ±∞ exact |

**Why 1e-9:** V8 and glibc implement `exp`/`log` independently and can differ
by one ulp. The observed worst case is 2.0e-12 relative, so 1e-9 leaves
about 500× headroom while still catching real defects. Changing a CDF
constant in its 15th significant digit (≈5e-15 relative) was *not* detected,
which is expected and is the cost of the tolerance; changing it in its 11th
digit (≈6e-11 relative) was.

## Results by module

| Module | Fixture cases | Python tests | Numbers compared | Bit-identical | Within tol. | Max abs | Max rel | Status |
|---|---|---|---|---|---|---|---|---|
| pricing (normal, time, Black-76, IV, Greeks, forward, gate) | 46 | 6 | 1,370 | 1,367 | 3 | 1.5e-11 | 2.0e-12 | **PASS** |
| normalize + integrity | 33 + 34 | 8 | 1,303 | 1,303 | 0 | 0 | 0 | **PASS** |
| pricing bridge + liquidity | 27 + 32 | 5 | 2,606 | 2,555 | 51 | 2.1e-13 | 6.7e-14 | **PASS** |
| levels (indicators, swings, candidates, levels, trade plans) | 49 | 4 | 4,839 | 4,839 | 0 | 0 | 0 | **PASS** |
| expiries + scan | 8 + 9 | 6 | 292 | 290 | 2 | 1.1e-14 | 9.7e-16 | **PASS** |
| jscompat (raw JS semantics) | 8 | 7 | 51 | 51 | 0 | 0 | 0 | **PASS** |
| **Total** | **246** | **36** | **10,461** | **10,405 (99.5%)** | **56** | **1.5e-11** | **2.0e-12** | **PASS** |

Non-numeric values compared exactly: 6,835. Identical on 3.11, 3.12 and 3.13.
The 56 non-identical numbers are all `exp`/`log` paths: Black-76 prices,
bisection IVs and the Greeks derived from them.

**Refresh table, end to end:** for all 9 scan cases, `refresh_table`
produced *identical* rows and identical text whether fed the TS-computed or
the Python-computed scan. This held at five session times (10:33 in session,
09:20 opening skip, 12:00 lunch skip, 14:55 after 14:50, 15:45 after close):
45 table comparisons, all equal.

## Coverage of required scenarios

| Scenario | Cases |
|---|---|
| Empty responses | `normalizeChain/emptyObject`, `/emptyOc`, `/nullRaw`; `normalizeQuote/emptyObject`, `/nullRaw`; `toCandles/empty`; `scan/emptyChain`, `scan/noCandles`; `deriveLevels/empty` |
| Missing fields | `normalizeQuote/missingLtp` (undefined vs null kept distinct), `/nullLtp`, `/ohlcPartial`; `toCandles/missingClose`, `/ragged` |
| Malformed values | `normalizeChain/malformedLegs` (string numbers, booleans, arrays, `{}` greeks, null/number strike values), `/oddKeys` (`abc`, `""`, `0x10`, `1e5`, `Infinity`, `nan`, `1_000`, duplicate `74100`/`74100.000000`), `/ocArray`, `/ocNotObject`; `normalizeQuote/badLtt`, `/overflowLtt`, `/depthArray`, `/depthMalformed`; `parseLastTradeTime` (11 forms); `isIsoDate` (13 forms); malformed expiry lists |
| Stale data | `checkQuote/stale`; freshness boundary at exactly 15.000 s (not stale) vs 15.0005 s (stale) and future trade time; `checkSnapshotSkew` at exactly 3000 ms vs 3001 ms; `scan/staleCandles` → `STALE` rows |
| API failures (as inputs) | futures fetch failed (`scan/futuresMissing`, `attachPricing/noFutures`), futures without LTP, crossed/one-sided books, chain with < 2 pairs |
| Safety decisions | 15 gate cases (incl. exactly 75 pts, calendar ±, slope warn, all blocks); skew block; stale-leg parity spread block; wrong ATM hint; malformed chain block |
| Expiry day / session boundaries | `time/…` at 15:29 and 15:31 on expiry day; `attachPricing/expiryDayBeforeClose` (prices) vs `/expiryDayAfterClose` (refuses); `expiries/expiryDay` (nearest keeps today, nextAfter moves on); refresh windows at 5 times |
| Liquidity, LTP, bid/ask, IV | `fairPrice` (tight, exactly 5%, wide, crossed, one-sided, zero LTP); `assessDepth` × 6 books × 4 sizes; `screenChain` × 7; legs below intrinsic → IV null; deep-ITM no-IV leg excluded from the scan |

## Mismatches found and how each was resolved

None was hidden or tolerance-widened. Each was fixed in Python to match TypeScript.

| # | Where | TS behaviour | Python (before) | Resolution |
|---|---|---|---|---|
| M1 | `normalizeChain` with `"oc": "oops"` | `Object.entries` iterates characters → 4 empty strikes → `CHAIN_INSUFFICIENT_PAIRS` | treated as empty → `CHAIN_EMPTY` (a different safety finding) | Iterate strings like arrays |
| M2 | Expired-contract message | `String(-1.9e-6)` = `-0.000001902…` | `repr` = `-1.9…e-06` | Implemented ECMAScript `Number::toString` |
| M3 | `Math.round` | `Math.round(0.49999999999999994)` = 0 | `floor(x+0.5)` = 1 | Compare the exact fractional part |
| M4 | `isIsoDate('0000-01-01')` | valid | invalid: the civil-date algorithm double-adjusted negatives under Python floor division | Fixed both conversions; added JS's `Date.UTC` 0–99 → 1900+ rule |
| G1 | Coverage gap (not a mismatch) | A mutation `age > max` → `age >= max+1` survived | — | Added freshness-boundary fixtures; mutant now fails |

Avoided by design (each would have mismatched): compensated `sum()` (Python
≥ 3.12), `max(0, NaN)`, banker's rounding, `'%.nf'` ties, `{}` falsiness,
arrays as objects, `undefined` vs `null`, integer-key ordering, `Date.UTC`
overflow.

**Unresolved mismatches: none.**

## Test-suite checks

- `engine/tests/test_comparator.py`: the comparator fails on tolerance breaches,
  NaN vs number, missing keys, length, type, null vs undefined, and an error vs
  a value.
- **Mutation checks**: one-line edits to the real code, each run against the
  full parity suite:

| Mutation | Result |
|---|---|
| CDF constant changed in its 11th digit (≈6e-11 relative) | detected |
| CDF constant changed in its 15th digit (≈5e-15 relative) | not detected (sub-tolerance, expected) |
| theta /365 → /366 | detected |
| even-count median → upper element | detected |
| gate 75 → 76 pts | detected |
| gate 40 → 41 pts | detected |
| mid-spread 5% → 6% | detected |
| touch `<=` → `<` | detected |
| grade A 0.6 → 0.61 | detected |
| vendor IV `> 60` → `>= 60` | detected |
| `next_after` `>` → `>=` | detected |
| scan min touches 2 → 3 | detected |
| staleness `>` → `>= max+1` | detected (after G1) |

## Commands and actual results

| Command | Result |
|---|---|
| `cd server && npx vitest run` (before changes) | 267/267 PASS |
| `cd server && npx tsx scripts/export-parity-fixtures.ts` | 9 files, 246 cases |
| `cd engine && python3.11/3.12/3.13 -B tests/parity_report.py` | 36 tests OK on each; statistics above |
| `cd engine && python3.X -m unittest discover -s tests -t tests` | OK on 3.11, 3.12, 3.13 (40 tests incl. comparator) |
| `cd engine && python3.X -m unittest test_refresh_table` | OK 5/5 on each |
| `cd engine && python3.11 run_refresh.py` | exit 0 |
| `cd tools && python3.X -m unittest test_dhan_cloud_probe` | OK 17/17 on each |
| `cd server && npm run typecheck && npm run build` | PASS |
| `cd server && npx vitest run` (after) | **276/276 PASS** (+9 drift tests) |
| Drift test with one hand-edited fixture digit | FAIL (as intended), PASS after restore |

## Limits (stated, not hidden)

- `iso_of` formats years 0000–9999 only; JS uses `±YYYYYY` outside that range.
  Unreachable: every date input is validated as 4 digits.
- `js_str` covers numbers, strings, booleans and null. `String(object)` is not
  emulated; the only reachable case is a malformed `security_id` object.
- 21-Sep futures LTP and all security ids, OI, volume and depth in fixtures are
  **synthetic** (labelled in `builder.ts`). Real market data: the GM 18-Sep
  chain prices, the 21-Sep leg quotes, and the 18/21-Sep 5-minute SENSEX candles.
- Parity is against the TS implementation. It proves the port reproduces the
  approved calculations, **not** that any signal or table is profitable.
