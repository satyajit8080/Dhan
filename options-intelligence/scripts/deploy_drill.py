"""Controlled deployment drill (no Docker daemon needed).

Starts a throwaway PostgreSQL, then:
  1. runs migrations twice through the CLI;
  2. creates an admin key through the CLI;
  3. launches the API with uvicorn, exactly as the Docker CMD does;
  4. exercises auth, the dashboard, status and the kill switch;
  5. crashes the API with SIGKILL, restarts it, and checks the persisted state;
  6. scans the logs and a pg_dump for every secret.
Prints a JSON report. No Dhan or xAI network calls are made (no client id; AI disabled).

    python scripts/deploy_drill.py
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from tests.conftest import _local_cluster  # noqa: E402


def main() -> int:
    dsn_admin, stop = _local_cluster()
    report: dict = {"steps": []}
    proc = None
    logdir = Path(tempfile.mkdtemp(prefix="doi-drill-"))
    try:
        pw = "Drill-" + secrets.token_hex(8)
        with psycopg.connect(dsn_admin, autocommit=True) as c:
            c.execute(f"CREATE ROLE doi LOGIN PASSWORD '{pw}'")
            c.execute("CREATE DATABASE doi OWNER doi")
        host = dict(x.split("=") for x in dsn_admin.split())["host"]
        port = dict(x.split("=") for x in dsn_admin.split())["port"]
        dsn = f"postgresql://doi:{pw}@/doi?host={host}&port={port}"
        fake_xai = "xai-" + secrets.token_hex(16)
        env = {**os.environ, "DATABASE_URL": dsn, "XAI_API_KEY": fake_xai, "AI_ENABLED": "false", "HOLIDAYS_FILE": "",
               "PYTHONPATH": str(ROOT / "backend")}  # fmt: skip
        env.pop("DHAN_CLIENT_ID", None)
        run = lambda *a: subprocess.run(
            [sys.executable, *a], env=env, capture_output=True, text=True, cwd=ROOT / "backend", timeout=120
        )
        m1, m2 = run("-m", "doi.db.migrate"), run("-m", "doi.db.migrate")
        report["steps"].append(
            {
                "migrations_first": m1.stdout.strip(),
                "migrations_second": m2.stdout.strip(),
                "ok": m1.returncode == m2.returncode == 0 and "up to date" in m2.stdout,
            }
        )
        k = run("-m", "doi.core.auth", "create-key", "--email", "drill@example.com", "--role", "admin", "--label", "drill")
        token = k.stdout.split("token (shown once, store it securely): ")[1].strip()
        report["steps"].append({"create_key": k.returncode == 0})

        def start(tag: str):
            log = open(logdir / f"api-{tag}.log", "w")
            p = subprocess.Popen([sys.executable, "-m", "uvicorn", "doi.app.main:create_app", "--factory", "--port", "18765", "--log-level", "info"],
                                 env=env, cwd=ROOT / "backend", stdout=log, stderr=subprocess.STDOUT)  # fmt: skip
            for _ in range(100):
                try:
                    if httpx.get("http://127.0.0.1:18765/healthz", timeout=1).status_code == 200:
                        return p
                except httpx.HTTPError:
                    time.sleep(0.2)
            raise RuntimeError("API did not start: " + (logdir / f"api-{tag}.log").read_text()[-2000:])

        proc = start("1")
        base, h = "http://127.0.0.1:18765", {"Authorization": f"Bearer {token}"}
        checks = {
            "healthz": httpx.get(f"{base}/healthz").status_code,
            "status_without_key": httpx.get(f"{base}/api/v1/system/status").status_code,
            "status_bad_key": httpx.get(
                f"{base}/api/v1/system/status", headers={"Authorization": "Bearer doi_" + "x" * 40}
            ).status_code,
            "status_with_key": httpx.get(f"{base}/api/v1/system/status", headers=h).status_code,
            "whoami": httpx.get(f"{base}/api/v1/auth/whoami", headers=h).json(),
            "dashboard": httpx.get(f"{base}/dashboard").status_code,
            "dashboard_csp": "frame-ancestors 'none'"
            in httpx.get(f"{base}/dashboard").headers.get("content-security-policy", ""),
            "metrics": httpx.get(f"{base}/metrics", headers=h).status_code,
            "kill_switch_on": httpx.post(
                f"{base}/api/v1/admin/kill-switch", json={"on": True, "note": "drill"}, headers=h
            ).json(),
        }
        status = httpx.get(f"{base}/api/v1/system/status", headers=h).json()
        checks["migration_version"] = status["database"]["migration_version"]
        before = httpx.get(f"{base}/api/v1/paper/portfolio", headers=h).json()["ledger_events"]
        report["steps"].append({"api_checks": checks})

        proc.send_signal(signal.SIGKILL)  # crash, no graceful shutdown
        proc.wait(timeout=10)
        proc = start("2")
        after = {
            "kill_switch_after_restart": httpx.get(f"{base}/api/v1/admin/kill-switch", headers=h).json(),
            "ledger_events_before_crash": before,
            "ledger_events_after_restart": httpx.get(f"{base}/api/v1/paper/portfolio", headers=h).json()[
                "ledger_events"
            ],  # +1 RECOVERY
            "approvals": [r["action"] for r in httpx.get(f"{base}/api/v1/admin/approvals", headers=h).json()["records"]],
        }
        # the ledger gains a RECOVERY event on restart, so its head changes. Verify continuity instead:
        from doi.app.runtime import PAPER_ACCOUNT, build_gate
        from doi.core.auth import ApprovalLog
        from doi.core.config import DEFAULT_STRATEGY_DIR
        from doi.intelligence.strategy_configs.loader import load_index_configs
        from doi.paper.reconcile import reconcile
        from doi.paper.store import PaperStore

        after["approval_chain_records"] = ApprovalLog(dsn).verify()
        restored = PaperStore(dsn).load(PAPER_ACCOUNT, build_gate(load_index_configs(DEFAULT_STRATEGY_DIR)))
        after["paper_ledger_verified_and_reconciled"] = reconcile(restored) == []
        report["steps"].append({"restart": after})
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = None

        dump = subprocess.run(["su", "postgres", "-c", f"pg_dump -h {host} -p {port} doi"] if os.geteuid() == 0 else ["pg_dump", "-h", host, "-p", port, "doi"],
                              capture_output=True, text=True, timeout=120).stdout  # fmt: skip
        logs = "".join(p.read_text() for p in logdir.glob("*.log"))
        report["steps"].append({"log_tracebacks": logs.count("Traceback")})
        leaks = {
            name: (s in logs, s in dump) for name, s in {"db_password": pw, "xai_key": fake_xai, "admin_token": token}.items()
        }
        report["steps"].append({"secret_scan": {k: {"in_logs": v[0], "in_db_dump": v[1]} for k, v in leaks.items()},
                                "log_bytes": len(logs), "dump_bytes": len(dump)})  # fmt: skip
        report["ok"] = not any(a or b for a, b in leaks.values()) and logs.count("Traceback") == 0
    finally:
        if proc is not None:
            proc.kill()
        stop()
    print(json.dumps(report, indent=1, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
