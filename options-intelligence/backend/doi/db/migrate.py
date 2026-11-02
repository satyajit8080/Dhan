"""Ordered, checksummed SQL migrations.

Each file in migrations/ (NNN_name.sql) runs exactly once, inside its own
transaction, under a Postgres advisory lock so concurrent starts cannot race.
An already-applied file whose content changed is refused (checksum mismatch).

    python -m doi.db.migrate            # uses DATABASE_URL
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

from doi.core.errors import ConfigError

DEFAULT_DIR = Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[3] / "migrations"))
_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_LOCK_ID = 0x0D01_6A11


def discover(directory: Path = DEFAULT_DIR) -> list[tuple[str, Path, str]]:
    out = []
    for p in sorted(directory.glob("*.sql")):
        m = _NAME.match(p.name)
        if not m:
            raise ConfigError(f"bad migration file name: {p.name}")
        out.append((m.group(1), p, hashlib.sha256(p.read_bytes()).hexdigest()))
    versions = [v for v, _, _ in out]
    if len(set(versions)) != len(versions):
        raise ConfigError("duplicate migration version numbers")
    return out


def migrate(dsn: str, directory: Path = DEFAULT_DIR) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY, name text NOT NULL, checksum char(64) NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            done = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
            for version, path, checksum in discover(directory):
                if version in done:
                    if done[version] != checksum:
                        raise ConfigError(f"migration {path.name} changed after it was applied")
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())  # type: ignore[arg-type]
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, path.name, checksum),
                    )
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    applied = migrate(dsn)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
