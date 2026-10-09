# Phase 3 — Dhan Cloud Verification and Read-Only Deployment Readiness

Date: 9 Oct 2026 · Branch `claude/eloquent-carson-o3l0po` (base `cd23505`) ·
Statuses: **PASS**, **FAIL**, **UNVERIFIED**, **BLOCKED**.
Nothing was deployed. No order code was added. No strategy rule was changed.

## 1. Executive summary

- **Official documentation is still inaccessible.** This environment's network
  policy returns 403 for every Dhan domain, so I could not read the Dhan Cloud
  pages or the v2 API pages directly. **BLOCKED**
- Official Dhan **source code** was read directly: `dhan-oss/DhanHQ-py` @
  `8c6583e` (8 Oct 2026, v2.3.0) and `dhan-oss/dhanhq-skills` @ `6b43f98`.
- **Dhan Cloud compatibility** stays UNVERIFIED. Community reports say Python
  3.11, a package/code scanner and per-run containers; none is official. The
  probe was rebuilt into two scripts with 17 tests and a 16-step manual
  checklist that settles each question.
- **Authentication.** A new, tested finding: the official SDK's
  `DhanLogin.generate_token()` **writes the PIN and TOTP code in plain text to
  the log** when a network error occurs (§5). PIN+TOTP must not run through the
  SDK as-is. Manual daily token remains the only approach without an unresolved
  risk.
- **Instruments.** Expiries and option-leg ids are already resolved live.
  Futures and lot size are still hard-coded; the registry runs out on
  **26 Nov 2026**. The dynamic source exists, but its column names and value
  formats are UNVERIFIED. One new defect was fixed: a malformed expiry entry
  (e.g. `"N/A"`) could be selected as the expiry.
- **Read-only safety.** The guard let 5 account-changing endpoints from the
  official SDK through (`/pnlExit`, `/ip/setIP`, `/ip/modifyIP`, `/RenewToken`,
  `/globalstocks/*`). It was tightened and now refuses all 21 order/account paths enumerated from the SDK
  (tested).
- **Strategy.** CE/PE signals remain disabled by design; 15 decisions are
  documented in `PHASE3_STRATEGY_GAPS.md`.
- **Tests:** 267/267 TypeScript, 5/5 engine, 17/17 probe; Python 3.11, 3.12, 3.13.
- **Decision: conditional GO** for porting the pure computation to Python;
  **NO-GO** for Cloud deployment, automated authentication and dynamic futures
  resolution until the checklist results come back (§10).

## 2. Files inspected and modified

**Inspected:**
- Repo: `docs/PHASE1_AUDIT.md`, `docs/PHASE2_VERIFICATION.md`, RULES.md,
  SKILL.md, `engine/*.py`, `tools/dhan_cloud_probe.py`, `server/src/**`
  (`transport.ts`, `instruments/*`, `digest.ts`, `client.ts`, `mcpServer.ts`).
- DhanHQ-py: `auth.py`, `dhan_http.py`, `_security.py`, `marketfeed.py`,
  `setup.py`, and every endpoint string in `src/dhanhq/*.py`.
- dhanhq-skills: `SKILL.md`, `references/*.md`, `scripts/dhan_helpers.py`.

**Modified / added (this phase):**

| File | Change | Why |
|---|---|---|
| `server/src/transport.ts` | +4 forbidden path fragments | Close 5 account-mutation gaps (§7) |
| `server/src/instruments/expiries.ts` | `isIsoDate` filter on the expiry list | Malformed entry selected as expiry (§6) |
| `server/src/client.ts`, `server/src/mcpServer.ts` | Text only | Claimed "NO trigger level" while trigger levels are returned |
| `plugin/server/mcpServer.cjs` | Rebuilt | Ships the above |
| `server/test/instruments.test.ts` (new) | 45 tests | Expiry day, missing contracts, malformed data, future dates, guard coverage |
| `tools/dhan_cloud_probe.py` | Rewritten | HEAD-only public URLs, timeouts, clock/timezone, scanner-safe wording |
| `tools/dhan_cloud_probe_extended.py` (new) | Env names, presence/length only; write test; persistence; packages | Questions the basic probe cannot answer |
| `tools/test_dhan_cloud_probe.py` (new) | 17 tests | Stdlib-only, no orders/credentials, no value leakage |
| `docs/PHASE3_*.md` (new) | Checklist, gaps, this report | — |

