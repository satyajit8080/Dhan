# Phase 2 — Verification of Dhan Cloud, Authentication and Automation Questions

Date: 9 Oct 2026 · Branch: `claude/eloquent-carson-o3l0po` · Builds on
[PHASE1_AUDIT.md](PHASE1_AUDIT.md).
**No deployment, no strategy-rule changes, no orders.**

## Evidence grades

Every claim below carries one of these grades.

| Grade | Meaning | Strength |
|---|---|---|
| **OFFICIAL-CODE** | Read in Dhan's own source: `dhan-oss/DhanHQ-py` @ `8c6583e` (8 Oct 2026, SDK v2.3.0), `dhan-oss/dhanhq-skills` @ `6b43f98` (30 Jun 2026). Cloned and read in full | Strongest |
| **OFFICIAL-DOC** | Dhan docs (`dhanhq.co/docs/v2`, `docs.dhanhq.co`) or Dhan support FAQ (`dhan.co/support`), read through search-engine excerpts | Strong, but excerpts may be stale; page age noted where known |
| **COMMUNITY** | MadeForTrade, Dhan's own developer forum, posts by users. Not Dhan policy | Indicative only |
| **THIRD-PARTY** | Vendor or independent sites | Weak |
| **REPO** | This repo's RULES.md / earlier notes, source unknown | Unverified |
| **TESTED** | Executed in this session | Strong for what was run |
| **UNVERIFIED** | No adequate source found | — |

### Access limitation (unchanged from Phase 1)
This session's network policy still blocks `dhanhq.co`, `docs.dhanhq.co`,
`developer.dhanhq.co`, `cloud.dhanhq.co`, `dhan.co`, `madefortrade.in` and
`images.dhan.co` (proxy 403). GitHub *was* reachable, so both official repos were
cloned and read directly. **The Dhan Cloud product pages themselves still could not
be read.** Dhan Cloud items below rest on official-doc excerpts plus community
reports. Each one has a concrete verification step in §9. Allowing these domains
in the environment's network settings, or running `tools/dhan_cloud_probe.py`
inside Dhan Cloud, closes most of the gaps.

**Key fact:** neither official repository mentions Dhan Cloud at all
(`grep -ri "dhan cloud\|cloud.dhan"`: zero hits). No official code describes the
Dhan Cloud runtime.

---

## 1. Dhan Cloud: runtime, execution, scheduling, persistence, logging, secrets, deployment

