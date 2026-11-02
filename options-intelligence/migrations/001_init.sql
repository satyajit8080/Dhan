-- Dhan Options Intelligence - initial schema (blueprint section 12).
-- PostgreSQL is the system of record. All timestamps are UTC (timestamptz).
-- Raw request manifests are stored separately from normalized rows so bad
-- ingestion can be audited and replayed.

BEGIN;

CREATE TABLE users (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    status      text NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE broker_sessions (
    id                  bigserial PRIMARY KEY,
    user_id             bigint NOT NULL REFERENCES users(id),
    provider            text NOT NULL DEFAULT 'dhan',
    encrypted_secret_ref text NOT NULL,       -- pointer into the secret store, never the token
    token_fingerprint   text,                 -- last 4 chars only
    token_expires_at    timestamptz,
    last_validated_at   timestamptz,
    UNIQUE (user_id, provider)
);

CREATE TABLE instrument_master_versions (
    version         char(64) PRIMARY KEY,     -- sha256 of the source file
    source_url      text NOT NULL,
    retrieved_at    timestamptz NOT NULL,
    effective_from  date NOT NULL,
    row_count       integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('active', 'superseded', 'rejected'))
);

CREATE TABLE instruments (
    master_version  char(64) NOT NULL REFERENCES instrument_master_versions(version),
    security_id     text NOT NULL,
    symbol          text NOT NULL,
    underlying_key  text,
    exchange_segment text NOT NULL,
    instrument_type text NOT NULL,
    expiry          date,
    strike          numeric(14, 4),
    option_type     char(2) CHECK (option_type IN ('CE', 'PE')),
    lot_size        integer,
    tick_size       numeric(10, 4),
    freeze_qty      integer,
    PRIMARY KEY (master_version, security_id)
);
CREATE INDEX instruments_contract_idx ON instruments (underlying_key, expiry, strike, option_type);

CREATE TABLE raw_request_manifests (
    id              bigserial PRIMARY KEY,
    endpoint        text NOT NULL,
    request_key     text NOT NULL,
    request_body    jsonb NOT NULL,           -- never includes headers/tokens
    requested_at    timestamptz NOT NULL,
    received_at     timestamptz,
    http_status     integer,
    error_code      text,
    attempts        integer NOT NULL DEFAULT 1,
    payload_sha256  char(64),
    payload_ref     text,                     -- object-store pointer, subject to retention
    parser_version  text NOT NULL
);
CREATE INDEX raw_request_manifests_key_idx ON raw_request_manifests (endpoint, request_key, requested_at DESC);

CREATE TABLE market_ticks (
    event_ts    timestamptz NOT NULL,
    receive_ts  timestamptz NOT NULL,
    security_id text NOT NULL,
    ltp         numeric(14, 4),
    bid         numeric(14, 4),
    ask         numeric(14, 4),
    bid_qty     bigint,
    ask_qty     bigint,
    volume      bigint,
    oi          bigint,
    source_seq  bigint
);
CREATE INDEX market_ticks_sec_ts_idx ON market_ticks (security_id, event_ts DESC);

CREATE TABLE market_bars (
    security_id    text NOT NULL,
    timeframe      text NOT NULL,
    bar_ts         timestamptz NOT NULL,
    open           numeric(14, 4) NOT NULL,
    high           numeric(14, 4) NOT NULL,
    low            numeric(14, 4) NOT NULL,
    close          numeric(14, 4) NOT NULL,
    volume         bigint,
    oi             bigint,
    source_version text NOT NULL,
    PRIMARY KEY (security_id, timeframe, bar_ts)
);

CREATE TABLE option_chain_snapshots (
    id                 bigserial PRIMARY KEY,
    underlying_key     text NOT NULL,
    expiry             date NOT NULL,
    request_key        text NOT NULL,
    master_version     char(64) NOT NULL REFERENCES instrument_master_versions(version),
    requested_at       timestamptz NOT NULL,
    received_at        timestamptz NOT NULL,
    source_ts          timestamptz,
    underlying_ltp     numeric(14, 4),
    data_quality_score numeric(5, 2),
    coverage_status    text NOT NULL,
    is_stale           boolean NOT NULL
);
CREATE INDEX option_chain_snapshots_lookup_idx
    ON option_chain_snapshots (underlying_key, expiry, received_at DESC, request_key);

CREATE TABLE option_chain_rows (
    snapshot_id bigint NOT NULL REFERENCES option_chain_snapshots(id) ON DELETE CASCADE,
    strike      numeric(14, 4) NOT NULL,
    option_type char(2) NOT NULL CHECK (option_type IN ('CE', 'PE')),
    security_id text NOT NULL DEFAULT '',
    ltp         numeric(14, 4),
    oi          bigint,
    change_oi   bigint,
    volume      bigint,
    iv          numeric(8, 4),
    delta       numeric(10, 6),
    gamma       numeric(12, 8),
    theta       numeric(14, 6),
    vega        numeric(14, 6),
    bid         numeric(14, 4),
    bid_qty     bigint,
    ask         numeric(14, 4),
    ask_qty     bigint,
    UNIQUE (snapshot_id, strike, option_type, security_id)
);