## 3. Verified facts vs assumptions

| Claim | Status | Evidence |
|---|---|---|
| SDK v2.3.0 requires Python ≥ 3.10 | PASS | `setup.py` `python_requires='>=3.10'` |
| dhanhq-skills says "Python 3.8+" | PASS (conflicts with SDK) | skills `SKILL.md` compatibility line |
| SDK 2.3.0 installs and imports on 3.11 | PASS | Installed in a 3.11 venv; pulls pandas 3.0.6, numpy 2.4.6 |
| PIN+TOTP endpoint `POST auth.dhan.co/app/generateAccessToken?dhanClientId&pin&totp` | PASS | `auth.py` |
| RenewToken is `GET /v2/RenewToken` with `access-token`, `dhanClientId` | PASS (SDK) / conflicts with docs excerpt (POST) | `auth.py` |
| Renew works only on active, web-generated tokens | UNVERIFIED (docs excerpt only) | SDK docstring says "expired or expiring" |
| SDK logs PIN/TOTP on network error | **PASS (tested)** | §5 |
| `fetch_security_list` writes a CSV to the working directory | PASS | `_security.py` |
| Segment instrument API `GET /v2/instrument/{segment}` | UNVERIFIED | Docs search excerpt only; not in the SDK |
| Compact CSV uses `SEM_*` columns | UNVERIFIED (official tooling, no fixture) | skills `instruments.md`, `dhan_helpers.py` |
| SENSEX = `51`/`IDX_I`; options and futures in `BSE_FNO` | PASS (official docs in repo) | skills quick-reference; SDK constants |
| Dhan Cloud runs Python 3.11 | UNVERIFIED (community) | MadeForTrade traceback |
| Scanner blocks `place_order`, `os.getenv`, unapproved packages | UNVERIFIED (community) | MadeForTrade / Tradehull forum |
| Per-run ephemeral container | UNVERIFIED (community inference) | Run log with `/tmp/script.py` |
| No GitHub → Dhan Cloud sync | UNVERIFIED (absence of evidence) | Zero mentions in official repos or search |
| Data API ₹499 + GST / month | UNVERIFIED (official FAQ excerpts, some ~600 days old) | dhan.co support |

**Corrections to earlier reports:**
- Phase 2 §4 said expiries were "already live" without noting the
  malformed-entry defect (fixed now).
- Phase 2 counted the guard as complete; it was not (fixed now).
- Phase 2 recommended PIN+TOTP "after accepting the risk" without knowing the
  SDK leaks the PIN to logs.

## 4. Cloud compatibility results

| Question | Status | How it will be settled |
|---|---|---|
| Python version | UNVERIFIED | Checklist C2 (basic probe) |
| Required packages installable | UNVERIFIED | C9a–d (install log + `pkg_*` lines) |
| Files persist between runs | UNVERIFIED | C8 (extended probe run twice) |
| Scheduled start / auto-stop | UNVERIFIED | C10, C11 (heartbeat timestamps) |
| Environment variables available | UNVERIFIED | C6 (presence + length of `PROBE_MARKER`) |
| Outbound to Dhan endpoints | UNVERIFIED | C4 (credential-free HEAD); C13 optional read-only `/profile` |
| Scanner rules | UNVERIFIED | C1, C5 messages |

**What the probes can and cannot do**, stated so nothing is over-claimed:
- The **basic** probe can establish Python version, platform, clock/timezone,
  credential-free reachability and live log streaming.
- The **extended** probe can establish env-variable presence/length, filesystem
  writability, persistence (only across two separate runs) and which packages
  are importable.
- **Neither** can install packages, configure or prove a schedule, or test
  authenticated APIs. Those need the manual steps in `PHASE3_CLOUD_CHECKLIST.md`.

