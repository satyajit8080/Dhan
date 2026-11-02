"""PostgreSQL fixture for persistence tests.

Uses TEST_DATABASE_URL when set (CI). Otherwise starts a throwaway local cluster
if PostgreSQL server binaries are installed. Skips when neither is possible."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import time

import pytest


def _local_cluster():
    bins = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))
    if not bins:
        return None, None
    bindir = os.path.dirname(bins[-1])
    d = tempfile.mkdtemp(prefix="doi-pg-", dir="/var/tmp" if os.path.isdir("/var/tmp") else None)
    port = str(56000 + os.getpid() % 1000)
    as_postgres = os.geteuid() == 0
    if as_postgres:
        shutil.chown(d, "postgres")
    run = (
        (lambda cmd: subprocess.run(["su", "postgres", "-c", cmd], check=True, capture_output=True))
        if as_postgres
        else (lambda cmd: subprocess.run(cmd, shell=True, check=True, capture_output=True))
    )
    run(f"{bindir}/initdb -D {d}/data -A trust -U postgres")
    run(f"{bindir}/pg_ctl -D {d}/data -o '-k {d} -p {port} -c listen_addresses= -c fsync=off' -l {d}/log -w start")
    dsn = f"host={d} port={port} user=postgres dbname=postgres"

    def stop():
        run(f"{bindir}/pg_ctl -D {d}/data -m immediate stop")
        shutil.rmtree(d, ignore_errors=True)

    return dsn, stop


@pytest.fixture(scope="session")
def pg_server():
    dsn = os.environ.get("TEST_DATABASE_URL")
    stop = None
    if not dsn:
        try:
            dsn, stop = _local_cluster()
        except (subprocess.CalledProcessError, OSError, KeyError) as e:
            pytest.skip(f"cannot start local PostgreSQL: {e}")
    if not dsn:
        pytest.skip("no PostgreSQL available (set TEST_DATABASE_URL)")
    psycopg = pytest.importorskip("psycopg")
    for _ in range(50):
        try:
            psycopg.connect(dsn).close()
            break
        except psycopg.OperationalError:
            time.sleep(0.1)
    yield dsn
    if stop:
        stop()


@pytest.fixture
def pg_dsn(pg_server):
    """Fresh, fully migrated schema per test."""
    import psycopg

    from doi.db.migrate import migrate

    with psycopg.connect(pg_server, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(pg_server)
    return pg_server
