# Phase 4 — Port Plan: TypeScript → Python function mapping

Branch `claude/eloquent-carson-o3l0po` (base `a9f7d23`). Read-only scope: no
I/O, no credentials, no orders, no Dhan Cloud, no CE/PE logic, no dynamic
futures lookup. `RULES.md`, `server/src/**` and `plugin/**` are unchanged.

## Layout

```
engine/
  refresh_table.py        existing approved engine — REUSED UNCHANGED by sensex.scan
  sensex/                 the port (stdlib only: math, re, decimal)
    jscompat.py           JS semantics needed for exact parity
    pricing.py            normal.ts, black76.ts, time.ts, forward.ts
    gate.py               pricing/gate.ts
    normalize.py          normalize.ts + endpoints/historical.ts toCandles
    integrity.py          integrity.ts
    pricing_bridge.py     pricingBridge.ts
    liquidity.py          liquidity.ts
    levels.py             levels.ts + indicators.ts / structure.ts helpers it uses
    expiries.py           instruments/expiries.ts (pure selection only)
    scan.py               refresh scan composed from the above + refresh_table.py
    errors.py             error classes mirroring errors.ts
  tests/                  parity + comparator tests (unittest, stdlib)
parity/fixtures/*.json    expected values, generated ONLY by TypeScript
server/test/parity/builder.ts        the generator (runs existing TS functions)
server/scripts/export-parity-fixtures.ts   writes the JSON
server/test/parity-fixtures.test.ts  fails if committed JSON ≠ fresh TS output
```

Output dicts use the TypeScript schema's camelCase keys (`types.ts`), so a
Python result and a TS result compare key by key.

## Function mapping

| # | Area | TypeScript (source) | Python | Status |
|---|---|---|---|---|
| 1 | Chain / quote parsing | `normalize.ts` `parseLastTradeTime`, `num` (internal), `normalizeQuote`, `normalizeChain`, `quarantineVendor` (internal), `makeProvenance` | `normalize.parse_last_trade_time`, `num`, `normalize_quote`, `normalize_chain`, `_quarantine_vendor`, `make_provenance` | PASS |
| 1 | Candle arrays | `endpoints/historical.ts` `toCandles` | `normalize.to_candles` | PASS |
| 2 | Normal CDF/PDF | `pricing/normal.ts` `normCdf`, `normPdf` (Hart/West) | `pricing.norm_cdf`, `norm_pdf` | PASS |
| 2 | Time | `pricing/time.ts` `expiryStampMs`, `yearFractionToExpiry`, `daysToExpiry`, `istToEpochMs` | `pricing.expiry_stamp_ms`, `year_fraction_to_expiry`, `days_to_expiry`, `ist_to_epoch_ms` | PASS |
| 2 | Black-76 | `pricing/black76.ts` `discountFactor`, `noArbBounds`, `b76Price`, `b76IV`, `b76Greeks` | `pricing.discount_factor`, `no_arb_bounds`, `b76_price`, `b76_iv`, `b76_greeks` | PASS |
| 2 | Parity forward | `pricing/forward.ts` `perStrikeForward`, `median`, `parityForward` (+ slope diagnostic) | `pricing.per_strike_forward`, `median`, `parity_forward` | PASS |
| 2 | Leg price / pricing | `pricingBridge.ts` `fairPrice`, `attachPricing` | `pricing_bridge.fair_price`, `attach_pricing` | PASS |
| 3 | Gate | `pricing/gate.ts` `checkGate`, `GATE_DEFAULTS` | `gate.check_gate`, `GATE_DEFAULTS` | PASS |
| 3 | Integrity | `integrity.ts` `checkQuote`, `checkChain`, `checkSnapshotSkew` | `integrity.check_quote`, `check_chain`, `check_snapshot_skew` | PASS |
| 3 | Liquidity | `liquidity.ts` `walkBook`, `assessDepth`, `screenChain`, `GRADE_THRESHOLDS` | `liquidity.walk_book`, `assess_depth`, `screen_chain` | PASS |
| 3 | Expiry selection | `instruments/expiries.ts` `isIsoDate`, `ExpiryCache.get` filter/sort, `nearest`, `nextAfter` | `expiries.is_iso_date`, `clean_expiries`, `nearest`, `next_after` | PASS (no cache/I/O) |
| 4 | Indicator helpers | `indicators.ts` `trueRanges`, `wilderSmooth`, `atr`, `vwap`, `sessionVwap`, `sessions`, `openingRange`, `istDateOf`, `istTimeOf` | `levels.*` same names in snake_case | PASS |
| 4 | Swings | `structure.ts` `findSwingPoints` | `levels.find_swing_points` | PASS |
| 4 | Levels | `levels.ts` `aggregateCandles`, `collectCandidates`, `countTouches`, `clusterCandidates` (internal), `deriveLevels`, `projectPremium`, `buildTradePlan` | `levels.aggregate_candles`, `collect_candidates`, `count_touches`, `_cluster`, `derive_levels`, `project_premium`, `build_trade_plan` | PASS |
| 5 | Refresh table | `engine/refresh_table.py` (already Python) | reused unchanged | PASS (existing tests) |
| 5 | Scan step | No TS function exists: SKILL.md tells the LLM to chain `get_market_snapshot` → candles → `compute_levels` → `refresh_table` | `scan.build_scan`, `scan.run_refresh` | PASS against a TS harness (`tsScan` in builder.ts) composed only from the TS functions above |

