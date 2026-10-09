# Phase 1 — Audit, Bug Fixes and Dhan Cloud Automation Research

Date: 9 Oct 2026 · Branch: `claude/eloquent-carson-o3l0po` · Scope: audit and fixes only.
Nothing was deployed, no order code was added, no real orders were placed.

**How to read the evidence labels**

| Label | Meaning |
|---|---|
| **VERIFIED (code)** | Observed by running code or tests in this repo |
| **VERIFIED (official)** | From Dhan's own published material: `dhan-oss/dhanhq-skills` reference docs (Dhan's official GitHub org), the DhanHQ docs homepage / support pages as returned by search |
| **REPORTED** | Present in this repo's own notes (RULES.md) or a secondary source, not confirmed against a primary Dhan source |
| **UNKNOWN** | Could not be verified |

**Research limitation.** This session's network policy blocked direct access to
`developer.dhanhq.co`, `docs.dhanhq.co` and `dhanhq.co` (proxy HTTP 403). API
facts were taken from Dhan's official `dhan-oss/dhanhq-skills` references (fetched
from `raw.githubusercontent.com`), the official `dhanhq` SDK on PyPI, and search
excerpts of Dhan's docs and support pages. **Dhan Cloud's runtime pages could not
be read**, so most Dhan Cloud specifics below are marked UNKNOWN. Re-run section E
with those pages open before Phase 2.

---

## A. Existing architecture and code flow

The repository has four parts. **None of them runs by itself on a schedule.**
Every scan is triggered by a person typing in a Claude chat.

```
 Claude chat (human types "refresh" / asks for a signal)
   │  skill: plugin/skills/sensex-scalp/SKILL.md   ← decision + presentation rules (LLM)
   │  commands: /dhan-token, /sensex-snapshot
   ▼
 MCP server (Node 20, stdio)  server/src → bundled to plugin/server/mcpServer.cjs
   │  11 read-only tools; order paths refused in transport.ts
   ▼
 Dhan REST v2  (/optionchain, /optionchain/expirylist, /marketfeed/quote,
                /charts/intraday, /charts/historical) + public scrip-master CSV

 engine/refresh_table.py  ← pure Python, no I/O; the LLM feeds it MCP outputs
 engine/commodity_engine.py ← separate MCX engine (not SENSEX)
```

### `get_market_snapshot` (snapshot.ts): the core, in strict order
1. **Expiry**: live `/optionchain/expirylist` (cached 6 h). Never hard-coded.
2. **Option chain first**: `/optionchain` for SENSEX (`UnderlyingScrip 51`, `IDX_I`).
3. **Futures immediately after**: `/marketfeed/quote` for the registered SENSEX
   future that expires on/after the option expiry (`registry.futuresForExpiry`).
4. **Single-timestamp rule**: chain and futures receipt times must be ≤ 3000 ms apart.
5. **Parity forward + Black-76 + gate**: pricingBridge.ts (formulas in §D).
6. **Liquidity**: stage 1 screens chain top-of-book; stage 2 is one `/marketfeed/quote`
   call for ≤ N candidates, walking the 5-level book at the requested size.

### `get_analysis` (client.ts)
Snapshot → chain positioning (PCR, max pain, buildup, OI concentration) →
1-minute candles (5 days) with indicators → rolling-history record →
**levels** (3 tiers: real candles → synthetic candles from the rolling snapshot
history → exchange day high/low) → strike ranking for CE and PE → trade plans.
It returns **no direction**: the skill returns `NO SIGNAL` because the direction
thresholds are not configured (RULES.md §6, SKILL.md §4).

### `refresh` (SKILL.md §0 + engine/refresh_table.py)
Snapshot → 5-minute index candles (Dhan, or INDmoney via another Claude connector)
→ `compute_levels` → `build_refresh_table` → a 10-row table: ATM±2, CE and PE, with
the option's projected premium at the trigger level.

