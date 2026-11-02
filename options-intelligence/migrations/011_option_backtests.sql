-- Phase 8: option-premium backtests and data provenance classes. Additive.

-- Rolling-option bars need their per-bar strike, underlying spot and IV to price a held contract.
ALTER TABLE market_bars ADD COLUMN IF NOT EXISTS strike numeric(14, 4);
ALTER TABLE market_bars ADD COLUMN IF NOT EXISTS spot numeric(14, 4);
ALTER TABLE market_bars ADD COLUMN IF NOT EXISTS iv numeric(10, 4);

-- Every research result says what kind of data produced it.
ALTER TABLE research_experiments ADD COLUMN IF NOT EXISTS data_class text NOT NULL DEFAULT 'UNSPECIFIED'
    CHECK (data_class IN ('HISTORICAL', 'SIMULATED', 'LIVE_RECORDED', 'UNSPECIFIED'));

CREATE TABLE IF NOT EXISTS research_option_backtests (
    id                  text PRIMARY KEY,
    strategy_version_id text NOT NULL REFERENCES strategy_versions(id),
    data_class          text NOT NULL CHECK (data_class IN ('HISTORICAL', 'SIMULATED', 'LIVE_RECORDED')),
    datasets            jsonb NOT NULL,
    params              jsonb NOT NULL,
    metrics             jsonb NOT NULL,
    metrics_sha256      char(64) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS research_option_backtests_append_only ON research_option_backtests;
CREATE TRIGGER research_option_backtests_append_only BEFORE UPDATE OR DELETE ON research_option_backtests
    FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation();
DROP TRIGGER IF EXISTS research_option_backtests_no_truncate ON research_option_backtests;
CREATE TRIGGER research_option_backtests_no_truncate BEFORE TRUNCATE ON research_option_backtests
    FOR EACH STATEMENT EXECUTE FUNCTION doi_forbid_mutation();
