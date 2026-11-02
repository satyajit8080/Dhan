-- Phase 7.0B: market-data recording and deterministic replay. Additive only.

-- Exact snapshot payload (canonical JSON of normalized rows) for bit-exact replay;
-- option_chain_rows keeps the queryable, rounded copy.
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS content_sha256 char(64);
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE option_chain_snapshots ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'dhan_rest';
CREATE UNIQUE INDEX IF NOT EXISTS option_chain_snapshots_dedupe
    ON option_chain_snapshots (underlying_key, expiry, received_at, content_sha256);

-- One tick per (security, event time); feed redeliveries are ignored.
CREATE UNIQUE INDEX IF NOT EXISTS market_ticks_dedupe ON market_ticks (security_id, event_ts);

CREATE TABLE IF NOT EXISTS historical_sources (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('DHAN', 'NSE_ARCHIVE', 'BSE_ARCHIVE', 'VENDOR', 'RECORDED')),
    licence     text NOT NULL,
    url         text,
    verified    boolean NOT NULL DEFAULT false,   -- availability/licence externally verified
    verified_at timestamptz,
    notes       text
);

CREATE TABLE IF NOT EXISTS historical_datasets (
    id               text PRIMARY KEY,
    source_id        text NOT NULL REFERENCES historical_sources(id),
    underlying_key   text NOT NULL,
    instrument_class text NOT NULL,              -- INDEX_BARS / ROLLING_OPTION / CHAIN_SNAPSHOTS
    series_key       text NOT NULL,              -- market_bars.security_id or snapshot selector
    granularity      text NOT NULL,
    date_from        date NOT NULL,
    date_to          date NOT NULL,
    audit_id         bigint REFERENCES data_quality_audits(id),
    status           text NOT NULL CHECK (status IN ('APPROVED', 'APPROVED_WITH_GAPS', 'QUARANTINED', 'UNSUPPORTED')),
    manifest         jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (date_to >= date_from)
);
CREATE INDEX IF NOT EXISTS historical_datasets_lookup ON historical_datasets (underlying_key, instrument_class, status);

INSERT INTO historical_sources (id, name, kind, licence, url, verified, notes) VALUES
  ('dhan_rest', 'DhanHQ v2 Data API', 'DHAN', 'Dhan Data API subscription terms', 'https://dhanhq.co/docs/v2/', false,
   'Depth of history per endpoint UNVERIFIED (docs host not reachable from build environment)'),
  ('recorded', 'Platform recorder (live capture)', 'RECORDED', 'Derived from Dhan data under the same terms', NULL, true,
   'Data captured by doi.data.recorder')
ON CONFLICT (id) DO NOTHING;
