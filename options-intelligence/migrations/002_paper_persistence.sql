-- Phase 6.1: persistence for the paper simulator (doi/paper).
-- Replaces the placeholder paper_* tables from 001. They were never written
-- (no code path used them) and their bigint ids and foreign keys to
-- users/signals do not match the engine's deterministic text ids.

DROP TABLE IF EXISTS paper_trades, paper_positions, paper_fills, paper_orders, paper_accounts CASCADE;

CREATE TABLE paper_accounts (
    id                text PRIMARY KEY,
    starting_capital  numeric(18, 2) NOT NULL CHECK (starting_capital > 0),
    cash              numeric(18, 4) NOT NULL,
    state             jsonb NOT NULL,            -- counters, streak, kill switch, session, last quote ts
    ledger_head       char(64) NOT NULL,
    event_count       integer NOT NULL CHECK (event_count >= 0),
    version           bigint NOT NULL DEFAULT 0,  -- optimistic lock; bumped on every save
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Tamper-evident ledger. payload_json/ts_iso hold the exact hashed text.
CREATE TABLE paper_ledger_events (
    account_id   text NOT NULL REFERENCES paper_accounts(id),
    seq          integer NOT NULL CHECK (seq > 0),
    ts           timestamptz NOT NULL,
    ts_iso       text NOT NULL,
    kind         text NOT NULL,
    payload_json text NOT NULL,
    payload      jsonb NOT NULL,
    prev_hash    char(64) NOT NULL,
    hash         char(64) NOT NULL UNIQUE,
    PRIMARY KEY (account_id, seq)
);
CREATE INDEX paper_ledger_events_kind_idx ON paper_ledger_events (account_id, kind, seq);

CREATE TABLE paper_cash_entries (
    account_id    text NOT NULL REFERENCES paper_accounts(id),
    seq           integer NOT NULL CHECK (seq > 0),
    ts            timestamptz NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('DEPOSIT', 'PREMIUM_DEBIT', 'PREMIUM_CREDIT', 'CHARGES')),
    amount        numeric(18, 4) NOT NULL,
    ref           text NOT NULL,
    balance_after numeric(18, 4) NOT NULL,
    amount_txt    text NOT NULL,             -- exact Decimal text; restores scale bit-for-bit (deterministic replay)
    balance_txt   text NOT NULL,
    CHECK (amount = amount_txt::numeric AND balance_after = balance_txt::numeric),
    PRIMARY KEY (account_id, seq)
);

CREATE TABLE paper_submissions (
    account_id      text NOT NULL REFERENCES paper_accounts(id),
    idempotency_key text PRIMARY KEY,           -- account:signal_id; duplicates impossible across restarts/processes
    signal_id       text NOT NULL,
    approved        boolean NOT NULL,
    order_id        text,
    decision        jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE paper_orders (
    account_id      text NOT NULL REFERENCES paper_accounts(id),
    id              text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    signal_id       text NOT NULL,
    position_id     text NOT NULL,
    purpose         text NOT NULL CHECK (purpose IN ('ENTRY', 'EXIT')),
    side            text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    security_id     text NOT NULL,
    quantity        integer NOT NULL CHECK (quantity > 0),
    filled_qty      integer NOT NULL CHECK (filled_qty >= 0),
    status          text NOT NULL,
    data            jsonb NOT NULL,
    PRIMARY KEY (account_id, id),
    CHECK (filled_qty <= quantity),
    CHECK (status <> 'FILLED' OR filled_qty = quantity)
);
CREATE INDEX paper_orders_active_idx ON paper_orders (account_id, status) WHERE status IN ('PAPER_ORDER_PENDING', 'PARTIALLY_FILLED');

CREATE TABLE paper_fills (
    account_id      text NOT NULL,
    id              text NOT NULL,
    order_id        text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,        -- order:quote-event; a tick can fill an order once
    ts              timestamptz NOT NULL,
    side            text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    price           numeric(14, 4) NOT NULL CHECK (price > 0),
    quantity        integer NOT NULL CHECK (quantity > 0),
    charges_total   numeric(14, 4) NOT NULL CHECK (charges_total >= 0),
    data            jsonb NOT NULL,
    PRIMARY KEY (account_id, id),
    FOREIGN KEY (account_id, order_id) REFERENCES paper_orders(account_id, id)
);

CREATE TABLE paper_positions (
    account_id      text NOT NULL REFERENCES paper_accounts(id),
    id              text NOT NULL,
    signal_id       text NOT NULL,
    underlying_key  text NOT NULL,
    security_id     text NOT NULL,
    state           text NOT NULL CHECK (state IN ('PLANNED', 'POSITION_OPEN', 'EXIT_PENDING', 'CLOSED', 'EVALUATED')),
    qty_entered     integer NOT NULL CHECK (qty_entered >= 0),
    qty_exited      integer NOT NULL CHECK (qty_exited >= 0),
    data            jsonb NOT NULL,
    PRIMARY KEY (account_id, id),
    CHECK (qty_exited <= qty_entered),
    CHECK (state <> 'PLANNED' OR qty_entered = 0)
);
-- At most one live (planned/open/exiting) position per contract per account.
CREATE UNIQUE INDEX paper_positions_one_live_per_contract
    ON paper_positions (account_id, security_id)
    WHERE state IN ('PLANNED', 'POSITION_OPEN', 'EXIT_PENDING');

CREATE TABLE paper_trades (
    account_id   text NOT NULL REFERENCES paper_accounts(id),
    position_id  text NOT NULL,
    net_pnl      numeric(18, 2) NOT NULL,
    gross_pnl    numeric(18, 2) NOT NULL,
    charges      numeric(18, 2) NOT NULL,
    closed_at    timestamptz NOT NULL,
    data         jsonb NOT NULL,
    PRIMARY KEY (account_id, position_id),
    FOREIGN KEY (account_id, position_id) REFERENCES paper_positions(account_id, id),
    CHECK (net_pnl = gross_pnl - charges)
);