| Question | Finding | Grade |
|---|---|---|
| What it is | Managed runtime to "create, version and deploy strategies… no servers to babysit"; a browser workspace where an AI agent can write the Python script | OFFICIAL-DOC ([docs.dhanhq.co/cloud](https://docs.dhanhq.co/cloud/), [developer.dhanhq.co](https://developer.dhanhq.co/)) |
| Language / runtime | Python. **Python 3.11** per a Dhan Cloud traceback (`/usr/local/lib/python3.11/site-packages/dhanhq/__init__.py`). RULES.md also says 3.11 | COMMUNITY ([thread](https://madefortrade.in/t/getting-error-cannot-import-name-dhancontext-from-dhanhq-in-dhan-cloud/94529)) + REPO. **Exact patch version UNVERIFIED** |
| Execution model | "Scheduled, event-driven and on-demand runs with full logs and one-click replay." A June 2026 run log shows each run **installing requirements, then executing `/tmp/script.py`** | OFFICIAL-DOC (docs home) + COMMUNITY ([thread](https://madefortrade.in/t/dhan-cloud-problem/91400)) |
| Resources | Sample container: 1 vCPU, 2 GB, Python. RULES.md says 1 vCPU / 3 GB | OFFICIAL-DOC (sample, illustrative) + REPO (conflicting memory size) |
| Scheduling | Sample: weekdays, start 09:20 IST, **auto-stop 15:30 IST**, run time 6h10m | OFFICIAL-DOC ([cloud.dhanhq.co/strategies](https://cloud.dhanhq.co/strategies), illustrative). Holiday handling UNVERIFIED |
| Dependencies | `requirements.txt` in the sample project. Packages are checked against an **"approved package list"** ("contact support to request a new package"). The platform reportedly **prepends its own base pins** (one report: `numpy==2.5.1` unsatisfiable) | OFFICIAL-DOC (file present in sample) + COMMUNITY ([scanner](https://madefortrade.in/t/dhan-cloud-tradehull-codebase-setup/91640), [numpy pin](https://madefortrade.in/t/dhan-cloud-python-deployment-failure-platform-injected-numpy-2-5-1-dependency-pin-is-unsatisfiable-blocking-global-stocks-order-execution/94464)) |
| Code scanner | Saving code that calls `place_order` raises a **HIGH security violation** and the save is blocked. `os.getenv()` and `os.path.exists()` reported flagged. RULES.md adds: no file writes, subprocess, port binding or Excel libs; split files over ~200 lines | COMMUNITY ([static-IP thread](https://madefortrade.in/t/static-ip-dhan-cloud-is-not-working/91100), [Tradehull forum](https://thforum.tradehull.com/)) + REPO. **Rule list UNVERIFIED** |
| Persistence | No evidence of persistent storage. Fresh install plus `/tmp/script.py` per run suggests an **ephemeral container per run**. "Global Variables" exist (FAQ titles: what, where, when updated, how to use); semantics unread | COMMUNITY (inference) + OFFICIAL-DOC (titles only). **UNVERIFIED** |
| Logging | "Full logs and one-click replay"; "all logs, infra and compute is managed". Retention and export UNVERIFIED | OFFICIAL-DOC |
| Secrets / config | An **"Env Variables"** section in the strategy UI (guide: `CLIENT_CODE`, `TOKEN`). One third-party guide reads them with `os.environ.get(...)`, but `os.getenv` is reportedly flagged. RULES.md: "credentials go in `{{VAR}}` Variables" (template placeholders) | COMMUNITY + THIRD-PARTY ([tradehull.com](https://tradehull.com/deploy-your-first-trading-bot/)) + REPO. **Access mechanism UNVERIFIED** |
| Monitoring / alerts | Docs have a "Monitoring & alerts" page; contents unread | OFFICIAL-DOC (title only) |
| Deployment process | Browser workspace: upload or paste files, save (scanner runs), set Env Variables, run or schedule. "One-click deploy" | OFFICIAL-DOC (marketing) + COMMUNITY (step list) |
| GitHub import / sync / deploy API | **No evidence anywhere**: not in either official repo, docs excerpts, or forum results | **UNVERIFIED; treat as unsupported** |
| Static IP of Dhan Cloud egress | Unknown. The forum thread is about the order-code scanner block, not IP | UNVERIFIED |
| Order rate in Dhan Cloud | "Same 10 orders/second limit applies" | COMMUNITY ([thread](https://madefortrade.in/t/does-orders-generated-via-colocated-dhan-cloud-have-rate-limits/91498)), irrelevant while read-only |
| Price | **Pay-per-use**, by resources consumed; estimator in the Developer Portal; rates not published in any retrievable page | OFFICIAL-DOC ([FAQ, ~52 days old](https://dhan.co/support/platforms/dhanhq-api/what-are-the-charges-for-deploying-my-algo-on-dhan-cloud/)) |

**Conclusion.** The runtime is Python (very likely 3.11) and runs scheduled,
logged, auto-stopping jobs in an apparently ephemeral container, behind a code and
package scanner. File, env and storage rules, the token mechanism and any GitHub
path are **not verifiable from published material**. The probe and checklist in §9
settle them in under an hour of console time.

---

## 2. Access-token lifecycle and unattended renewal

| Fact | Evidence | Grade |
|---|---|---|
| Tokens are valid **24 h**; Dhan attributes this to exchange/SEBI API-access guidelines (v2.4) | [Authentication](https://dhanhq.co/docs/v2/authentication/), [Releases](https://dhanhq.co/docs/v2/releases/) | OFFICIAL-DOC |
| **PIN + TOTP generation without a browser**: `POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=&pin=&totp=`, requires TOTP enabled on the account; response carries `expiryTime` (24 h) | `DhanLogin.generate_token(pin, totp)` in `src/dhanhq/auth.py`; README "Method 2"; docs excerpt | **OFFICIAL-CODE** + OFFICIAL-DOC |
| **RenewToken**: `GET https://api.dhan.co/v2/RenewToken`, headers `access-token`, `dhanClientId`; new 24 h token, old one expired | `DhanLogin.renew_token()` (SDK uses **GET**; docs excerpt says POST, so the method is inconsistent) | OFFICIAL-CODE + OFFICIAL-DOC |
| Renewal works **only on active tokens**, and only for tokens **generated from Dhan Web** | Docs excerpt. The SDK docstring says "expired or expiring", contradicting the docs | OFFICIAL-DOC vs OFFICIAL-CODE (conflict) |
| API key + secret (valid 12 months) → OAuth consent → **browser login every day** → `consumeApp-consent` | `generate_login_session()` opens a browser; `consume_token_id()` | OFFICIAL-CODE |
| Profile check: `GET /v2/profile` → `tokenValidity`, `dataPlan`, `dataValidity` | `DhanLogin.user_profile()`; skills SKILL.md | OFFICIAL-CODE |
| Expired-token errors: `807` (Data API), `DH-901` (Trading API) | dhanhq-skills `references/error-codes.md` | OFFICIAL-CODE (docs in repo) |
| PIN/TOTP endpoint returned **HTTP 500** for one user (May 2026); daily generation cap unknown | [forum](https://madefortrade.in/t/errorfailed-to-generate-token-via-pin-totp/89678) | COMMUNITY |

### Can renewal run unattended?

| Path | Unattended? | Conditions / risk |
|---|---|---|
| **A. PIN + TOTP** (`generate_token`) | **Yes, technically.** Officially supported by the SDK; no browser | Code must hold the **PIN and the TOTP base32 secret**. Together these are full login credentials: anyone who reads them can also log in and trade. Needs a secret store you trust (Dhan Cloud Env Variables if confirmed private). TOTP code generation needs `pyotp` or a stdlib HMAC-SHA1 implementation (~15 lines) if `pyotp` is not on the approved list |
| **B. RenewToken chaining** | **Fragile.** Works only while the token never lapses and (per docs) only for web-generated tokens. One missed run (weekend, outage, holiday) means a human must log in again | Also needs a place to keep the latest token between runs. With no persistent storage (§1), each run cannot see the previous run's renewed token. **Not viable on an ephemeral runtime** |
| **C. API key OAuth** | **No.** Requires a daily browser login | — |
| **D. Manual daily paste** into Env Variables | No, one human step per day | Simplest and safest; what the current plugin does |
| **E. Dhan Cloud injects a token for the connected account** | **UNVERIFIED.** No evidence found; community guides all set `TOKEN` or PIN/TOTP manually | Check in console (§9) |

**Recommendation for the read-only scanner:** start with **D** (paste each morning).
Move to **A** only after you accept the credential-storage risk in writing and
confirm that Env Variables are encrypted and private. Do not build on **B**.

---

## 3. Can the existing Python engine run as a read-only scheduled scanner on Dhan Cloud?

**TESTED on Python 3.11.17:** `engine/refresh_table.py` imports only the standard
library. `python3.11 -m unittest test_refresh_table` gives 5/5 OK, and
`run_refresh.py` reproduces the saved table. Language-wise, it will run on Dhan
Cloud's reported runtime.

**But it cannot run alone.** `build_refresh_table()` is a pure function whose
inputs come from the TypeScript MCP server, which cannot run on Dhan Cloud:

| Input to `build_refresh_table` | Produced today by | Available in Python? |
|---|---|---|
| `forward`, `T`, `df` | `pricingBridge.ts` + `pricing/forward.ts`, `time.ts` | **No**: needs a port |
| `legs[].iv_pct` (server Black-76 IV), bid/ask/ltp | `pricing/black76.ts` (`b76IV`) + chain normalisation | Black-76 *price* exists in Python; **IV inversion does not** |
| `resistances`, `supports` (touches, sources), `atr` | `levels.ts`, `indicators.ts`, `structure.ts` | **No** (`commodity_engine.py` has a *different* MCX method; reusing it would change the strategy) |
| `candle_ref_price`, `candles_ms`, `snapshot_ms` | Dhan REST via MCP | **No** data adapter in Python |
| Gate decision (publish / blank table) | `pricing/gate.ts` + skew rule | **No** |
| ATM±2 strike selection | SKILL.md (the LLM picks the rows) | **No**: must be code |

**Answer:** yes, as a read-only scheduled scanner, **after** the minimum port in §10.
As-is, no: it has no data source and no level/IV inputs.

Runtime fit (assuming the REPORTED rules hold): stdlib-only, no file writes, no
subprocess, no ports, files under 200 lines. Also: `dhanhq` 2.3.0 installs and
imports on 3.11 (TESTED), but it pulls in `pandas` and `numpy`
(OFFICIAL-CODE `setup.py`). Given the reported numpy-pin conflict, the scanner
should call the REST API with `urllib` or `requests`. It should not depend on the
SDK.

---

## 4. Resolving SENSEX instruments and futures dynamically

| Item | How to resolve at runtime | Grade |
|---|---|---|
| Underlying | `51` / `IDX_I` (stable index id) | OFFICIAL-CODE (skills quick-reference) |
| Option expiries | `POST /v2/optionchain/expirylist {UnderlyingScrip:51, UnderlyingSeg:"IDX_I"}`, already live in the TS code | OFFICIAL-CODE (SDK `expiry_list`) |
| Option legs (CE/PE security ids) | Returned inside `/v2/optionchain` per strike (`security_id`), already used | OFFICIAL-CODE (skills option-chain ref) |
| **Futures contract** (replaces the hard-coded SEP/OCT/NOV list) | Download the instrument list for **`BSE_FNO` only** via `GET https://api.dhan.co/v2/instrument/BSE_FNO` (CSV, one segment, parse in memory, no file write). Fall back to the full `api-scrip-master.csv`. Filter: exchange `BSE`, instrument `FUTIDX`, underlying symbol `SENSEX`, expiry ≥ option expiry; take the earliest | OFFICIAL-DOC ([Instrument List](https://dhanhq.co/docs/v2/instruments/)) + OFFICIAL-CODE (CSV URLs in `_security.py`; column names in skills `instruments.md`) |
| Lot size | `SEM_LOT_UNITS` from the same rows, instead of the constant 20 | OFFICIAL-CODE (skills: "use lot size from the security master, not from stale constants") |
| Column names | Official references (Apr–Jun 2026) use `SEM_SMST_SECURITY_ID`, `SEM_EXM_EXCH_ID`, `SEM_INSTRUMENT_NAME`, `SEM_EXPIRY_DATE`, `SEM_LOT_UNITS`, `SEM_CUSTOM_SYMBOL`. The detailed CSV / segment API uses "detailed tags" with a mapping table. Our `scripmaster.ts` claims a Sept 2026 rename | OFFICIAL-CODE / OFFICIAL-DOC. **The current header is UNVERIFIED** (CSV host blocked): match both name sets, as `scripmaster.ts` already does |
| Refresh cadence | Once per run (daily). Derivative ids change per expiry | OFFICIAL-CODE ("Resolve derivative IDs fresh") |
| Auth for the instrument endpoint | Unconfirmed whether `/v2/instrument/...` needs a token. The CDN CSV needs none | UNVERIFIED (test once) |

**Safety rule to keep:** if no future with expiry ≥ the option expiry is found,
the gate must **block** (as today), never fall back to the index LTP.

---

## 5. How GitHub updates could reach Dhan Cloud

| Path | Exists? | Grade |
|---|---|---|
| Native GitHub connect / auto-sync / webhook | **No evidence.** Absent from official repos, docs excerpts and forum results | UNVERIFIED (treat as unsupported) |
| Dhan Cloud deployment API or CLI | No evidence | UNVERIFIED |
| "Version" strategies inside Dhan Cloud | Docs: "create, **version** and deploy" (in-platform versioning) | OFFICIAL-DOC |
| Manual: copy files from a GitHub release into the browser workspace | Works with what is documented | OFFICIAL-DOC (upload/paste flow) |

**Supported workflow today: semi-automated.**
1. GitHub Actions on every push: unit tests, TS↔Python golden parity, a lint that
   rejects `os.`/`open(`/`subprocess` and > 200-line files (mirroring the REPORTED
   scanner rules), then build a release zip.
2. A person pastes or uploads the tagged release into Dhan Cloud and saves; the
   scanner runs on save.
3. The strategy logs `version=<git tag>` on start, so every log line traces to a commit.

---

## 6. The missing CE/PE signal thresholds: decisions only you can make

Nothing here is invented. SKILL.md §4 says the server "computes everything" but
"what is NOT configured is the threshold set… how far above the level counts as a
break, what ADX counts as trending, what PCR counts as bullish, how many OI strikes
must confirm". Until then it **must** return `NO SIGNAL`. RULES.md §6 also gives
rule *shapes* without numbers. The table lists every decision the code needs. None
were changed.

| # | Decision required | Where the rule is stated | What is already fixed |
|---|---|---|---|
| D1 | **Break distance**: how far a 1-minute close must be beyond `breakoutAbove` / `breakdownBelow` to count (points, ATR fraction, or just the existing +5 buffer) | SKILL §4; RULES §6 "1-min close through a level" | Trigger = level ± 5, rounded to 5 (RULES §4) |
| D2 | **"Next bar holds"**: close stays beyond the level? low/high stays beyond? with what tolerance? | RULES §6 | — |
| D3 | **Volume confirmation source**: the SENSEX index reports no volume. Which series (which futures contract) supplies "volume > 10-bar average", and does the 10 bars include the breakout bar? | RULES §6; `indicators.ts` vwapNote | 10-bar average named |
| D4 | **Trend filter**: is ADX used at all? If so, the threshold, the period (code default 14), the timeframe (1-min/5-min), and whether +DI > −DI is required for CE (and the reverse for PE) | SKILL §4 | Indicators computed, no thresholds |
| D5 | **PCR**: OI-PCR or volume-PCR; the strike range counted (whole chain or ±N strikes); bullish and bearish cut-offs; neutral band | SKILL §4 | PCR computed over the whole chain |
| D6 | **OI confirmation**: how many strikes, within which band around ATM, which buildup types count (e.g. put short-buildup = bullish?), minimum OI change | SKILL §4; SKILL §1 example "put writing at…, call unwinding at…" | Buildup classified per leg |
| D7 | **Combination logic**: must D1–D6 all pass (AND), or is it a score with a cut-off? What if CE and PE both qualify? | SKILL §1 rule 6 (one signal only) | — |
| D8 | **Rejection scalp**: what counts as a "wick rejecting a level" (wick beyond, close back inside? minimum wick size?) and the tolerance | RULES §6 | "2 consecutive 1-min wicks" |
| D9 | **Level quality for signals**: may a `WEAK_LEVEL` (< 2 touches) or a tier-2/tier-3 level (rolling history / exchange range) trigger a *signal*, or only be displayed? 1-min vs 5-min levels when they differ? | RULES §4–5; SKILL §0 | Display rules exist |
| D10 | **"No entry within 3 pts of a level"**: measured from which level (the trigger, or the next level), and how it interacts with the 5-pt buffer | RULES §6 | 3 pts named |
| D11 | **Theta filter**: "theta over 10 min < 1% of premium". Calendar minutes (theta/day × 10/1440) or trading minutes (× 10/375)? | RULES §6 | Theta is per calendar day |
| D12 | **IV filter**: definition of "chain ATM IV" (CE IV, PE IV, or their mean at `atmStrike`) | RULES §6 | +1 pt named |
| D13 | **Strike choice**: confirm the code defaults as your rules: delta band 0.35–0.60, minimum grade B, max round-trip 1.2%, lots used for grading | `strikeSelect.ts` DEFAULTS; SKILL §3 Gate C ("A or B") | Defaults exist in code but are not in RULES |
| D14 | **Signal lifetime and de-duplication**: how long a signal stays valid, when the same level may fire again, max signals per day for an alert-only scanner (the "3 losing scalps" rule needs fills, which a scanner doesn't have) | RULES §6 (risk) | — |
| D15 | **Risk rule data**: "max loss/trade 1% of available margin" needs the funds API (account data). Is the scanner allowed to read funds, or is this rule out of scope for read-only? | RULES §6 | — |

Until D1–D8 are answered, the automated scanner can publish the **refresh table**
(levels plus projected premiums: fully specified) but **no CE/PE signal**.

---

## 7. Subscriptions, static IP, permissions, cost

| Requirement | Needed for the read-only scanner? | Detail | Grade |
|---|---|---|---|
| **Data API plan** | **Yes**: option chain, quotes, candles, live feed | ₹499 + GST per month, auto-debited every 30 days; free-with-trades promo discontinued. Absent ⇒ `806` / `DH-902` | OFFICIAL-DOC ([FAQ](https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/), some pages ~600 days old; [trading-apis page](https://dhanhq.co/trading-apis) lists Data ₹499, Trading ₹0) + OFFICIAL-CODE (skills: "Data APIs require an active data plan") |
| Trading API | No (no orders) | Free | OFFICIAL-DOC |
| **Static IP** | **No** | Required **only** for order place/modify/cancel, super and forever orders; primary + secondary IP; 7-day edit lock; error `DH-911` (some support pages say `DH-905 Invalid IP`) | OFFICIAL-CODE (skills SKILL.md, orders.md; SDK `set_ip/modify_ip/get_ip`) + OFFICIAL-DOC |
| TOTP enabled on account | Only for path A (§2) | Required by `generateAccessToken` | OFFICIAL-DOC + OFFICIAL-CODE |
| Account segments | BSE F&O data visibility; `activeSegment` in `/profile` | Check `dataPlan`, `dataValidity`, `activeSegment` | OFFICIAL-CODE |
| Dhan Cloud compute | Yes, if hosted there | Pay-per-use; rates only in the portal estimator | OFFICIAL-DOC |
| Rate limits | Yes | Data 5/s & 100 000/day; Quote 1/s; Option chain 1 unique / 3 s; Non-trading 20/s. A 60 s scan (~4–5 calls) uses ~1,900 calls/day | OFFICIAL-CODE (skills error-codes.md, SKILL.md) |
| Exchange algo registration (SEBI retail-algo framework) | No for an alert-only scanner (applies to order-placing algos) | Not researched further, since no orders | UNVERIFIED (out of scope) |

---

## 8. Verified capability matrix

**N** = native to Dhan Cloud · **I** = via documented API · **X** = needs external
infrastructure · **U** = unverified · **—** = not required (read-only).

| # | Capability | Class | Evidence (grade) | Status / how to close |
|---|---|---|---|---|
| 1 | Python runtime | N | 3.11 traceback (COMMUNITY); engine passes on 3.11 (TESTED) | Probe prints exact version |
| 2 | Start on trading days | N (weekday schedule) + U (holidays) | Sample schedule (OFFICIAL-DOC) | Code skips non-trading days: expiry list + no-data check |
| 3 | Live market data | I | REST endpoints (OFFICIAL-CODE); needs Data plan | Probe shows api.dhan.co reachable |
| 4 | Compute levels / IV / table | I (after port) | Engine TESTED on 3.11 | §10 port |
| 5 | No duplicate signals | U | No verified persistence | Read "Global Variables" docs; else de-dup per run only |
| 6 | Orders | — | Scanner blocks `place_order` (COMMUNITY); RULES forbid | Not planned |
| 7 | Positions / risk controls | — | APIs exist (OFFICIAL-CODE) | Not planned |
| 8 | Network/API error handling | I | Error families (OFFICIAL-CODE skills); TS fixes in Phase 1 | Port with the engine |
| 9 | Auth renewal unattended | I (PIN+TOTP) with credential risk; B fragile | `generate_token` (OFFICIAL-CODE) | Your decision (§2) + console check for native token |
| 10 | Safe restart | N? | Ephemeral runs (COMMUNITY); engine stateless (code) | Probe a killed run |
| 11 | Logs | N | "Full logs and replay" (OFFICIAL-DOC) | Retention UNVERIFIED |
| 12 | Session-boundary stop | N + code | Auto-stop 15:30 sample (OFFICIAL-DOC); skip windows (TESTED) | — |
| 13 | Secrets | N? | Env Variables UI (COMMUNITY); `os.getenv` reportedly flagged | Probe in console (§9) |
| 14 | Instrument resolution | I | `/v2/instrument/BSE_FNO`, CSV (OFFICIAL-DOC/CODE) | Header names to confirm |
| 15 | GitHub → Dhan Cloud | X / U | No evidence of sync | Manual paste of CI release |
| 16 | Alerts only when needed | X / U | Outbound HTTP REPORTED; "Monitoring & alerts" page unread | Probe outbound to Telegram |
| 17 | Persistence across runs | U | None documented | Global Variables docs |
| 18 | Cost | N (pay-per-use) | FAQ (OFFICIAL-DOC); rates unknown | Portal estimator |

---

## 9. How to verify every remaining unknown (≈ 1 hour in the console, no orders)

| # | Unknown | Exact step | Expected evidence |
|---|---|---|---|
| V1 | Python version, clock/timezone, outbound reach, live log streaming, auto-stop | Paste `tools/dhan_cloud_probe.py` as a strategy; **Run now**; then schedule it Mon–Fri 15:20 so auto-stop at 15:30 cuts it | `PROBE python=…`, `local_clock` vs `ist`, reachability lines, last heartbeat before stop |
| V2 | Scanner rules | Save four one-line variants separately: `import os; os.environ.get("X")`, `open("/tmp/x","w")`, `import subprocess`, a 250-line file | Accepted / rejected message for each |
| V3 | Approved packages | `requirements.txt` with `requests`, then `pyotp`, then `dhanhq==2.3.0` | Which are rejected; whether the numpy pin breaks the install |
| V4 | Secret access | Create Env Variable `PROBE_X`; read it the way the console docs show (`os.environ`, `{{PROBE_X}}`, or a provided API) and print only its length | Length printed ⇒ mechanism confirmed |
| V5 | Persistence | Run twice; first run writes to Global Variables if writable (or `/tmp`), second run reads | Value present / absent |
| V6 | Native token | Look for a "connected account" token in the strategy settings or Global Variables; read the "Global Variables" FAQ | Documented or not |
| V7 | GitHub / deploy API | Developer Portal → strategy settings → any "Git", "Import" or "API key for deployments" | Present / absent |
| V8 | Instrument CSV header | One local run: `curl https://api.dhan.co/v2/instrument/BSE_FNO -H "access-token: …" \| head -1` | Header row → pin column names |
| V9 | RenewToken method | One call with `GET` (as the SDK) on a web-generated token | 200 vs 405 |
| V10 | Cost | Developer Portal cost estimator for 1 vCPU, Mon–Fri 09:15–15:30 | ₹/month |
| V11 | Docs access for this agent | Allow `dhanhq.co`, `docs.dhanhq.co`, `developer.dhanhq.co`, `dhan.co`, `madefortrade.in` under the environment's network settings | Lets the next session read the pages directly |

---

## 10. Minimum changes to run the existing engine automatically, read-only

Scope: reproduce the **existing** `refresh` table on a schedule, alert-only.
No signals (they need §6), no orders, no rule changes. Everything is a
**port of existing, tested logic**, checked by golden parity tests against the
TypeScript output.

| # | Change | Lines (est.) | Why it is the minimum |
|---|---|---|---|
| M1 | `engine/dhan_rest.py`: stdlib `urllib` POST client for `/optionchain/expirylist`, `/optionchain`, `/marketfeed/quote`, `/charts/intraday`, and the instrument list. Rate classes as documented; Phase-1 error families (807 auth, 806 plan, 805 retry) | ~150 | No data source exists in Python |
| M2 | `engine/forward_iv.py`: port `parityForward` (median within ±1.5% band), `fairPrice`, `b76IV` bisection, `T`/`DF`, and the gate (40-pt spread, 75-pt / carry band, 3 s skew) | ~180 | `build_refresh_table` needs `forward`, `T`, `df`, `iv_pct`; the gate decides blank vs table |
| M3 | `engine/levels.py`: port `deriveLevels` + `atr`, `sessions`, `openingRange`, `sessionVwap`, `findSwingPoints` (5-min bars, spot = index LTP) | ~200 (split in two files) | `build_refresh_table` needs resistances/supports with touches and sources |
| M4 | `engine/instruments.py`: resolve SENSEX future (≥ option expiry) and lot size from `BSE_FNO` instrument rows; block if none | ~70 | Removes the 26 Nov 2026 cliff |
| M5 | `engine/scan.py`: one scan = expiries → chain → futures (≤ 3 s skew) → 5-min candles → levels → ATM±2 legs → `build_refresh_table` → print table + `version=<tag>` to the log | ~80 | Replaces the LLM's orchestration |
| M6 | `engine/main.py`: loop with `time.sleep(60)`, exits at 15:30 IST; credentials via whatever V4 confirms; on auth/plan error print one clear line and idle | ~50 | Dhan Cloud entry point |
| M7 | Golden tests: TS server and Python engine on the same fixtures (`server/test/fixtures.ts` GM chain), identical to 1e-6 | ~120 | Proves the port preserved the strategy |

Not included on purpose:
- The rolling-history fallback (needs persistence, V5).
- INDmoney candles (a Claude-only connector).
- Alerts (§11 phase C, after V1 confirms outbound HTTP).
- Signals (§6).
- Orders.

Total ≈ 850 lines of Python ported from ~2,000 lines of TypeScript, because only
the refresh path is needed (no liquidity stage 2, analytics or strike ranking).

---

## 11. Proposed deployment plan (separate from the changes; NOT executed)

**Phase A — Verify (no code on Dhan Cloud except the probe)**
1. Allow the Dhan domains for this agent (V11) **or** run V1–V10 yourself and paste the outputs back.
2. Confirm the Data API plan is active (`/profile` → `dataPlan`, `dataValidity`).
3. Decide the token path (§2: D or A) and record the decision.

**Phase B — Build and prove locally (in this repo)**
1. Implement M1–M7 on a branch; all tests and golden parity green in GitHub Actions.
2. Run `engine/main.py` locally against live data for 3 sessions, in parallel with
   `/sensex-snapshot` + `refresh`; tables must match row for row.

**Phase C — Dhan Cloud dry run (read-only, logs only)**
1. Upload the tagged release (CI zip) to a new strategy; set Env Variables per V4;
   schedule Mon–Fri 09:15 start, 15:30 auto-stop.
2. Two weeks of log review: every scan logged, gate blocks explained, no crashes,
   cost in the portal estimate.
3. Then, optionally, add alerts (Telegram, if V1 shows outbound HTTP), only for:
   auth/plan failures, a futures-resolution failure, gate blocked > N minutes,
   and crash loops.

**Updating:** GitHub `main` → tag → CI zip → manual upload → the first log line
shows the tag. Roll back by re-uploading the previous tag. In-platform
versioning (OFFICIAL-DOC) can hold the history.

**Recovery:** each run is stateless. Restart = rerun. Retryable API errors back
off in place. Auth/plan errors log once and idle. The futures-resolution gap
blocks the gate (no fallback to index LTP).

**Signals and orders:** only after you answer §6 (signals). Orders would also
need an explicit request, static IP, and a separate executor, which the Dhan
Cloud scanner reportedly blocks in the same file. RULES.md §1 currently forbids
orders.

---

## Sources

Official code (cloned, read in full)
- dhan-oss/DhanHQ-py @ 8c6583e: `src/dhanhq/auth.py`, `_security.py`, `_option_chain.py`, `_historical_data.py`, `setup.py`, `README.md`, `examples/auth_example.py` — <https://github.com/dhan-oss/DhanHQ-py>
- dhan-oss/dhanhq-skills @ 6b43f98: `skills/dhanhq/SKILL.md`, `references/*.md`, `scripts/resolve_security.py` — <https://github.com/dhan-oss/dhanhq-skills>

Official docs and FAQ (via search excerpts)
- Dhan Cloud docs: <https://docs.dhanhq.co/cloud/> · Developer console: <https://developer.dhanhq.co/> · Strategies sample: <https://cloud.dhanhq.co/strategies>
- Authentication: <https://dhanhq.co/docs/v2/authentication/> · Releases: <https://dhanhq.co/docs/v2/releases/> · Instrument List: <https://dhanhq.co/docs/v2/instruments/> · Python SDK guide: <https://docs.dhanhq.co/api/v2/guides/sdks/python>
- Dhan Cloud charges: <https://dhan.co/support/platforms/dhanhq-api/what-are-the-charges-for-deploying-my-algo-on-dhan-cloud/>
- Data API subscription: <https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/> · Trading/Data API pricing: <https://dhanhq.co/trading-apis>

Community (MadeForTrade, Dhan's forum)
- Python 3.11 traceback: <https://madefortrade.in/t/getting-error-cannot-import-name-dhancontext-from-dhanhq-in-dhan-cloud/94529>
- Per-run install / `/tmp/script.py`: <https://madefortrade.in/t/dhan-cloud-problem/91400>
- Approved package list, Env Variables: <https://madefortrade.in/t/dhan-cloud-tradehull-codebase-setup/91640>
- Injected numpy pin: <https://madefortrade.in/t/dhan-cloud-python-deployment-failure-platform-injected-numpy-2-5-1-dependency-pin-is-unsatisfiable-blocking-global-stocks-order-execution/94464>
- `place_order` HIGH violation: <https://madefortrade.in/t/static-ip-dhan-cloud-is-not-working/91100>
- Order rate in Dhan Cloud: <https://madefortrade.in/t/does-orders-generated-via-colocated-dhan-cloud-have-rate-limits/91498>
- PIN/TOTP 500 error: <https://madefortrade.in/t/errorfailed-to-generate-token-via-pin-totp/89678>

Third-party
- Tradehull deploy guide (env var names): <https://tradehull.com/deploy-your-first-trading-bot/>
