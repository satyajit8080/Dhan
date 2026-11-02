-- Phase 7.3: permanent meeting memory, decisions, human-gated recommendations,
-- and pre-registered predictions. Additive. Audit tables are append-only.

CREATE TABLE IF NOT EXISTS ai_evidence_packs (
    id             text PRIMARY KEY,
    sha256         char(64) NOT NULL UNIQUE,
    as_of          timestamptz NOT NULL,
    underlying_key text,
    content        jsonb NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_meetings (
    id               text PRIMARY KEY,
    event_id         text UNIQUE,                       -- one meeting per admitted event
    event            jsonb NOT NULL,
    evidence_pack_id text REFERENCES ai_evidence_packs(id),
    status           text NOT NULL CHECK (status IN ('OPEN', 'DECIDED', 'ABORTED')),
    abort_reason     text,
    final_action     text,
    started_at       timestamptz NOT NULL,
    ended_at         timestamptz,
    cost_inr         numeric(14, 6) NOT NULL DEFAULT 0,
    llm_calls        integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ai_meetings_started ON ai_meetings (started_at DESC);

CREATE TABLE IF NOT EXISTS ai_meeting_turns (
    meeting_id       text NOT NULL REFERENCES ai_meetings(id),
    seq              integer NOT NULL,
    round            text NOT NULL,                     -- R1 | CONTRARIAN | R2 | RISK | CIO | COMPLIANCE
    agent_id         text NOT NULL,
    agent_version    text NOT NULL,
    status           text NOT NULL CHECK (status IN ('OK', 'ABSTAIN', 'UNAVAILABLE')),
    stance           text,
    confidence       integer,
    output           jsonb,
    errors           jsonb NOT NULL DEFAULT '[]',
    visible_sections text[] NOT NULL,
    call_ids         text[] NOT NULL,
    cost_inr         numeric(14, 6) NOT NULL DEFAULT 0,
    model            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meeting_id, seq),
    UNIQUE (meeting_id, round, agent_id)
);

CREATE TABLE IF NOT EXISTS ai_disagreements (
    meeting_id text NOT NULL REFERENCES ai_meetings(id),
    seq        integer NOT NULL,
    topic      text NOT NULL,
    agents     text[] NOT NULL,
    positions  jsonb NOT NULL,
    resolution text,
    PRIMARY KEY (meeting_id, seq)
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    meeting_id      text PRIMARY KEY REFERENCES ai_meetings(id),
    cio_action      text,
    final_action    text NOT NULL CHECK (final_action IN ('RECOMMEND_PAPER', 'WATCH', 'NO_ACTION', 'ESCALATE')),
    overrides       jsonb NOT NULL,                     -- deterministic rules applied over the CIO (risk veto, compliance, ...)
    rationale       jsonb,
    dissent         jsonb NOT NULL,
    risk_dry_run    jsonb,
    compliance      jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id              text PRIMARY KEY,                   -- also the paper engine signal_id (idempotency)
    meeting_id      text NOT NULL UNIQUE REFERENCES ai_meetings(id),
    underlying_key  text NOT NULL,
    expiry          date NOT NULL,
    strike          numeric(14, 4) NOT NULL,
    option_type     char(2) NOT NULL CHECK (option_type IN ('CE', 'PE')),
    stop_loss       numeric(14, 4) NOT NULL,
    targets         jsonb NOT NULL,
    valid_until     timestamptz NOT NULL,
    status          text NOT NULL CHECK (status IN ('PENDING_HUMAN', 'APPROVED', 'REJECTED', 'EXPIRED', 'SUBMITTED', 'RISK_REJECTED')),
    decided_by      text,
    decided_at      timestamptz,
    paper_result    jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_predictions (
    id             text PRIMARY KEY,
    agent_id       text NOT NULL,
    agent_version  text NOT NULL,
    meeting_id     text REFERENCES ai_meetings(id),
    underlying_key text NOT NULL,
    target         text NOT NULL,
    direction      text NOT NULL CHECK (direction IN ('UP', 'DOWN', 'FLAT')),
    confidence     integer NOT NULL CHECK (confidence BETWEEN 1 AND 5),
    horizon_min    integer NOT NULL CHECK (horizon_min > 0),
    ref_value      numeric(16, 4) NOT NULL,
    created_at     timestamptz NOT NULL,
    resolve_at     timestamptz NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    mode           text NOT NULL DEFAULT 'LIVE' CHECK (mode IN ('LIVE', 'REPLAY')),
    CHECK (resolve_at > created_at),
    -- LIVE predictions are stamped by the database clock: no backdating, no post-dating.
    -- REPLAY predictions (historical simulation) are kept separate from live scoring.
    CHECK (mode = 'REPLAY' OR (created_at >= recorded_at - interval '5 minutes' AND created_at <= recorded_at + interval '5 seconds'))
);
CREATE INDEX IF NOT EXISTS ai_predictions_due ON ai_predictions (resolve_at);
DROP TRIGGER IF EXISTS ai_predictions_append_only ON ai_predictions;
CREATE TRIGGER ai_predictions_append_only BEFORE UPDATE OR DELETE ON ai_predictions
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();

CREATE TABLE IF NOT EXISTS ai_prediction_outcomes (
    prediction_id  text PRIMARY KEY REFERENCES ai_predictions(id),
    resolved_at    timestamptz NOT NULL,
    realized_value numeric(16, 4) NOT NULL,
    observed_at    timestamptz NOT NULL,                -- timestamp of the market observation used
    signed_return  numeric(16, 8) NOT NULL,
    hit            boolean NOT NULL,
    brier          numeric(10, 8) NOT NULL
);
CREATE OR REPLACE FUNCTION doi_outcome_after_prediction() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p ai_predictions%ROWTYPE;
BEGIN
    SELECT * INTO p FROM ai_predictions WHERE id = NEW.prediction_id;
    IF NEW.observed_at < p.resolve_at OR NEW.resolved_at < p.resolve_at OR (p.mode = 'LIVE' AND p.recorded_at > NEW.observed_at) THEN
        RAISE EXCEPTION 'outcome for % must be observed at/after resolve_at and after the prediction was recorded', NEW.prediction_id;
    END IF;
    RETURN NEW;
END
$$;
DROP TRIGGER IF EXISTS ai_prediction_outcomes_timing ON ai_prediction_outcomes;
CREATE TRIGGER ai_prediction_outcomes_timing BEFORE INSERT ON ai_prediction_outcomes
    FOR EACH ROW EXECUTE FUNCTION doi_outcome_after_prediction();
DROP TRIGGER IF EXISTS ai_prediction_outcomes_append_only ON ai_prediction_outcomes;
CREATE TRIGGER ai_prediction_outcomes_append_only BEFORE UPDATE OR DELETE ON ai_prediction_outcomes
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();

DROP TRIGGER IF EXISTS ai_meeting_turns_append_only ON ai_meeting_turns;
CREATE TRIGGER ai_meeting_turns_append_only BEFORE UPDATE OR DELETE ON ai_meeting_turns
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
DROP TRIGGER IF EXISTS ai_decisions_append_only ON ai_decisions;
CREATE TRIGGER ai_decisions_append_only BEFORE UPDATE OR DELETE ON ai_decisions
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
DROP TRIGGER IF EXISTS ai_evidence_packs_append_only ON ai_evidence_packs;
CREATE TRIGGER ai_evidence_packs_append_only BEFORE UPDATE OR DELETE ON ai_evidence_packs
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
DROP TRIGGER IF EXISTS ai_disagreements_append_only ON ai_disagreements;
CREATE TRIGGER ai_disagreements_append_only BEFORE UPDATE OR DELETE ON ai_disagreements
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
