# Phase 8: live integration and validation report (2 Nov 2026)

Every result here is labelled **LIVE** (it touched a real external system) or **OFFLINE** (fakes, simulations or a local database). No broker order was placed, and no live-trading path exists.

## Environment blockers (checked 2 Nov 2026)

| Host | Needed for | Result from this cloud session |
|---|---|---|
| `api.x.ai`, `docs.x.ai` | Grok calls, official docs and prices | **Blocked** (proxy 403) |
| `api.dhan.co` | Dhan REST via our transport | Reachable, but **no Dhan credentials** are configured in this environment |
| `api-feed.dhan.co` | Live Market Feed (WebSocket) | **Blocked** |
| `images.dhan.co` | Instrument master CSV | **Blocked** (our transport needs it to resolve contracts) |
| `dhanhq.co` | Dhan docs | **Blocked** |
| `nsearchives.nseindia.com`, `www.nseindia.com`, `www.bseindia.com` | Official circulars and holiday lists | **Blocked** |
| Docker daemon | Image build | **Not running** (`/var/run/docker.sock` missing) |

To unblock, allow those hosts in the environment's network settings ([docs](https://code.claude.com/docs/en/cloud-environments#network-access)), then provide `XAI_API_KEY`, `DHAN_CLIENT_ID` and a Dhan token.

## Priority 1: Grok

| Check | Status |
|---|---|
| Official request format, models, prices | **NOT VERIFIED LIVE** (host blocked). Usage/cost fields and price units were verified earlier from official GitHub sources (xai-proto@7c1372f, xai-sdk-python@884ba42) |
| Real usage reporting | **NOT VERIFIED LIVE** |
| One real meeting, ₹20 cap | **NOT RUN.** `scripts/grok_live_check.py` is ready: it runs the model listing and price refresh, a probe call, all 15 agents, one meeting, and a budget reconciliation, stopping at ₹20. Here it exited with `XAI_API_KEY not set` and then `ProxyError` |
| Budget vs provider billing | **NOT VERIFIED LIVE.** Offline: settlement prefers provider `cost_in_usd_ticks`, and overruns freeze the month (tested) |
| ₹3,000 hard cap | Unchanged; enforced by a database CHECK constraint (OFFLINE, tested) |

## Priority 2: Dhan (LIVE through the user-connected Dhan MCP, read-only)

These calls went through the Dhan MCP connector, not through this repository's `DhanTransport`, so they validate Dhan's data, not our client code.

| Observation (LIVE, 2 Nov 2026, after market hours) | Result |
|---|---|
| Expiry list for NIFTY (`13`/`IDX_I`) | Weekly and monthly expiries from 2026-11-03 onward returned |
| Option chain NIFTY 2026-11-03 | Strike step 50; index LTP ₹23,440.80 |
| **OI unit** | Every non-zero OI is an exact multiple of 65 (the current NIFTY lot), for example 6,772,220 = 65 × 104,188. **OI is reported in units, not contracts.** Our default `oi_unit: units` is correct. The quote endpoint OI for contract 54052 (11,422,125) equals the chain's CE OI at 23500, so the two endpoints are consistent |
| Vendor IV | Visibly unreliable (0.0, 135.6, 190.0 on deep strikes), so keeping it quarantined is justified |
| Far strikes | LTP 0.00 / OI 0. The normaliser keeps a zero LTP but never treats zero bid/ask/IV as prices |
| Instrument master expiry stamp | Shown as `2026-11-03 14:30:00`. We use the date only, with expiry at 15:30 IST (the timezone of the stamp is unverified) |
| Bid/ask depth and timestamps | **Not available** through the MCP's formatted output, so it can't be verified this way |
| WebSocket feed, decoding, reconnects | **NOT VERIFIED LIVE** (feed host blocked). Offline decoder tests use the layout from the official DhanHQ-py source |
| Live recording and feature snapshots | **NOT RUN.** Needs our own transport with credentials and the instrument master host |
| Orders | **None placed.** Only market-data actions were called |

## Priority 3: Trading research (OFFLINE)

