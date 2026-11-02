# Phase 7: architecture, deployment and operations

The system stays in **research and paper-trading mode**. No code path places, modifies or cancels a broker order. AI output never reaches the Bull50 MCP server or the `sensex-scalp` skill.

## 1. Architecture (as built)

```
DhanHQ (read-only REST + Live Market Feed)
   │  allowlisted transport, per-key scheduler, feed collector
   ▼
api process ──► MarketRecorder (SafeRecorder: failures never propagate) ──► PostgreSQL
   │             option_chain_snapshots(+exact payload) · market_bars · market_ticks
   │
   ├─ paper engine (PaperService: one DB transaction per operation, optimistic lock)
   ├─ auth (bearer keys, roles) · approval_records (hash chain, append-only) · kill switch (system_controls)
   ├─ /api/v1/ai/* (meetings on request, recommendations, human approval, budget, leaderboard)
   ├─ /api/v1/research/* (lab) · /api/v1/system/status · /metrics · /api/v1/reports/daily · /dashboard
   │
ai-worker process (python -m doi.ai.worker): reads recorded data only, never calls Dhan
   features (FeatureEngine on replayed data) → detectors (dwell, cooldown, caps) → ai_events queue
   → MeetingEngine: data-quality gate → 7 independent specialists → Contrarian → rebuttals
     → AI Risk Officer → CIO → Python authority (RiskGate dry-run veto, Risk Officer veto,
       contrarian-veto rule, compliance) → PENDING_HUMAN recommendation
   → LLMGateway: kill switch → routing/downgrade → cache → atomic budget reservation → xAI → settle
   → predictions pre-registered → PgScoring resolves them from later recorded observations
```

| Package | Responsibility |
|---|---|
| `doi/analytics`, `doi/intelligence/feature_engine` | Python source of truth for every number (7.0A) |
| `doi/data/recorder.py`, `doi/data/historical_ingestion/runner.py` | Recording, exact replay with SQL `as_of` bounds, audited ingestion, coverage (7.0B) |
| `doi/core/auth.py`, `doi/app/security.py` | Keys, roles, approval records, kill switch, the single human-gated paper bridge (7.0C) |
| `doi/llm/*` | Pricing, atomic budget, gateway, xAI client (7.1) |
| `doi/ai/events.py` | Detectors, admission control, queue (7.1) |
| `doi/ai/evidence.py`, `doi/ai/agents.py` | Evidence packs, strict claim validation, the 15 agents (7.2) |
| `doi/ai/meetings.py`, `doi/ai/service.py`, `doi/ai/worker.py` | Meetings, decisions, approvals, worker (7.3) |
| `doi/research/lab.py` | Strategy DSL, backtester, splits, holdout, promotion and rollback (7.4) |
| `doi/ai/scoring.py` | Independent scoring and leaderboard (7.5) |
| `doi/app/ops_routes.py`, `doi/reporting.py`, `doi/app/dashboard.html` | Status, metrics, report, dashboard (7.6) |

## 2. Deployment

```bash
cd options-intelligence
cp .env.example .env            # set POSTGRES_PASSWORD, DATABASE_URL, DHAN_CLIENT_ID; token in secrets/dhan.token
docker compose up -d --build    # db, redis (unused), api, ai-worker
```

The `api` and `ai-worker` processes both run `python -m doi.db.migrate` logic at start-up (advisory-locked, idempotent, checksummed).

Create users and keys. The token is printed once and only its hash is stored:

```bash
docker compose exec api python -m doi.core.auth create-key --email you@example.com --role admin
```

Then open `http://127.0.0.1:8000/dashboard` and paste the key. It is kept in `sessionStorage` for that tab only.

**Enabling Grok** (off by default): set `AI_ENABLED=true` and `XAI_API_KEY`, then restart `api` and `ai-worker`. Do this only after verifying the xAI items in section 7. Until then, meetings abort with `AI_UNAVAILABLE` and nothing else is affected.

**Enabling an index**: set `live_enabled: true` in its YAML only after `scripts/smoke_live.py` passes for that index.

## 3. Migrations

| # | File | Content |
|---|---|---|
| 001 | `001_init.sql` | Blueprint schema |
| 002 | `002_paper_persistence.sql` | Paper simulator persistence |
| 003 | `003_recording.sql` | Exact snapshot payloads, tick de-dupe, historical catalogue |
| 004 | `004_auth_controls.sql` | api_keys, hash-chained approval_records, system_controls, append-only paper ledger |
| 005 | `005_llm_orchestration.sql` | Price book, budget (DB CHECK enforces the cap), reservations, calls, cache, ai_events |
| 006 | `006_agents.sql` | Agent registry with immutable versions |
| 007 | `007_meetings.sql` | Evidence packs, meetings, turns, disagreements, decisions, recommendations, predictions, outcomes (timing triggers) |
| 008 | `008_append_only_hardening.sql` | TRUNCATE and statement-level guards on all audit tables |
| 009 | `009_research.sql` | Strategies, versions, activations, experiments, results, once-only holdout |
| 010 | `010_scoring.sql` | Scorecard history |

