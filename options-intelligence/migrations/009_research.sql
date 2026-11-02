-- Phase 7.4: strategy research laboratory. Additive.

CREATE TABLE IF NOT EXISTS strategies (
    id                text PRIMARY KEY,
    name              text NOT NULL UNIQUE,
    active_version_id text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id                text PRIMARY KEY,
    strategy_id       text NOT NULL REFERENCES strategies(id),
    version           integer NOT NULL,
    spec              jsonb NOT NULL,
    spec_sha256       char(64) NOT NULL,
    parent_version_id text REFERENCES strategy_versions(id),
    author            text NOT NULL,                 -- human actor or agent id@version
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, version),
    UNIQUE (strategy_id, spec_sha256)
);

CREATE TABLE IF NOT EXISTS strategy_activations (
    seq          bigserial PRIMARY KEY,
    strategy_id  text NOT NULL REFERENCES strategies(id),
    version_id   text NOT NULL REFERENCES strategy_versions(id),
    action       text NOT NULL CHECK (action IN ('PROMOTE', 'ROLLBACK')),
    actor        text NOT NULL,
    approval_seq bigint,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research_hypotheses (
    id                  text PRIMARY KEY,
    statement           text NOT NULL,
    author              text NOT NULL,
    strategy_version_id text NOT NULL REFERENCES strategy_versions(id),
    status              text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research_experiments (
    id                  text PRIMARY KEY,
    strategy_version_id text NOT NULL REFERENCES strategy_versions(id),
    dataset_id          text REFERENCES historical_datasets(id),
    manifest            jsonb NOT NULL,                -- data sha, spec sha, feature config hash, engine/code version, split
    manifest_sha256     char(64) NOT NULL,
    status              text NOT NULL CHECK (status IN ('RUNNING', 'FAILED_IS', 'PASSED_IS', 'FAILED_HOLDOUT', 'PASSED_HOLDOUT',
                                                         'PAPER_VALIDATION', 'PAPER_PASSED', 'PAPER_FAILED')),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research_results (
    experiment_id text NOT NULL REFERENCES research_experiments(id),
    stage         text NOT NULL CHECK (stage IN ('DEVELOPMENT', 'VALIDATION', 'WALK_FORWARD', 'SENSITIVITY', 'COST_STRESS', 'HOLDOUT', 'PAPER')),
    metrics       jsonb NOT NULL,
    metrics_sha256 char(64) NOT NULL,
    passed        boolean NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id, stage)
);

-- The holdout can be opened exactly once per experiment.
CREATE TABLE IF NOT EXISTS research_holdout_access (
    experiment_id text PRIMARY KEY REFERENCES research_experiments(id),
    opened_by     text NOT NULL,
    opened_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS experiment_id text;
ALTER TABLE backtest_runs ADD COLUMN IF NOT EXISTS split_label text;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['strategy_versions', 'strategy_activations', 'research_results', 'research_holdout_access']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION doi_forbid_mutation()', t || '_append_only', t);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_no_truncate', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION doi_forbid_mutation()', t || '_no_truncate', t);
    END LOOP;
END
$$;