- **Option-premium backtester** (`doi/research/options_backtest.py`) prices the held contract by strike across ATM-relative rolling series. Fills are conservative: assumed half-spread plus slippage, stops at the bar low, and targets at the bar close. Every fill carries the full charges: brokerage, STT, exchange charges, SEBI fee, stamp duty and GST. Covered by 6 tests on **SIMULATED** (Black-76) data.
- **Data classes**: HISTORICAL, SIMULATED and LIVE_RECORDED are enforced on every option backtest, recorded on lab experiments, and stored append-only.
- **NSE charges**: the schedule has 0.03553% from 1 Mar 2026, per NSE/FA/73061. **Secondary-source verified**; the official PDF is blocked. Sources: [NSE/FA/73061](https://nsearchives.nseindia.com/content/circulars/FA73061.pdf), [Zerodha charges](https://zerodha.com/charges/), [Upstox](https://upstox.com/news/market-news/trading/bse-nse-revise-transaction-fees-for-cash-and-f-and-o-trades-revised-rates-to-come-into-effect-from-october-1/article-118923/), [Business Standard](https://www.business-standard.com/markets/news/higher-stt-and-transaction-charges-on-nse-bse-mcx-effective-today-124100100146_1.html).
- **Holidays (2026)**: 15 Jan, 26 Jan, 3 Mar, 2 Oct, 10 Nov, 24 Nov, 25 Dec. **Secondary-source verified** ([Tickertape](https://www.tickertape.in/blog/nse-holidays-2026/), [Upstox](https://upstox.com/stocks-market/nse-holidays/), [HDFC Sky](https://hdfcsky.com/news/nse-to-be-closed-on-march-3-on-account-of-holi), [algotest](https://algotest.in/blog/share-market-holiday/)). Mahashivratri 2026 could not be confirmed. The Muhurat session (Sun 8 Nov) is not modelled.
- **Not done**: no real historical option data has been ingested, so no HISTORICAL backtest exists yet.

## Priority 4: Deployment

| Check | Status |
|---|---|
| Docker image build | **NOT RUN** (no daemon). Package data, including the dashboard, YAML configs and holidays, was verified in a non-editable install |
| Controlled deployment (`scripts/deploy_drill.py`) | **PASSED, OFFLINE/local.** Real PostgreSQL 16 with a password-auth DSN; migrations CLI twice (11 applied, then "up to date"); key CLI; uvicorn exactly as the Docker CMD; 401 without or with a bad key; 200 with a key; dashboard with CSP; metrics; kill switch on; **SIGKILL crash and restart**: kill switch still on, paper ledger re-verified and reconciled (2 events, then 3 with RECOVERY), approval chain intact; **secret scan**: DB password, xAI key and admin token absent from logs and `pg_dump`; 0 tracebacks |
| Bugs found by the drill, now fixed | (1) The API process never installed log redaction. (2) The redaction filter cleared `record.args`, which broke uvicorn access logs with a traceback on every request |

## Priority 5: Validation

| Check | Status |
|---|---|
| ≥ 10 supervised paper sessions | **NOT RUN.** Needs live Dhan credentials, reachable feed and master hosts, and 10 trading days |
| Predictions vs outcomes | Implemented (`PgScoring`), with outcome timing enforced by triggers. **No live predictions exist yet** |
| Single vs three-agent vs full team | Harness `doi/research/team_study.py` is built and tested on **SIMULATED** replies only (it verifies the harness, not the model). Each team runs the same cases; the scorecard covers regime recognition, good-rejection rate, evidence-cited claims, calibration, worst recommendation, mean return, and LLM cost per useful recommendation |
| Risk-adjusted performance, cost per useful recommendation | Metrics exist in the team study and the option-premium backtester. **No real numbers yet** |

### Supervised runbook, once unblocked
1. Run `scripts/smoke_live.py` for each index, and set `live_enabled: true` only for indices that pass.
2. Run `scripts/grok_live_check.py` (≤ ₹20) and check the price book and usage fields. Then set `AI_ENABLED=true`.
3. Each session:
   - The API records data and the worker holds meetings.
   - A human approves or rejects recommendations on the dashboard.
   - The worker's scoring resolves predictions.
4. After 10 sessions, run the team study on the recorded contexts (LIVE_RECORDED) and option-premium backtests on approved Dhan datasets (HISTORICAL). Judge the results by the scorecard criteria, not by signal count.

## Offline test totals
256 tests pass. Lint and the security scan are clean, and there are no known dependency vulnerabilities.