All migrations from 003 onward are additive. 002 replaced the never-written placeholder paper tables from 001, as approved in Phase 6.1.

## 4. Budget enforcement (hard cap ₹3,000 per month)

- The usable budget is the hard limit minus a ₹150 safety buffer.
- Every Grok call, including each retry, reserves its worst-case cost before it is sent. The cost is `ceil(chars/3)+32` prompt tokens at the input rate, plus `max_tokens` and a reasoning allowance at the output rate, converted at ₹90/USD × 1.18. The reservation is a single conditional `UPDATE`, and a table `CHECK (frozen OR spent + reserved <= usable)` makes overspending impossible at the database level, whatever the concurrency.
- Settlement uses the provider-reported `cost_in_usd_ticks` when present (1 tick = 1e-10 USD, per xai-proto), and otherwise the price book. A cost above the reservation is recorded in `overrun_inr` and **freezes** the month, so the cap is never silently exceeded.
- Timeouts and unknown failures are settled at the full estimate (they may have been billed). Clean HTTP errors are released.
- Downgrade as the budget is used:

  | Utilisation | Model routing |
  |---|---|
  | ≥ 60% | Only the CIO and Contrarian keep the reasoning tier |
  | ≥ 80% | Every call uses the fast tier |
  | ≥ 97% | No new AI calls at all |

  Admission control also refuses events whenever the AI is unavailable.
- Admins can lower the cap or unfreeze it via `POST /api/v1/ai/budget`. Raising it above `LLM_MONTHLY_LIMIT_INR` is refused, and every change is an approval record.

**Cost estimate.** Measured with real prompt sizes and pessimistic seed prices: a full trade meeting (7 specialists plus contrarian, risk, CIO and compliance) costs about **₹11**. It breaks down as 9 fast-tier calls at about 1,050 prompt tokens (about ₹1.9) and 2 reasoning calls at about 2,360 prompt tokens plus 2,000 reasoning tokens (about ₹9.2).

With the default caps (3 trade meetings a day, cooldowns, 22 sessions), that is about ₹730 a month for trade meetings. The rest of the budget is headroom, and any overrun is enforced by the cap. These are estimates until xAI prices are verified.

## 5. Failure handling (tested)

| Failure | Behaviour |
|---|---|
| Grok outage, 429 or 5xx | Gateway retries (each attempt reserved), then the circuit breaker opens. The meeting aborts `AI_UNAVAILABLE`. Polling, recording and the paper engine continue (`test_system_e2e`) |
| Budget exhausted | Admission refuses or the gateway refuses. The meeting gives NO_ACTION. Paper and market data are unaffected |
| Kill switch | Persisted. Admission skips AI events, the gateway refuses, paper entries are rejected by the RiskGate, and it is restored on restart |
| Database down for the recorder | `SafeRecorder` counts the failure, and polling and the feed continue |
| Worker crash mid-meeting | Turns are persisted. A resume reuses successful turns (never re-billed) and retries failed ones |
| Bad AI output | Schema and evidence validation, one corrective retry, then ABSTAIN. Quorum failure gives NO_ACTION |
| Prompt injection in evidence | Evidence is delimited as data, there are no write tools, Python enforces every authority rule, and the dashboard renders text only |
| Tampering | Append-only triggers (row, statement and TRUNCATE). Hash chains on the paper ledger and approvals detect superuser edits |

## 6. Operations

- **Status**: `GET /api/v1/system/status` (viewer). **Metrics**: `GET /metrics` (viewer; Prometheus text). **Daily report**: `GET /api/v1/reports/daily` or `python -m doi.reporting`.
- **Drills** to run before relying on the system:
  1. Kill switch on and off.
  2. Budget lowered to force a refusal.
  3. API key revoked.
  4. Database restore of a backup followed by `PaperStore.load` and reconciliation.
- **Backups**: PostgreSQL holds everything (paper ledger, approvals, meetings, predictions). Back it up daily and test restores.

## 7. Verification status

Verified from official sources reachable here:
- **xAI**: usage fields (`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_prompt_text_tokens`, `cost_in_usd_ticks`) and model-listing price units, from `xai-org/xai-proto@7c1372f` and `xai-sdk-python@884ba42`.
- **Dhan feed**: packet layout from `dhan-oss/DhanHQ-py@8c6583e`.
- **STT**: 0.15% from 1 Apr 2026, from several published broker schedules.

**UNVERIFIED** (not reachable, or no credentials, in this environment):
- docs.x.ai: the REST chat-completions request/response shape, which model names are live, current prices, and whether REST returns `cost_in_usd_ticks`.
- dhanhq.co: history depth per endpoint, intraday look-back, option-chain OI unit, and live feed behaviour.
- Completeness of the NSE/BSE 2026–27 holiday list.
- The NSE options exchange charge (0.03503% vs about 0.03553%).
- The INR/USD rate and GST treatment.
- Licences for any archive or vendor data beyond Dhan.

**Not live-tested**: any call to xAI, any call to Dhan (REST or WebSocket), the Docker images, and the dashboard in a real browser. Every test ran offline against fakes, plus a real local PostgreSQL 16.
