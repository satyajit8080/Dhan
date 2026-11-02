-- Phase 7.5: agent scorecard history (append-only). Additive.

CREATE TABLE IF NOT EXISTS ai_agent_scorecards (
    computed_at   timestamptz NOT NULL,
    mode          text NOT NULL CHECK (mode IN ('LIVE', 'REPLAY')),
    agent_id      text NOT NULL,
    agent_version text NOT NULL,              -- '*' = all versions of the agent
    n             integer NOT NULL,
    metrics       jsonb NOT NULL,
    rank          integer,
    provisional   boolean NOT NULL,
    PRIMARY KEY (computed_at, mode, agent_id, agent_version)
);
DROP TRIGGER IF EXISTS ai_agent_scorecards_append_only ON ai_agent_scorecards;
CREATE TRIGGER ai_agent_scorecards_append_only BEFORE UPDATE OR DELETE ON ai_agent_scorecards
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
DROP TRIGGER IF EXISTS ai_agent_scorecards_no_truncate ON ai_agent_scorecards;
CREATE TRIGGER ai_agent_scorecards_no_truncate BEFORE TRUNCATE ON ai_agent_scorecards
    FOR EACH STATEMENT EXECUTE FUNCTION doi_forbid_mutation();