CREATE TABLE data_quality_audits (
    id              bigserial PRIMARY KEY,
    dataset_id      text NOT NULL,
    index_key       text NOT NULL,
    date_from       date NOT NULL,
    date_to         date NOT NULL,
    expected_count  integer NOT NULL,
    actual_count    integer NOT NULL,
    missing_pct     numeric(7, 4) NOT NULL,
    gap_count       integer NOT NULL,
    duplicate_count integer NOT NULL,
    missing_strikes integer NOT NULL,
    audit_version   text NOT NULL,
    status          text NOT NULL CHECK (status IN ('APPROVED', 'APPROVED_WITH_GAPS', 'QUARANTINED', 'UNSUPPORTED')),
    reasons         jsonb NOT NULL DEFAULT '[]',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX data_quality_audits_idx ON data_quality_audits (index_key, status, date_from);

CREATE TABLE market_features (
    event_ts           timestamptz NOT NULL,
    underlying_key     text NOT NULL,
    timeframe          text NOT NULL,
    feature_json       jsonb NOT NULL,
    feature_version    text NOT NULL,
    data_quality_score numeric(5, 2),
    coverage_status    text NOT NULL,
    PRIMARY KEY (underlying_key, timeframe, event_ts, feature_version)
);

CREATE TABLE index_strategy_configs (
    underlying_key       text NOT NULL,
    config_version       text NOT NULL,
    weights_json         jsonb NOT NULL,
    thresholds_json      jsonb NOT NULL,
    contract_filters_json jsonb NOT NULL,
    enabled_at           timestamptz,
    PRIMARY KEY (underlying_key, config_version)
);

CREATE TABLE signals (
    id                  bigserial PRIMARY KEY,
    underlying_key      text NOT NULL,
    option_contract     text,
    direction           text NOT NULL CHECK (direction IN ('BUY_CE_CANDIDATE', 'BUY_PE_CANDIDATE', 'WAIT', 'REJECTED')),
    scores              jsonb NOT NULL,
    entry               numeric(14, 4),
    stop_loss           numeric(14, 4),
    targets             jsonb,
    reason_json         jsonb NOT NULL,
    feature_snapshot_id text,
    config_version      text NOT NULL,
    status              text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signals_idx ON signals (underlying_key, created_at DESC, status);

CREATE TABLE signal_evaluations (
    signal_id       bigint NOT NULL REFERENCES signals(id),
    horizon         text NOT NULL,
    mfe             numeric(14, 4),
    mae             numeric(14, 4),
    outcome         text,
    net_return      numeric(14, 4),
    evaluation_ts   timestamptz NOT NULL,
    PRIMARY KEY (signal_id, horizon)
);

-- Paper trading: simulated only. No column or table references a broker order id.
CREATE TABLE paper_accounts (
    id                  bigserial PRIMARY KEY,
    owner               bigint NOT NULL REFERENCES users(id),
    starting_capital    numeric(16, 2) NOT NULL,
    equity              numeric(16, 2) NOT NULL,
    available_cash      numeric(16, 2) NOT NULL,
    daily_start_equity  numeric(16, 2) NOT NULL
);

CREATE TABLE paper_orders (
    id              bigserial PRIMARY KEY,
    account_id      bigint NOT NULL REFERENCES paper_accounts(id),
    signal_id       bigint REFERENCES signals(id),
    contract_id     text NOT NULL,
    side            text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity        integer NOT NULL CHECK (quantity > 0),
    limit_price     numeric(14, 4),
    status          text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE paper_fills (
    id                 bigserial PRIMARY KEY,
    order_id           bigint NOT NULL REFERENCES paper_orders(id),
    fill_ts            timestamptz NOT NULL,
    fill_price         numeric(14, 4) NOT NULL,
    quantity           integer NOT NULL,
    slippage           numeric(14, 4) NOT NULL,
    charges            numeric(14, 4) NOT NULL,
    charge_schedule    text NOT NULL,
    fill_model_version text NOT NULL,
    idempotency_key    text NOT NULL UNIQUE
);

CREATE TABLE paper_positions (
    account_id     bigint NOT NULL REFERENCES paper_accounts(id),
    contract_id    text NOT NULL,
    quantity       integer NOT NULL,
    average_price  numeric(14, 4) NOT NULL,
    unrealized_pnl numeric(16, 2),
    PRIMARY KEY (account_id, contract_id)
);

CREATE TABLE paper_trades (
    id            bigserial PRIMARY KEY,
    account_id    bigint NOT NULL REFERENCES paper_accounts(id),
    entry_fill_id bigint NOT NULL REFERENCES paper_fills(id),
    exit_fill_id  bigint REFERENCES paper_fills(id),
    gross_pnl     numeric(16, 2),
    charges       numeric(16, 2),
    net_pnl       numeric(16, 2),
    mfe           numeric(14, 4),
    mae           numeric(14, 4),
    exit_reason   text
);

CREATE TABLE backtest_runs (
    run_id           uuid PRIMARY KEY,
    index_key        text NOT NULL,
    strategy_version text NOT NULL,
    config_version   text NOT NULL,
    date_from        date NOT NULL,
    date_to          date NOT NULL,
    dataset_manifest jsonb NOT NULL,
    code_version     text NOT NULL,
    metrics          jsonb,
    status           text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX backtest_runs_idx ON backtest_runs (index_key, created_at DESC, strategy_version);

CREATE TABLE backtest_trades (
    id        bigserial PRIMARY KEY,
    run_id    uuid NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    contract  text NOT NULL,
    entry_ts  timestamptz NOT NULL,
    exit_ts   timestamptz,
    quantity  integer NOT NULL,
    fills     jsonb NOT NULL,
    charges   numeric(16, 2) NOT NULL,
    pnl       numeric(16, 2)
);

-- TimescaleDB hypertables when the extension is available.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
        PERFORM create_hypertable('market_ticks', 'event_ts', if_not_exists => TRUE);
        PERFORM create_hypertable('market_bars', 'bar_ts', if_not_exists => TRUE);
    END IF;
END
$$;

COMMIT;
