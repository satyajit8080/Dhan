"""Persist FeatureVectors to the existing `market_features` table (migration 001).

Immutability: a vector for (underlying, timeframe, as_of, version_key) is written
once. Writing a different vector under the same key is refused, because it means
non-determinism or a version that wasn't bumped."""

from __future__ import annotations

import json
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from doi.core.canonical import canonical
from doi.core.errors import DoiError
from doi.intelligence.feature_engine.engine import FeatureVector


class FeatureConflict(DoiError):
    pass


class FeatureStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def save(self, vec: FeatureVector) -> bool:
        """True if inserted, False if an identical vector already existed."""
        doc = {**json.loads(canonical(vec.to_dict())), "digest": vec.digest}
        tf = f"{vec.timeframe_min}m"
        with psycopg.connect(self.dsn) as c, c.transaction():
            row = c.execute(
                "INSERT INTO market_features (event_ts, underlying_key, timeframe, feature_json, feature_version,"
                " data_quality_score, coverage_status) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (underlying_key, timeframe, event_ts, feature_version) DO NOTHING RETURNING 1",
                (vec.as_of, vec.underlying_key, tf, Jsonb(doc), vec.version_key, vec.data_quality_score, vec.coverage_status),
            ).fetchone()
            if row is not None:
                return True
            existing = c.execute(
                "SELECT feature_json->>'digest' FROM market_features"
                " WHERE underlying_key = %s AND timeframe = %s AND event_ts = %s AND feature_version = %s",
                (vec.underlying_key, tf, vec.as_of, vec.version_key),
            ).fetchone()
        if existing is None or existing[0] != vec.digest:
            raise FeatureConflict(
                f"different feature vector already stored for {vec.underlying_key} {tf} {vec.as_of} {vec.version_key}"
            )
        return False

    def load(self, underlying_key: str, timeframe_min: int, as_of: datetime, version_key: str) -> dict | None:
        with psycopg.connect(self.dsn) as c:
            row = c.execute(
                "SELECT feature_json FROM market_features WHERE underlying_key = %s AND timeframe = %s AND event_ts = %s"
                " AND feature_version = %s",
                (underlying_key, f"{timeframe_min}m", as_of, version_key),
            ).fetchone()
        return row[0] if row else None