Probe safety, verified by tests:
- Imports are standard-library only (AST check against `sys.stdlib_module_names`).
- No order, login, token or subprocess code.
- The basic probe sends `HEAD` with no headers or body, to public URLs, with a
  10 s timeout. Errors are reduced to their class name, never the message.
- The extended probe makes no network calls, never prints a variable's value,
  masks credential-like names, and deletes only files it created (one 20-byte
  persistence marker per writable directory is left on purpose).
- Local run here (Python 3.11.17): every Dhan host reported
  `UNREACHABLE URLError`, which reflects this sandbox's policy, not Dhan Cloud.
  The container clock was UTC (`local_is_ist=False`).

## 5. Authentication assessment

| | Manual daily token | RenewToken | PIN + TOTP |
|---|---|---|---|
| **Preconditions** | Generate on web.dhan.co (My Profile → Access DhanHQ APIs) | Token must still be **active**; docs: only tokens generated from Dhan Web (UNVERIFIED) | TOTP enabled on account; PIN; TOTP **base32 secret** to compute codes |
| **Expiry** | 24 h | New 24 h token; old one expired immediately | 24 h (`expiryTime` in response) |
| **Unattended?** | No: one human step per day | Only if it never lapses (weekends, holidays, outages break the chain) | Yes, technically |
| **Needs persistent storage?** | No (variable updated by hand) | **Yes**: the next run must see the newly issued token. Persistence UNVERIFIED (C8) | No (regenerate each run) |
| **Credential risk** | Lowest: a 24 h token only | Each renewed token must be stored somewhere writable, so the store becomes a long-lived secret | **Highest**: PIN + TOTP secret is a full login. **Tested: SDK `generate_token` logs `dhanClientId`, `pin` and `totp` in plain text on any request failure**, because `requests` puts the URL, query string included, in the exception that the SDK logs. Credentials in the query string can also reach any proxy or platform log |
| **Dhan Cloud compatible (evidence)?** | Probably: needs Env Variables (UNVERIFIED, C6) | **BLOCKED** until persistence is proven (C8) | UNVERIFIED: needs Env Variables (C6), `pyotp` or a stdlib TOTP (C9c), and a leak-free client |
| **Other risk** | — | — | Docs excerpt (consent flow): "only one token… at any given point". If that applies to PIN+TOTP, a cloud-generated token could **invalidate the token the local Claude plugin is using**. UNVERIFIED |

Recommendation (unchanged in kind, stronger in reason):
- Use the **manual daily token** for the read-only scanner.
- Do not implement unattended login. If it is ever approved, it must not use the
  SDK's `generate_token`. It would need its own client that never logs the URL
  or the exception text, and a written decision accepting full-credential storage.

## 6. Instrument-resolution assessment

| Item | Today | Dynamic source | Status |
|---|---|---|---|
| Option expiry dates | Live `/optionchain/expirylist`, cached 6 h, default = next weekly after today | Same | PASS (code, 9 new tests); malformed-entry defect **fixed** |
| Option contracts and security ids | From `/optionchain` per strike (`security_id`) | Same | PASS (design); live response shape UNVERIFIED here |
| Exchange segments | `IDX_I` (underlying), `BSE_FNO` (derivatives) | Official constants | PASS |
| Underlying id | `51` | Official quick-reference | PASS |
| **SENSEX futures** | Hard-coded SEP/OCT/NOV 2026; `futuresForExpiry` refuses with "Add the next contract" | Instrument list (`/v2/instrument/BSE_FNO` or the CSV) | **BLOCKED** after 26 Nov 2026 until resolved dynamically; source format UNVERIFIED (C16) |
| Future expiry dates | Hard-coded | Same rows | UNVERIFIED |
| Lot size | Hard-coded 20 | `SEM_LOT_UNITS` (official tooling) | UNVERIFIED |
| Security id of futures | Hard-coded 844615 / 864571 / 1100929 | Same rows | UNVERIFIED (cannot be checked from here) |

