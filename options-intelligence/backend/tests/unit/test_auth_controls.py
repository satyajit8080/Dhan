"""Phase 7.0C: API keys and roles, append-only hash-chained approvals, persistent kill switch."""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from doi.app.main import create_app
from doi.app.runtime import init_paper
from doi.core.auth import ApprovalChainBroken, ApprovalLog, AuthStore, ControlStore, Principal, hash_token, main
from doi.core.config import Settings
from doi.core.logging import RedactingFilter


def test_keys_roles_and_revocation(pg_dsn):
    store = AuthStore(pg_dsn)
    uid = store.create_user("Alice@Example.com")
    key_id, token = store.create_key(uid, "approver", "laptop")
    with psycopg.connect(pg_dsn) as c:
        stored = c.execute("SELECT key_hash FROM api_keys WHERE id = %s", (key_id,)).fetchone()[0]
    assert stored == hash_token(token) and token not in stored  # only the hash is stored
    who = store.authenticate(token)
    assert who.actor == "alice@example.com" and who.role == "approver" and who.has("viewer") and not who.has("admin")
    assert store.authenticate("doi_wrong") is None and store.authenticate(None) is None and store.authenticate("x" * 300) is None
    store.revoke(key_id)
    assert store.authenticate(token) is None
    with psycopg.connect(pg_dsn) as c:
        c.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (uid,))
    _, t2 = store.create_key(uid, "admin")
    assert store.authenticate(t2) is None  # disabled users can't authenticate


def test_approval_log_chain_and_append_only(pg_dsn):
    log = ApprovalLog(pg_dsn)
    who = Principal(AuthStore(pg_dsn).create_user("b@x"), "b@x", "approver")
    for i in range(3):
        log.append(who, "APPROVE", "ai_recommendation", f"REC-{i}", "ok", {"i": i})
    assert log.verify() == 3 and [r.subject_id for r in log.records("ai_recommendation")] == ["REC-0", "REC-1", "REC-2"]
    with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("DELETE FROM approval_records")
    with psycopg.connect(pg_dsn) as c:
        c.execute("SET session_replication_role = replica")  # superuser bypass
        c.execute("UPDATE approval_records SET note = 'forged' WHERE seq = 2")
    with pytest.raises(ApprovalChainBroken):
        log.verify()


def test_concurrent_approvals_keep_chain(pg_dsn):
    log = ApprovalLog(pg_dsn)
    who = Principal(None, "svc", "admin")

    async def go():
        await asyncio.gather(*(asyncio.to_thread(log.append, who, "TEST_APPROVAL", "t", str(i)) for i in range(20)))

    asyncio.run(go())
    assert log.verify() == 20


def _boot(pg_dsn):
    from tests.unit.test_paper_integration import runtime

    rt = runtime()
    asyncio.run(init_paper(rt, Settings(database_url=pg_dsn, holidays_file=None), datetime.now(UTC) - timedelta(seconds=5)))
    # tests must not depend on wall-clock market hours (same overrides as runtime())
    from doi.paper.engine import ExecutionPolicy
    from doi.risk.models import RiskConfig
    from tests.unit.test_paper_integration import AlwaysOpen

    rt.paper.gate.calendar = AlwaysOpen()
    rt.paper.cal = rt.paper.gate.calendar
    rt.paper.gate.config = RiskConfig(last_entry_time=time(23, 59, 59))
    rt.paper.policy = ExecutionPolicy(square_off=None)
    return rt


def test_api_roles_enforced_when_database_configured(pg_dsn):
    rt = _boot(pg_dsn)
    c = TestClient(create_app(runtime=rt))
    keys = {r: rt.auth.create_key(rt.auth.create_user(f"{r}@x"), r)[1] for r in ("viewer", "approver", "admin")}
    h = lambda r: {"Authorization": f"Bearer {keys[r]}"}
    assert c.get("/api/v1/paper/portfolio").status_code == 401
    assert c.get("/api/v1/paper/portfolio", headers=h("viewer")).status_code == 200
    assert c.get("/api/v1/auth/whoami", headers=h("approver")).json() == {
        "actor": "approver@x",
        "role": "approver",
        "dev_mode": False,
    }
    assert c.post("/api/v1/admin/kill-switch", json={"on": True}, headers=h("approver")).status_code == 403
    assert c.post("/api/v1/paper/positions/POS-1/close", headers=h("viewer")).status_code == 403
    assert c.get("/healthz").status_code == 200  # health stays public


def test_kill_switch_persists_across_restart_and_blocks_paper(pg_dsn):
    rt = _boot(pg_dsn)
    c = TestClient(create_app(runtime=rt))
    _, admin = rt.auth.create_key(rt.auth.create_user("root@x"), "admin")
    r = c.post("/api/v1/admin/kill-switch", json={"on": True, "note": "drill"}, headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200 and rt.kill_switch and rt.paper.kill_switch
    assert ControlStore(pg_dsn, ApprovalLog(pg_dsn)).kill_switch() is True
    rt2 = _boot(pg_dsn)  # restart: switch restored from the database before anything runs
    assert rt2.kill_switch and rt2.paper.kill_switch
    acts = [x.action for x in ApprovalLog(pg_dsn).records("system")]
    assert acts == ["KILL_SWITCH_ON"]
    _, appr = rt2.auth.create_key(rt2.auth.create_user("ap@x"), "approver")
    from tests.unit.test_paper_integration import ORDER

    res = TestClient(create_app(runtime=rt2)).post(
        "/api/v1/paper/orders", json=ORDER, headers={"Authorization": f"Bearer {appr}"}
    )
    assert res.status_code == 422 and any("kill_switch" in x for x in res.json()["detail"]["reasons"])


def test_dev_mode_without_database_is_explicit():
    from tests.unit.test_paper_integration import runtime

    c = TestClient(create_app(runtime=runtime()))
    assert c.get("/api/v1/auth/whoami").json()["dev_mode"] is True


def test_tokens_redacted_and_cli(pg_dsn, monkeypatch, capsys):
    flt = RedactingFilter()
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "Authorization: Bearer doi_abcdefgh12345678", (), None)
    flt.filter(rec)
    assert "doi_abcdefgh12345678" not in rec.msg
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    assert main(["create-key", "--email", "cli@x", "--role", "viewer"]) == 0
    out = capsys.readouterr().out
    token = out.split("token (shown once, store it securely): ")[1].strip()
    assert AuthStore(pg_dsn).authenticate(token).role == "viewer"
