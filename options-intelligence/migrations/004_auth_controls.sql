-- Phase 7.0C: authentication, append-only approval records, persistent controls.
-- Additive. Also makes the paper ledger append-only at the database level.

CREATE OR REPLACE FUNCTION doi_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END
$$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text;

CREATE TABLE IF NOT EXISTS api_keys (
    id           text PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users(id),
    key_hash     char(64) NOT NULL UNIQUE,      -- sha256 of the bearer token; the token itself is never stored
    role         text NOT NULL CHECK (role IN ('viewer', 'approver', 'admin')),
    label        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,
    last_used_at timestamptz
);

-- Every human decision (approvals, rejections, promotions, rollbacks, kill switch,
-- budget changes) is hash-chained and immutable.
CREATE TABLE IF NOT EXISTS approval_records (
    seq          bigint PRIMARY KEY,
    created_at   timestamptz NOT NULL,
    user_id      bigint REFERENCES users(id),
    actor        text NOT NULL,
    role         text NOT NULL,
    action       text NOT NULL CHECK (action IN ('APPROVE', 'REJECT', 'PROMOTE', 'ROLLBACK', 'KILL_SWITCH_ON',
                                                  'KILL_SWITCH_OFF', 'BUDGET_CHANGE', 'PAPER_ORDER', 'PAPER_CLOSE', 'TEST_APPROVAL')),
    subject_kind text NOT NULL,
    subject_id   text NOT NULL,
    note         text,
    payload      jsonb NOT NULL,
    prev_hash    char(64) NOT NULL,
    hash         char(64) NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS approval_records_subject ON approval_records (subject_kind, subject_id, seq);
DROP TRIGGER IF EXISTS approval_records_append_only ON approval_records;
CREATE TRIGGER approval_records_append_only BEFORE UPDATE OR DELETE ON approval_records
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();

DROP TRIGGER IF EXISTS paper_ledger_events_append_only ON paper_ledger_events;
CREATE TRIGGER paper_ledger_events_append_only BEFORE UPDATE OR DELETE ON paper_ledger_events
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();

CREATE TABLE IF NOT EXISTS system_controls (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_by bigint REFERENCES users(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO system_controls (key, value) VALUES ('kill_switch', '{"on": false}') ON CONFLICT (key) DO NOTHING;