New tests (`server/test/instruments.test.ts`, synthetic data only):
- expiry-day behaviour for the futures and the options;
- front-month roll the day after expiry;
- the 26 Nov cliff raising a clear error;
- unordered future expiry dates;
- malformed expiry entries (`"N/A"`, `15-10-2026`, `2026-02-30`, numbers, `null`);
- a non-list response;
- scrip-master CSVs with changed headers, ragged rows, quoted commas, non-numeric
  lot size, HTTP 503, and an id resolving to the wrong symbol.

No real security ids, column values or lot sizes were guessed. Test ids are
`900001`-style placeholders.

Implementation is deliberately **not** done: resolving futures needs the real
header and value format (checklist C16). Writing a parser for guessed columns
would be the kind of guess this phase forbids.

## 7. Python dependency and portability assessment

`engine/refresh_table.py` imports `math`, `dataclasses`, `datetime`, `typing`:
standard library only. **PASS** on 3.11.17, 3.12, 3.13 (5/5 tests each;
`run_refresh.py` exit 0).

**Inputs it still receives from the TypeScript server**, through the Claude
model, which stitches tool outputs together:

| `build_refresh_table` input | Comes from today | Gap |
|---|---|---|
| `forward`, `T`, `df` | `get_market_snapshot` → `pricing.forward`, `T`, `discount_factor` | Needs Python port: parity forward, gate, skew rule |
| `legs[].iv_pct` | snapshot `legs_near_atm[].iv_pct` (server Black-76 IV) | Needs IV bisection port |
| `legs[].bid/ask` | **Not in `legs_near_atm`**; only in `liquidity.candidates` (≤ 12, screened) | ATM±2 legs outside the candidates fall back to LTP with zero spread. Python scanner should read bid/ask from the chain directly |
| `legs[].ltp` | `legs_near_atm[].ltp` | Chain normalisation port |
| ATM±2 selection | The model, per SKILL.md | Must become code (`atm_strike` ± 2 × strike step) |
| `resistances`, `supports` (touches, sources) | `compute_levels` on 5-min index candles | Level engine port |
| `atr`, `bar_minutes` | `compute_levels` (`atr14` of the series supplied) | Port; must be the 5-min ATR to match `bar_minutes=5` |
| `candle_ref_price` | Index LTP from the snapshot | Trivial once the chain is fetched |
| `snapshot_ms`, `candles_ms` | Snapshot `epoch_ms`, last candle time | Trivial |
| `now` | Caller | Use `datetime.now(timezone.utc)` (engine now converts to IST) |

**Port plan (not started).** Package `engine/sensex/`, each file < 200 lines,
standard library only, no file writes, no `os`:

| Module | Responsibility | Dhan API dependency | Tests |
|---|---|---|---|
| `dhan_rest.py` | `urllib` POST/GET; rate classes (option chain 1 per 3 s per key, quote 1/s, data 5/s); error families 805/806/807-810/811/DH-9xx; never logs URL, headers or exception text | `/optionchain/expirylist`, `/optionchain`, `/marketfeed/quote`, `/charts/intraday` | Stubbed responses mirroring `regressions.test.ts`; guard parity with `transport.ts` |
| `normalize.py` | Chain → legs (ltp, bid, ask, security_id); quote → ltp, depth, OHLC; vendor IV ignored | — | Port `guards.test.ts` normalisation cases |
| `pricing.py` | `T` (ACT/365 to 15:30 IST), DF, `fair_price`, per-strike forward, median in ±1.5% band, IV bisection (reuse `refresh_table.black76`) | — | **Golden parity**: TS `GM` fixture forward/IV to 1e-6 |
| `gate.py` | 40-pt spread, 75-pt / −5…+15% carry, 3 s skew; block ⇒ blank table | — | Port `pricing.golden.test.ts` gate cases |
| `levels.py` (+`indicators.py`) | `deriveLevels`, session VWAP, ATR (Wilder), swings, opening range | — | Port `levels.test.ts` + Phase 1 regressions |
| `instruments.py` | Expiry validation (`isIsoDate`), next-weekly rule; futures resolution **after C16** | `/optionchain/expirylist`; instrument list | Port `instruments.test.ts` |
| `scan.py` | One scan: expiries → chain → futures (≤ 3 s) → candles → levels → ATM±2 → `build_refresh_table` | All above | End-to-end with recorded fixtures |
| `main.py` | Loop + `sleep`; stop at 15:30 IST; token via the C6 mechanism; one log line per scan with version | — | Clock-injected unit tests |