### What exists vs what doesn't
| Exists | Does not exist |
|---|---|
| Read-only market data, pricing, gate, levels, liquidity, strike ranking | Any scheduler, loop, or unattended entry point |
| Token from chat / env / file | Automatic token generation or renewal |
| Rolling history persisted to a temp file | Signal store, de-duplication, execution history |
| Logs to stderr, token-redacted | Alerts / notifications |
| — | **Order placement**: forbidden by RULES.md §1 and blocked in code |
| — | **Direction thresholds**: the strategy never emits CE/PE by design today |

---

## B. Bugs discovered, root causes and fixes

All fixes are VERIFIED (code). Each has a regression test that **fails on the
original code and passes after the fix** (shown in §C).

| # | Severity | Defect | Root cause | Fix (files) |
|---|---|---|---|---|
| 1 | High (build) | `npm ci` fails with `EUSAGE`; a clean checkout cannot install | `package.json` declares `esbuild ^0.24.0`, but the lock file had no top-level esbuild entry | Lock resynced; only esbuild entries changed (`server/package-lock.json`) |
| 2 | High (reliability) | One network blip (DNS, connection reset, TLS) fails the whole call with no retry | `fetch()` rejects with `TypeError('fetch failed')`; the catch only retried `AbortError`/typed errors and re-threw everything else | Network faults wrapped as retryable `TransportError` (`transport.ts`) |
| 3 | High (ops) | Wrong error classification: **811** (invalid expiry) reported as bad credentials; **806 / DH-902** (no data plan) told the user to refresh the token; **807** (token expired) not recognised; **805** (too many requests) not backed off; **800 / DH-908 / DH-909** not retried | Code table didn't match the DhanHQ v2 annexure | Code families rebuilt from the annexure; plan errors get their own message and checked before HTTP 401/403 (`transport.ts`) |
| 4 | Medium (calc) | "Session VWAP" was a **5-day** VWAP. It is a *structural* level (exempt from touch confirmation) and feeds `candleIndicators.vwap` | `vwap()` sums every bar passed; `get_analysis`/`get_candles` pass 5 days | `sessionVwap()` uses the latest IST session only; used by levels and indicators (`indicators.ts`, `levels.ts`) |
| 5 | High (calc) | Trade-plan **stop/target premiums wrong** by `delta × (trigger − spot)`. Example: spot 74,756, trigger 74,800, support 74,690, Δ 0.5, premium 125. Old stop premium **70**; correct ≈ **92**. A premium stop at 70 fires far too late | Greeks measured at spot, but the expansion was centred on the trigger while starting from today's premium | Expansion centred on spot; added `triggerPremium` (`levels.ts`) |
| 6 | High (calc) | `get_analysis` with `series:"futures"` places levels against the **index** LTP while the bars are **futures** prices. On 18 Sep the gap was 321 pts, so every level classed as resistance and there was no support | One `spotForLevels` (index LTP) for every series; candles also used the front-month contract, not the snapshot's | Spot matches the bar series (futures LTP for futures bars); candles use the snapshot's contract; adds `levelPriceSpace` (`client.ts`) |
| 7 | High (calc) | Tier-2 rolling-history levels: **every synthetic index bar's high/low was replaced by the futures *session* high/low**. That manufactured a "confirmed" level with one touch per bar, in futures space. Tier-3 reference levels also compared futures day range with the index LTP | Session-wide extreme applied to each bar; no futures→index basis conversion | Widen a bar only when a **new** day extreme appeared since the previous scan, converted by the observed basis; reference levels converted to index space (`priceHistory.ts`) |
| 8 | Medium (risk) | A leg with **no depth assessment** passed the liquidity floor (default B). It could become `best` and get a trade plan when every assessed leg graded C/F. That contradicts SKILL.md Gate C | Floor only checked `if (depth present)` | Missing depth now means "liquidity unverified" → ineligible (`strikeSelect.ts`) |
| 9 | Medium (logic) | Default expiry on **expiry day** was today's contract. RULES.md §6 says *next weekly on expiry day*, and after 15:30 IST the snapshot threw (T ≤ 0) instead of serving the next weekly | `nearest()` uses `>= today` | `nextAfter()` (`> today`) for the snapshot default; explicit `expiry` still honoured (`expiries.ts`, `snapshot.ts`) |
| 10 | Medium (logic) | Python `refresh` no-trade filter allowed **after 15:30** and **before 09:15**. A tz-aware UTC datetime shifted the windows by 5h30m | Windows ended at 15:30; no pre-open window; `.time()` taken in the caller's zone | Windows cover the closed market; tz-aware input converted to IST (`engine/refresh_table.py`) |
| 11 | Low (docs) | SKILL.md called `DH-906` "Invalid Token" (it's Order Error). The `get_analysis` interval said default 5 (it is 1). Expiry descriptions didn't match the new default | Drift | Corrected (`SKILL.md`, `mcpServer.ts`) |
| 12 | Low (packaging) | README references `.claude-plugin/marketplace.json` and `server/.env.example`; the plugin has no manifest or `.mcp.json`. All dot-files appear lost in the GitHub web upload. README implied the server reads `.env` (it doesn't) | Upload dropped dot-files | **Reconstructed** `plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json`, `.claude-plugin/marketplace.json`, `server/.env.example` (no secrets). These are reconstructions, so verify them before a marketplace install |
| 13 | High (supply chain) | `@modelcontextprotocol/sdk` 1.30.0 (shipped in the bundle) had a high-severity advisory (OAuth client credential redirect); `source-map-js` high | Outdated deps | `npm audit fix` (non-breaking): SDK → 1.32.1. Bundle rebuilt |

### Found but intentionally NOT changed

| Item | Why left | Recommendation |
|---|---|---|
| Futures registry is hard-coded (SEP/OCT/NOV 2026). After **26 Nov 2026** every snapshot blocks (no future for the cross-check) | Adding DEC needs its securityId from the scrip master, which can't be verified from here | **Automation blocker**: resolve futures from the scrip master at runtime (Phase 2) |
| Levels use 5 days of 1-min bars (swings and touches across days); RULES.md §4 says "today's candles" | Strategy decision, not a provable bug | Owner to decide |
| Option-chain fields `previous_oi` / `previous_close_price` are relied on for OI buildup. Dhan's official reference shows `oi_change` and does not list `previous_*` | Can't verify without a live call | Check one live response; if absent, buildup silently reads `unknown` |
| Remaining `npm audit`: vitest, vite, tinypool, esbuild (critical/high/moderate) | Dev/test tooling only, not in the shipped bundle; fixes need major-version bumps | Upgrade vitest 2→4 in a separate change |
| `commodity_engine.py` (MCX): rejects the nearest weak level instead of trying the next stronger one, unlike the SENSEX engine; no tests | Out of SENSEX scope | Separate task |
| `PriceHistory` writes a temp file | Fine locally; conflicts with the REPORTED "no file writes" Dhan Cloud rule | Handle in the Python port |
| Option-chain limiter serialises all chain calls globally (docs allow concurrent unique requests) | Conservative, not wrong | — |
| `scripts/smoke.ts` | Needs a live token; not run | Run once with a real token |

---

## C. Tests executed and actual results

| Check | Before | After |
|---|---|---|
| `npm ci` | **FAIL** — `EUSAGE: lock file's esbuild@0.28.2 does not satisfy esbuild@0.24.2` | OK |
| `npm test` (vitest) | 204 / 204 pass (after a manual `npm install`) | **222 / 222 pass** (10 files) |
| `npm run typecheck` / `npm run build` | clean | clean |
| `npm run bundle` → MCP `initialize` + `tools/list` + `dhan_token_status` over stdio | not run | 11 tools listed, status call answered |
| New `test/regressions.test.ts` against the **original** source | — | **17 / 17 FAIL** (proves they catch the defects; a few also fail because they call helpers the fix adds) |
| Same tests against fixed source | — | 17 / 17 pass |
| `python3 engine/run_refresh.py` | OK | OK (output unchanged) |
| New `engine/test_refresh_table.py` (stdlib `unittest`) on original / fixed | — | 3 of 5 FAIL / **5 / 5 pass** |
| `npm audit` | 8 advisories incl. high in the shipped MCP SDK | 6 remaining, all dev-only tooling |

Two pre-existing tests in `priceHistory.test.ts` asserted the buggy behaviour of
defect 7 (a futures day range applied with no futures price and no prior scan).
They were rewritten to the corrected semantics, and one new case was added.

Not tested: anything that needs a live token or the network (smoke script, real
Dhan responses), and any Dhan Cloud behaviour.

---

## D. Official Dhan API mapping for the existing SENSEX logic

### Instruments
| Item | Value | Status |
|---|---|---|
| SENSEX underlying | `securityId 51`, segment `IDX_I` | VERIFIED (official: dhan-oss reference table) |
| SENSEX options/futures segment | `BSE_FNO` (feed code 8) | VERIFIED (official) |
| Lot size 20 | from a margin check (RULES.md) | REPORTED; lot sizes change, so resolve from the scrip master |
| Futures ids 844615 / 864571 / 1100929 | hard-coded | REPORTED |
| NIFTY 13 / BANKNIFTY 25 (`IDX_I`) | the code marks these UNVERIFIED | VERIFIED (official reference table lists 13 and 25) |

### Endpoints used
| Logic | Endpoint | Rate limit | Status |
|---|---|---|---|
| Expiries | `POST /v2/optionchain/expirylist` `{UnderlyingScrip, UnderlyingSeg}` | option-chain class | VERIFIED (official) |
| Chain (index LTP, per-strike CE/PE LTP, bid/ask, OI, volume, vendor IV/Greeks) | `POST /v2/optionchain` `{UnderlyingScrip, UnderlyingSeg, Expiry}`; `data.oc` keyed by strike string | 1 unique request / 3 s | VERIFIED (official) |
| Futures cross-check, Stage-2 depth | `POST /v2/marketfeed/quote` `{SEGMENT: [ids]}`; 5-level `depth.buy/sell`, `last_trade_time`, `ohlc` | 1 req/s, ≤ 1000 instruments | VERIFIED (official) |
| Intraday candles | `POST /v2/charts/intraday`; interval 1/5/15/25/60; `"YYYY-MM-DD HH:MM:SS"`; epoch-second timestamps | Data: 5/s, 100 000/day | VERIFIED (official) |
| Daily candles | `POST /v2/charts/historical`; `expiryCode`, `oi` | Data class | VERIFIED (official) |
| Scrip master | `images.dhan.co/api-scrip-master-detailed.csv` (code says columns renamed Sept 2026) | none | URL REPORTED; column rename UNKNOWN |
| **Live WebSocket feed** (not used today) | `MarketFeed` v2: 5 sockets/user, 5000 instruments/socket, 100 per subscribe message; Full packet has LTP, OI, OHLC and 5-level depth | — | VERIFIED (official) |
| Token check | `GET /profile` (`tokenValidity`, `dataPlan`, `dataValidity`) | non-trading class | VERIFIED (official) |

### Every calculation, with formula, inputs and output
| Calculation | Formula / rule | Inputs (timeframe) | Output | Depends on |
|---|---|---|---|---|
| Fair leg price | mid `(b+a)/2` if two-sided, uncrossed and spread ≤ 5% of mid, else LTP | chain top-of-book (snapshot) | price per leg | market data |
| Time to expiry | `T = (expiry 15:30 IST − snapshot time) / 365 d` | expiry, chain receipt time | years | independent |
| Discount factor | `DF = e^(−rT)`, r = 6.5% (config) | r, T | DF | independent |
| Per-strike forward | `F_K = K + (C − P)/DF` | paired fair prices | F_K | market data |
| ATM hint | strike with min `|C − P|` | paired legs | strike | market data |
| Forward | **median** of `F_K` within ±1.5% of the ATM hint | per-strike F_K | F | market data |
| Gate (any block ⇒ publish nothing) | per-strike spread ≤ 40 pts; same-expiry future \|Fut − F\| ≤ 75 pts, else annualised carry `ln(Fut/F)/Δt` in −5%…+15%; chain/futures skew ≤ 3000 ms | F, futures LTP, receipt times | pass/block + reasons | market data |
| IV | bisection of Black-76 on fair price; null outside no-arb bounds | price, F, K, T, r | σ | calc |
| Greeks (Black-76) | Δ = DF·N(d1) (put −DF·N(−d1)); Γ = DF·φ(d1)/(F σ√T); vega/100; θ = (rV − DF F φ(d1) σ/(2√T))/365; ρ = −TV/100 | F, K, T, σ, r | per leg | calc |
| Liquidity | walk 5-level book for `lots × 20`; round-trip `(VWAP_buy − VWAP_sell)/mid`; grade A ≤ 0.6%, B ≤ 1.2%, C ≤ 2.5%, else F; incomplete fill ⇒ F | quote depth | grade | market data |
| PCR / max pain / buildup / OI peaks | ΣPE OI / ΣCE OI; strike minimising writer payout; sign(ΔP)×sign(ΔOI) | chain | positioning | market data |
| Indicators | EMA 9/20/50/200 (SMA-seeded); Wilder ATR14, ADX/±DI 14, RSI14; **session** VWAP; ROC 5/15/60; relative volume | candles (1-min default, 5 days) | values (no verdict) | market data |
| Levels | candidates (fractal swings ±2, session/prev-session H/L, 15-min opening range, session VWAP, last-20-bar range) → cluster within tol = max(0.03%·spot, 0.25·ATR, 2) → touches = bars whose high (res) / low (sup) is within tol → non-structural needs ≥ 2 touches → nearest above/below spot → trigger = level ± 5, rounded to 5 | candles + spot in the **same price space** | `breakoutAbove`, `breakdownBelow`, ladder | market data |
| Strike ranking | weighted score: delta band 0.35–0.60, liquidity grade, round-trip, OI, theta burn, volume; reject no IV, grade < B, **no depth**, round-trip > 1.2% | priced legs, depth, positioning | ranked candidates | calc |
| Trade plan | target = next level (else ±1 ATR); stop = opposite level; premiums `V0 + Δ(x−spot) + ½Γ(x−spot)²` | levels, best strike | entry/trigger/target/stop | calc |
| Refresh premium (Python) | trigger in forward space `F_t = trigger + (F − candle_ref)`; time to trigger = clamp(\|distance\|/ATR × bar, 5, 60) min; Black-76 reprice at `F_t`, `T − mins`; target = reprice − ½ spread | snapshot + levels + ATR(5-min) | 10-row table | calc |
| Direction | **not configured**, so always `NO SIGNAL` | — | — | human decision pending |

External services: Dhan REST (required); INDmoney via another Claude connector
(optional candle fallback; **not available outside Claude**); the Claude LLM
itself (it applies SKILL.md's rules and formatting).

---

## E. Dhan Cloud compatibility assessment

### What is known
| Topic | Finding | Status |
|---|---|---|
| What it is | Managed runtime to create, version and deploy Python strategies; browser workspace with an AI agent that writes scripts | VERIFIED (official, docs homepage / developer console) |
| Runs | "Scheduled, event-driven and on-demand runs with full logs and one-click replay" | VERIFIED (official docs homepage text) |
| Sample project | `main.py, strategy.py, dhan_client.py, config.py, requirements.txt, .env.example`; 1 vCPU / 2 GB; weekdays 09:20 start, 15:30 IST auto-stop | VERIFIED (official), but illustrative, not a spec |
| Pricing | Pay-per-use by resources consumed; rates not found | Model VERIFIED (support page), rates UNKNOWN |
| Global Variables | Exist (FAQ titles); semantics not retrieved | UNKNOWN |
| Python version | RULES.md says 3.11 only | REPORTED |
| Restrictions | RULES.md: 1 vCPU/3 GB; continuous loop with sleep; **no `os.getenv` / `os.path` / file writes / subprocess / Excel libs / port binding**; pinned deps; no persistent storage; AI code scanner blocks on any flag; split files over ~200 lines; outbound HTTP allowed | REPORTED (no primary source found) |
| GitHub integration / auto-sync | No evidence found | UNKNOWN. **Assume none.** |
| Restart policy, alert channels, log retention, execution-time limits | Not found | UNKNOWN |
| Auth inside Dhan Cloud (does the runtime inject a token?) | "Connected to your Dhan account", mechanism unknown | UNKNOWN |
| Egress static IP (needed only for orders) | Not found | UNKNOWN |

### Verdict per component
| Component | Can it run on Dhan Cloud as-is? | Why |
|---|---|---|
| MCP server (TypeScript/Node) | **No** | Python runtime only (all evidence); it's a stdio server that needs an LLM client; it writes a temp file |
| Claude plugin / skill (LLM rules, chat output) | **No** | Needs Claude; it isn't code |
| `engine/refresh_table.py` | **Yes, likely** | Pure stdlib Python, no I/O |
| Snapshot, parity forward, Black-76, gate, levels, liquidity | **Needs adaptation** | Must be ported to Python (about 2,000 lines of logic; the tests define the expected numbers) |
| INDmoney candle fallback | **No** | It's a Claude connector |

**Overall: needs adaptation.** The strategy engine can move to Dhan Cloud once
ported to Python. The interactive Claude tooling stays where it is.

---

## F. Proposed unattended-automation architecture

```
 GitHub (source of truth) ──CI: tests, golden fixtures, build a single-folder bundle──┐
                                                                                      │ manual paste/upload
                                                                                      ▼ (no verified auto-sync)
 Dhan Cloud strategy  (Python, scheduled Mon–Fri 09:15–15:30 IST, auto-stop)
   main.py ── loop every N s (sleep), respects rate budget
     ├─ session guard: trading day? inside 09:25–11:30 / 13:00–14:50? (holiday list from the expiry/market calendar)
     ├─ auth: token from Dhan Cloud account binding or Variables; /profile check; 807/DH-901 ⇒ ALERT + idle
     ├─ data: expirylist → optionchain → futures quote (same order and skew rule as today)
     │        candles via /charts/intraday (or a MarketFeed WebSocket for live LTP)
     ├─ engine (ported 1:1 from server/src + refresh_table.py): forward, gate, Black-76, levels, liquidity, refresh table
     ├─ signal de-dup: key = (date, side, strike, trigger); store = Global Variables if writable, else in-memory + alert-side de-dup
     ├─ output: stdout JSON logs (Dhan Cloud logs) + outbound alert (e.g. Telegram) ONLY on: new qualifying setup,
     │          gate blocked > K min, auth/plan failure, futures-registry gap, crash loop
     └─ orders: NONE (RULES.md §1). Phase 4+ only, behind an explicit flag, after paper → sandbox → 1-lot
```

Design rules carried over unchanged: chain first, ≤ 3 s skew, no index-LTP spot,
gate blocks publish nothing, vendor IV quarantined, `NO SIGNAL` until the owner
supplies direction thresholds.

### Token strategy (the biggest unattended-operation risk)
| Option | Unattended? | Status |
|---|---|---|
| Web-generated token (24 h) | No: daily manual step | VERIFIED (official) |
| `RenewToken` before expiry (another 24 h; web-generated, still-active tokens only) | Yes while it never lapses; one missed renewal needs a human | VERIFIED (official) |
| API key + secret (12 months) consent flow | No: daily browser login | VERIFIED (official) |
| TOTP-based token generation via API (v2.5) | Possibly fully unattended | Feature VERIFIED (release notes); endpoint details UNKNOWN |
| Dhan Cloud account binding | Possibly native | UNKNOWN |

---

## G. Capability matrix

Legend: **N** native to Dhan Cloud · **I** possible via documented integration ·
**X** needs external infrastructure · **U** unsupported / not yet verified.

| # | Capability | Class | Notes |
|---|---|---|---|
| 1 | Start on trading days | **N** (schedule shown) + **U** (holiday calendar) | Weekday schedule is shown in the sample; exchange holidays need our own check |
| 2 | Fetch live market data | **I** | REST verified; WebSocket verified for the API (unverified *inside* Dhan Cloud). Needs the Data API plan |
| 3 | Update indicators / evaluate logic | **I** (after Python port) | Pure computation |
| 4 | Process signals without duplicates | **U** | Needs persistence; Global Variables semantics unknown; in-memory resets on restart |
| 5 | Execute orders automatically | **U / not required** | Existing strategy has no orders and RULES.md forbids them; also needs static IP + whitelisting (VERIFIED requirement), unknown for Dhan Cloud egress |
| 6 | Order status, positions, risk controls | **U / not required** | APIs exist (order book, positions, kill switch: VERIFIED official); out of scope |
| 7 | Network/API errors, expired auth | **I** (errors) / **U** (auth renewal) | Retry + error families now correct in the TS code; to be ported |
| 8 | Safe restart after failure | **U** | Restart policy unknown; the engine is stateless per scan, so restart is safe except for de-dup state |
| 9 | Logs, execution history, errors | **N** (logs, replay per docs) / **U** (retention) | History beyond logs needs storage (unknown) or an external store (X) |
| 10 | Stop at session boundary | **N** (auto-stop shown) | Also enforced in code by the no-trade windows |
| 11 | Update deployed code when GitHub changes | **X / U** | No verified GitHub sync. CI builds; deploy step is manual unless Dhan exposes an API |
| 12 | Alert only when action is needed | **X** (likely) | Outbound HTTP to Telegram is REPORTED allowed; Dhan "Monitoring & alerts" page exists, contents UNKNOWN |

**Human action still required today:** daily token (until TOTP or renewal is
proven); deploying code updates; adding futures contracts (until scrip-master
resolution is built); **configuring the direction thresholds**; acting on any
signal (no orders by design).

---

## H. Remaining blockers, permissions, subscriptions, costs, limits

1. **Direction thresholds not configured.** Even fully automated, the engine can
   only output levels and `NO SIGNAL`. This is an owner decision.
2. **Token lifecycle.** 24 h validity (SEBI/exchange rule). Unattended operation
   needs TOTP generation or unbroken `RenewToken` chaining, both unproven here.
3. **Data API plan**: ₹499 + tax / month (Dhan support page; may be stale).
   Required for option chain, quotes, candles and the feed (`806` / `DH-902` otherwise).
4. **Dhan Cloud cost**: pay-per-use, rates unknown. A sample 6h10m/day 1 vCPU run is shown.
5. **Dhan Cloud runtime rules unverified** (Python version, scanner, file/env access,
   storage, restart, GitHub). The REPORTED "no file writes / no `os.getenv`" rules
   would rule out the current persistence and config approach.
6. **Futures registry ends 26 Nov 2026.** After that every snapshot blocks.
7. **Static IP** (orders only): mandatory per Dhan docs; primary + secondary IP,
   7-day edit lock. Not needed while read-only.
8. **Rate limits**: option chain 1 unique / 3 s; quotes 1/s; data 5/s and 100 k/day.
   The current scan (~4–5 calls) every 30–60 s fits comfortably.
9. **INDmoney fallback** is unavailable outside Claude; on Dhan Cloud, tiers 2/3
   (rolling history, exchange range) are the only fallbacks.
10. **This audit could not read Dhan Cloud's docs** (network policy).

---

## I. Future deployment plan (proposed, not executed)

**Phase 2 — Python engine (no deployment)**
1. Port `pricing/*`, `pricingBridge`, `integrity`, `levels`, `indicators`, `liquidity`,
   `strikeSelect`, `snapshot` to a stdlib-only Python package (≤ 200-line files if
   the scanner rule is real). Keep `refresh_table.py` as-is.
2. Cross-language golden tests: run the TS and Python engines on the same fixtures
   in CI (GitHub Actions) and require identical outputs to 1e-6.
3. Replace the hard-coded futures registry with scrip-master resolution plus a test
   that fails 14 days before the last registered contract expires.

**Phase 3 — Dhan Cloud dry run (read-only, alerts only)**
1. With the Dhan Cloud docs open, confirm each UNKNOWN in §E. Then pick the auth
   path (TOTP, renewal, or native binding) and the de-dup store.
2. Deploy the CI-built bundle manually; schedule Mon–Fri 09:15–15:30 IST, auto-stop.
3. Run 2–3 weeks logging every table and setup with timestamps (the RULES.md roadmap
   paper phase), comparing against the local MCP output.

**Keeping it updated:** GitHub `main` is the source of truth. A tag triggers CI:
tests, then golden parity, then a zip artifact plus a changelog. A person
uploads/pastes it to Dhan Cloud and records the deployed tag in a startup log
line (`version=<tag>`). Roll back by redeploying the previous tag.

**Failure recovery:** stateless per scan, so restart is the default recovery.
- Retryable errors (805/800/DH-904/908/909/network): backoff in place.
- Auth errors (807–810/DH-901): alert once, idle until the token is valid.
- Plan errors (806/DH-902): alert once, stop.
- Gate blocked: log; alert only if it persists > K minutes.
- Crash loop (> 3 restarts in 10 min, if detectable): alert.

**Phase 4+ (only on explicit request):** sandbox orders → 1-lot live with trade cap,
daily loss cap and a kill switch (RULES.md §7); static IP setup.

---

## J. Recommendation

**Not the entire application.** Dhan Cloud should host **only the strategy
engine**, ported to Python, as a read-only, scheduled scanner that alerts.
- The MCP server and Claude skill are an interactive analysis tool tied to Node
  and an LLM. They should stay local.
- GitHub plus CI remain the place where code is tested and released. There is no
  verified GitHub→Dhan Cloud sync, so deployment stays a deliberate manual step.
- **Zero human intervention cannot be promised yet.** It depends on three things
  still unverified or undecided: an unattended token path, Dhan Cloud's runtime
  rules and storage, and the owner's direction thresholds.

### Files changed in this phase
| File | Reason |
|---|---|
| `server/package-lock.json` | #1 lock sync; #13 MCP SDK 1.30.0 → 1.32.1, source-map-js |
| `server/src/transport.ts` | #2, #3 |
| `server/src/indicators.ts`, `server/src/levels.ts` | #4 session VWAP; #5 trade-plan projection |
| `server/src/client.ts` | #6 futures-series spot and contract |
| `server/src/priceHistory.ts` | #7 |
| `server/src/strikeSelect.ts` | #8 |
| `server/src/instruments/expiries.ts`, `server/src/snapshot.ts` | #9 |
| `server/src/mcpServer.ts`, `plugin/skills/sensex-scalp/SKILL.md` | #11 |
| `plugin/server/mcpServer.cjs` | Rebuilt bundle with all fixes |
| `engine/refresh_table.py` | #10 |
| `server/test/regressions.test.ts` (new), `server/test/priceHistory.test.ts` | Regression tests; 2 tests updated for #7 |
| `engine/test_refresh_table.py` (new) | Regression tests for #10 |
| `server/.env.example`, `plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json`, `.claude-plugin/marketplace.json` (new) | #12 reconstructed files |
| `README.md`, `docs/PHASE1_AUDIT.md` | Docs |

### Sources
- Dhan official skill references: <https://github.com/dhan-oss/dhanhq-skills> (error codes, rate limits, endpoints, instruments, static IP, live feed)
- DhanHQ docs home / Dhan Cloud overview: <https://docs.dhanhq.co/>, <https://developer.dhanhq.co/>
- Authentication and releases (24 h token, RenewToken, API key, TOTP): <https://dhanhq.co/docs/v2/authentication/>, <https://dhanhq.co/docs/v2/releases/>
- Dhan Cloud charges (pay-per-use): <https://dhan.co/support/platforms/dhanhq-api/what-are-the-charges-for-deploying-my-algo-on-dhan-cloud/>
- Data API subscription: <https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/>
- Option chain rate limit: <https://dhanhq.co/docs/v2/option-chain/>
- Static IP requirement: <https://dhanhq.co/docs/v2/orders/>
