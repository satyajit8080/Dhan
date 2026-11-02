# Dhan Options Intelligence

Paper-only signal platform for NIFTY, BANKNIFTY, FINNIFTY, SENSEX and BANKEX index options.
It implements Technical Blueprint V2 (2 Nov 2026). It does not give investment advice and no live
order path exists.

It is self-contained so it can move to its own repository unchanged (`git subtree split -P options-intelligence`).

## Status against the roadmap (blueprint section 15)

| Phase | State | What exists |
|---|---|---|
| 0 Foundations | done | `pyproject.toml`, Docker Compose (TimescaleDB, Redis, API), `migrations/001_init.sql` with all 21 tables (applies cleanly on PG16), CI, log redaction |
| 1 Dhan data adapter | done, needs a live run | Read-only transport with an endpoint allowlist, a token-expiry monitor, an instrument master that resolves all five indices (ids, lot size and strike step are never hard-coded), expiry list, chain normalization, and `scripts/smoke_live.py` |
| 2 Scheduler | REST done | Per-request-key 3 s cooldown, bounded priority pool, coalescing, cache, jittered backoff, circuit breaker. The WebSocket feed collector is still to do |
| 3 Historical data and quality | planner and audit done | One ATM offset and one side per request, windows of 30 days or less, and a coverage audit that returns APPROVED / WITH_GAPS / QUARANTINED / UNSUPPORTED. The SENSEX/BANKEX history gate is `blocked` |
| 6 Risk + paper execution | done (in memory) | `doi/risk/` is a deterministic risk gate (sizing, daily cap, aggregate risk, concurrency, correlated exposure, loss streak, reward/risk net of costs, liquidity, freshness, entry cutoff). `doi/paper/` is a bid/ask fill model with latency, slippage, lot-sized partial fills and a protective entry limit. It also has date-effective charges (brokerage, STT, exchange, SEBI, stamp duty, GST), stop/target/underlying/time/kill-switch exits, a hash-chained ledger with cash reconciliation, an optional JSONL sink, and REST routes. Postgres persistence and restoring after a restart are not wired yet |
| 6.1 Persistence | done | PostgreSQL (`migrations/002_paper_persistence.sql`) holds accounts, cash book, the hash-chained ledger, submissions, orders, fills, positions and trades. Each engine operation is saved in one transaction with an optimistic version lock, a ledger-chain continuity check and an SQL cash re-sum. At start-up the app migrates, loads, reconciles and recovers |
| 6.2 Reliability | done | `PaperExecution.recover`, `doi/paper/reconcile.py` (an independent ledger projection compared against state), positions carried over a restart or holiday are squared off at the next open, and the holiday calendar is in `doi/core/holidays.yaml` (partial, see below) |
| 6.3 Integration | done, needs a live run | Chain polling and the `doi/data/dhan/collector.py` Live Market Feed (v2 binary, full packet with depth, reconnect with jitter, auth disconnects are fatal) both feed the persisted engine through `PaperService`. The feed is off by default (`DHAN_FEED_ENABLED`) |
| 7.0A Feature engine | done | `doi/analytics/` holds the pure technical, Black-76 and option-chain functions. `doi/intelligence/feature_engine/` holds the versioned config, the feature declarations (inputs, lookback, warm-up, null policy, publication time), `FeatureEngine.calculate()`, which returns an immutable `FeatureVector` with per-feature provenance and a SHA-256 digest, and `FeatureStore`, which writes once to `market_features`. It drops all data not yet known at `as_of`, and tests enforce this |
| 7.0B–7.6 AI research institution | done (offline-tested) | Recording and replay, auth and approvals, the Grok gateway with a ₹3,000 hard budget, 15 agents, meetings, the research lab, the leaderboard, dashboard and ops. See `docs/phase7-deployment.md`. Grok and Dhan are **not live-tested** |
| 4 (rest), 5, 7, 8 | not started | Analytics, signal engine, backtests, dashboard |

## Layout

```
backend/doi/
  core/                     config, errors, canonical models, redacting logger
  data/dhan/                transport (allowlist), token, normalize, adapter
  data/instruments/         instrument master parse/version/resolve
  data/scheduler/           KeyedScheduler (live + historical lanes)
  data/historical_ingestion rolling-option request planner
  data/quality/             coverage audit + session grid
  data/provider.py          MarketDataProvider protocol
  risk/                     RiskGate + candidate/decision models
  paper/                    fills, charges, engine, ledger, chain driver
  intelligence/strategy_configs/  five versioned index YAMLs + strict loader
  app/                      FastAPI app + runtime wiring
migrations/  infra/docker/  scripts/
```

## Migrations

`python -m doi.db.migrate` (also runs at start-up when `DATABASE_URL` is set). Files run in order, each in
one transaction, under an advisory lock and with a recorded checksum. Editing an already-applied file is refused.

## Develop

```bash
cd backend
pip install -e ".[dev]"
pytest -q                      # persistence tests use TEST_DATABASE_URL or a local PostgreSQL install; skipped otherwise
ruff check .
bandit -q -r doi -ll
pip-audit
```

## Run

```bash
cp .env.example .env   # fill DHAN_CLIENT_ID, POSTGRES_PASSWORD; put the token in secrets/dhan.token
docker compose up --build
```

Live smoke test during market hours (Phase 1 exit criteria):

```bash
python scripts/smoke_live.py
```

Every index config ships with `live_enabled: false`. Set it to true only after that index passes the smoke test.

## Safety invariants (enforced by tests)

- `DhanTransport` can only POST to read-only data paths. Order, position, funds and margin paths raise before any network call.
- The `dhanhq` SDK object is not created at runtime because it carries order methods. Its REST contract is used through the allowlisted transport instead.
- Tokens are never logged. Status shows only the last 4 characters. JWTs and auth headers are redacted from all logs.
- No index config can inherit from another. Unknown keys are rejected, weights must total 100, and thresholds left as `null` mean the engine returns WAIT.
- A backtest dataset can be used only if its audit status is APPROVED (or APPROVED_WITH_GAPS when policy allows it).
- Paper fills never use the LTP. They happen only at the bid/ask, only on a quote seen after the simulated latency, and only in whole lots. With no touch there is no fill.
- Every paper order idempotency key (`account:signal_id`) and fill key (`order:quote event`) is unique. Duplicate or out-of-order ticks are ignored. Order and position state machines reject illegal transitions.
- The paper ledger is hash-chained and `verify()` reconciles cash against every fill. The same inputs always produce the same ledger hash.

Paper routes: `POST /api/v1/paper/orders`, `GET /api/v1/paper/portfolio`, `GET /api/v1/paper/trades`,
`GET /api/v1/paper/ledger`, `POST /api/v1/paper/positions/{id}/close`. Charge rates are research defaults
(STT is 0.15% on sold premium from 1 Apr 2026, per Budget 2026-27). Reconcile them against real contract notes.