Estimated ~850 lines of Python. Acceptance requires identical outputs to the
TypeScript server on shared fixtures.

## 8. Test commands and actual results

| Command | Result |
|---|---|
| `cd server && npm run typecheck` | PASS |
| `cd server && npm run build` | PASS |
| `cd server && npx vitest run` | **PASS: 267/267** (11 files; +45 new) |
| Same new `instruments.test.ts` against pre-change `src/` | **8 FAIL / 37 PASS**, as intended: the 8 cover the two defects fixed |
| `cd engine && python3.11/3.12/3.13 -m unittest test_refresh_table` | PASS 5/5 on each |
| `cd engine && python3.11 run_refresh.py` | PASS (exit 0) |
| `cd tools && python3.11/3.12/3.13 -m unittest test_dhan_cloud_probe` | PASS 17/17 on each |
| Basic probe, local run (1 heartbeat) | Ran; all Dhan hosts `UNREACHABLE` (sandbox policy) |
| Extended probe, local run | Ran; writable dirs, packages `NOT_INSTALLED` in system Python; markers cleaned up |
| Bundle smoke: MCP `initialize` + `tools/list` | PASS: 11 tools |
| SDK `generate_token` logging test (dummy values) | Leak **confirmed** |
| Any test against live Dhan or Dhan Cloud | **BLOCKED** (network policy) |

## 9. Remaining blockers

| # | Blocker | Status | Owner / next step |
|---|---|---|---|
| B1 | Dhan docs unreachable from this environment | BLOCKED | Allow the Dhan domains in environment network settings, or rely on checklist outputs |
| B2 | Dhan Cloud runtime facts (version, scanner, env, filesystem, persistence, schedule) | UNVERIFIED | You: checklist C1–C12 |
| B3 | Instrument list header and value formats | UNVERIFIED | You: C16 (one `curl`) |
| B4 | Futures registry ends 26 Nov 2026 | BLOCKED after that date | Dynamic resolution after B3; or add DEC manually with a verified id |
| B5 | Unattended authentication | BLOCKED (security: SDK leaks; persistence unknown) | Use the manual token; revisit only with a written risk decision |
| B6 | CE/PE thresholds D1–D15 | BLOCKED (owner decision) | You: `PHASE3_STRATEGY_GAPS.md` |
| B7 | Data API plan active | UNVERIFIED | You: C13 or web.dhan.co profile |
| B8 | Cloud cost | UNVERIFIED | You: C14 |
| B9 | Dev-only npm advisories (vitest/vite) | Known, not shipped | Separate upgrade |

## 10. Recommended next phase

**Phase 4A: Python port of the pure computation (GO, conditional).**
- Nothing in it depends on Dhan Cloud facts. The worst-case runtime (stdlib
  only, no files, no `os`) is the design target, and correctness is proven
  against the existing TypeScript outputs.
- Modules: `normalize`, `pricing`, `gate`, `levels`/`indicators`, `scan`
  (using recorded fixtures), and `dhan_rest` (tested with stubs only).
- **Excluded until evidence arrives:**
  - `instruments.py` futures resolution (needs C16);
  - `main.py` credential handling (needs C6);
  - any unattended login (B5);
  - any CE/PE logic (B6);
  - any deployment.

**In parallel, you:** run checklist C1–C16 and paste the outputs back; answer
D1–D8.

**Phase 4B (after the outputs):** fill the results table; implement futures
resolution against the real header; choose the token mechanism; then a
read-only, logs-only dry run on Dhan Cloud.
