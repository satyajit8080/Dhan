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
| 4, 5, 7, 8 | not started | Analytics, signal engine, backtests, dashboard |

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

## Develop

```bash
cd backend
pip install -e ".[dev]"
pytest -q
ruff check .
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
