-- Phase 7.1: LLM gateway accounting, hard budget, cache, event queue. Additive.

CREATE TABLE IF NOT EXISTS llm_price_book (
    model                   text NOT NULL,
    effective_from          timestamptz NOT NULL,
    input_per_m_usd         numeric(12, 6) NOT NULL CHECK (input_per_m_usd >= 0),
    cached_input_per_m_usd  numeric(12, 6) NOT NULL CHECK (cached_input_per_m_usd >= 0),
    output_per_m_usd        numeric(12, 6) NOT NULL CHECK (output_per_m_usd >= 0),
    source                  text NOT NULL,           -- 'seed-unverified' | 'xai:/v1/language-models'
    verified                boolean NOT NULL DEFAULT false,
    PRIMARY KEY (model, effective_from)
);

-- The hard cap is enforced by the database itself: spent + reserved can never
-- exceed the limit unless the month is frozen (a recorded provider overrun).
CREATE TABLE IF NOT EXISTS llm_budget_months (
    month          date PRIMARY KEY CHECK (extract(day FROM month) = 1),
    hard_limit_inr numeric(12, 2) NOT NULL CHECK (hard_limit_inr > 0),
    usable_inr     numeric(12, 2) NOT NULL CHECK (usable_inr > 0 AND usable_inr <= hard_limit_inr),
    spent_inr      numeric(16, 6) NOT NULL DEFAULT 0 CHECK (spent_inr >= 0),
    reserved_inr   numeric(16, 6) NOT NULL DEFAULT 0 CHECK (reserved_inr >= 0),
    overrun_inr    numeric(16, 6) NOT NULL DEFAULT 0 CHECK (overrun_inr >= 0),
    frozen         boolean NOT NULL DEFAULT false,
    frozen_reason  text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (frozen OR spent_inr + reserved_inr <= usable_inr)
);

CREATE TABLE IF NOT EXISTS llm_budget_reservations (
    id           text PRIMARY KEY,
    month        date NOT NULL REFERENCES llm_budget_months(month),
    estimate_inr numeric(16, 6) NOT NULL CHECK (estimate_inr > 0),
    settled_inr  numeric(16, 6),
    status       text NOT NULL CHECK (status IN ('RESERVED', 'SETTLED', 'RELEASED')),
    purpose      text NOT NULL,
    agent_id     text,
    meeting_id   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    settled_at   timestamptz
);
CREATE INDEX IF NOT EXISTS llm_budget_reservations_open ON llm_budget_reservations (month) WHERE status = 'RESERVED';

CREATE TABLE IF NOT EXISTS llm_calls (
    id                text PRIMARY KEY,
    reservation_id    text REFERENCES llm_budget_reservations(id),
    agent_id          text,
    agent_version     text,
    meeting_id        text,
    purpose           text NOT NULL,
    model             text NOT NULL,
    tier              text NOT NULL,
    request_sha256    char(64) NOT NULL,
    cache_hit         boolean NOT NULL,
    attempt           integer NOT NULL,
    prompt_tokens     integer,
    cached_tokens     integer,
    completion_tokens integer,
    reasoning_tokens  integer,
    cost_usd          numeric(16, 10),
    cost_source       text,                        -- PROVIDER_TICKS | PRICE_BOOK | ESTIMATE_ON_FAILURE | CACHE
    fx_inr_per_usd    numeric(10, 4),
    cost_inr          numeric(16, 6) NOT NULL DEFAULT 0,
    latency_ms        integer,
    status            text NOT NULL,               -- OK | ERROR | TIMEOUT | CACHE_HIT | REFUSED_BUDGET | REFUSED_KILL_SWITCH
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_calls_month ON llm_calls (created_at);
CREATE INDEX IF NOT EXISTS llm_calls_agent ON llm_calls (agent_id, created_at);

CREATE TABLE IF NOT EXISTS llm_cache (
    request_sha256 char(64) PRIMARY KEY,
    model          text NOT NULL,
    response       jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_events (
    id             text PRIMARY KEY,
    kind           text NOT NULL,
    underlying_key text,
    fingerprint    text NOT NULL UNIQUE,              -- one meeting per real-world event
    priority       integer NOT NULL DEFAULT 5,
    payload        jsonb NOT NULL,
    status         text NOT NULL CHECK (status IN ('QUEUED', 'SKIPPED', 'RUNNING', 'DONE', 'FAILED')),
    skip_reason    text,
    valid_until    timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    attempts       integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ai_events_queue ON ai_events (status, priority, created_at) WHERE status = 'QUEUED';
CREATE INDEX IF NOT EXISTS ai_events_kind ON ai_events (kind, underlying_key, created_at);