## JS semantics the port must reproduce (`jscompat.py`)

Each one produced, or would have produced, a real mismatch.

| JS behaviour | Naive Python | Helper |
|---|---|---|
| `Math.round` ties toward +∞; exact for 0.49999999999999994 | `round()` is banker's; `floor(x+0.5)` wrong at 0.49999999999999994 | `js_round` |
| `toFixed` exact-binary ties away from zero | `'%.2f'` half-even | `to_fixed` |
| `String(x)`: exponent only below 1e-6 or ≥ 1e21; `2` not `2.0` | `repr`/`str` differ | `js_str` (ECMAScript Number::toString) |
| `Number(str)`: `""`→0, hex/octal/binary, rejects `nan`, `inf`, `1_000` | `float()` accepts `nan`, `inf`, `1_000` | `js_number` |
| `[].reduce((a,b)=>a+b, 0)` plain summation | `sum()` uses compensated summation since 3.12 | `js_sum` |
| `Math.max(0, NaN)` is NaN | `max(0, nan)` is 0 | `js_max`, `js_min` |
| `Object.keys` order: array-index keys first, ascending | dict insertion order | `object_keys` |
| `Object.entries("str")` iterates characters | — | `normalize_chain` |
| `{}` and `[]` are truthy; arrays are `typeof 'object'` | `{}` falsy; list ≠ dict | `js_truthy`, `is_js_object`, `js_get` |
| `undefined` ≠ `null`; JSON drops undefined properties | single `None` | `UNDEFINED` sentinel |
| `Date.UTC` normalises overflow (31 Feb → 3 Mar); years 0–99 → 1900+ | `datetime` raises | `date_utc`, `civil_ms` |
| `toISOString` on epoch ms | — | `iso_of` (proleptic Gregorian, floor division) |
| Booleans are not numbers | `isinstance(True, int)` | `is_number` |

## Dependencies of the scan step

| Input | Source in production (future) | In this phase |
|---|---|---|
| Raw `/optionchain` `data` | Dhan REST (not ported: I/O) | Fixture JSON |
| Raw `/marketfeed/quote` for the futures contract | Dhan REST; contract id from the registry or a future dynamic lookup (BLOCKED) | Fixture JSON (synthetic quote) |
| 5-minute index candles | Dhan `/charts/intraday` arrays | Repo's real 18/21-Sep candles |
| Expiry | `expiries.next_after` on the live list | Fixed in fixture |
| Strikes for the table | **BLOCKED**, see below | Passed explicitly |
| `now` | Caller's clock | Passed explicitly |

## BLOCKED: not decided here, needs your decision

| # | Item | Why it is blocked | Effect today |
|---|---|---|---|
| P1 | **ATM±2 strike selection** | Exists only as prose in SKILL.md ("ATM±2 strikes (ATM from the parity forward)"). There is no TS code, and no rule for gaps in the strike ladder (±2 listed strikes vs ±200 points). | `build_scan` requires `strikes`. `atmStrike` is returned so a caller can apply the rule once it is defined |
| P2 | **Legs without a computable IV** (price outside no-arbitrage bounds) or missing from the chain | RULES.md §5 defines blank-table conditions but not a per-row "no IV" display | Such legs are left out of `legs` and listed in `excludedLegs` with the reason; no value is invented |
| P3 | **Dynamic futures lookup** | Phase 3: instrument-list format unverified | Not implemented; the caller supplies the futures quote and expiry |
| P4 | **CE/PE signals** | `PHASE3_STRATEGY_GAPS.md` D1–D15 | Not implemented |

## Not ported (out of Phase 4 scope)

- **I/O:** `transport.ts`, `ratelimit.ts`, `config.ts` token handling,
  `scripmaster.ts` and `ExpiryCache` caching. Credentials and network come later.
- **`get_analysis`-only analytics** not used by the refresh path: `analytics.ts`
  (PCR, max pain, buildup), `strikeSelect.ts`, `computeIndicators`
  (EMA/ADX/RSI), structure classification, `priceHistory.ts` (rolling-history
  tiers 2/3), `digest.ts`.

## Test plan (as executed)

1. The TS builder runs the existing functions on fixed inputs and writes
   `parity/fixtures/<module>.json` (246 cases).
2. A vitest drift test regenerates them in memory and requires equality.
3. Python `engine/tests/test_parity_*.py` compare every case with the
   documented tolerance (`docs/PHASE4_PARITY_RESULTS.md`).
4. `engine/tests/test_comparator.py` proves the comparator itself fails on
   differences.
5. Mutation checks: deliberate one-line changes to each module must make
   the parity tests fail.
