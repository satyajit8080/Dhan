-- Phase 7.2: agent registry with immutable versions. Additive.

CREATE TABLE IF NOT EXISTS ai_agents (
    id             text PRIMARY KEY,
    name           text NOT NULL,
    responsibility text NOT NULL,
    tier           text NOT NULL CHECK (tier IN ('fast', 'reasoning')),
    scored_on      text NOT NULL,
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ai_agent_versions (
    agent_id      text NOT NULL REFERENCES ai_agents(id),
    version       text NOT NULL,                  -- sha256 prefix of instructions+schema+scope+tier+limits
    instructions  text NOT NULL,
    policy        text NOT NULL,
    sections      text[] NOT NULL,
    output_schema jsonb NOT NULL,
    tier          text NOT NULL,
    max_tokens    integer NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, version)
);
DROP TRIGGER IF EXISTS ai_agent_versions_append_only ON ai_agent_versions;
CREATE TRIGGER ai_agent_versions_append_only BEFORE UPDATE OR DELETE ON ai_agent_versions
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
