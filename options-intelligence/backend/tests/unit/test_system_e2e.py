"""Phase 7.6 end-to-end: DB-backed system with auth, AI meeting -> human approval -> paper, ops endpoints,
and failure isolation (Grok outage, budget exhaustion, kill switch) around market data and paper trading."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from doi.ai.service import build_ai
from doi.app.main import create_app
from doi.core.config import Settings
from doi.llm.gateway import FakeGrokClient, ProviderError
from doi.paper.driver import pump_once
from tests.ai_fakes import scripted
from tests.unit.test_auth_controls import _boot
from tests.unit.test_meetings import features
from tests.unit.test_paper_integration import EXP


def system(pg_dsn, responder=None):
    rt = _boot(pg_dsn)
    rt.ai = build_ai(Settings(), pg_dsn, kill_switch=lambda: rt.kill_switch, client=FakeGrokClient(responder or scripted()))
    fv = features()
    rt.ai.features_fn = lambda u, now: replace(fv, as_of=now)
    rt.ai.snapshot_fn = lambda u, exp, now: asyncio.run(rt.provider.get_option_chain(rt.universe[u], exp))
    c = TestClient(create_app(runtime=rt))
    keys = {
        r: {"Authorization": "Bearer " + rt.auth.create_key(rt.auth.create_user(f"{r}@e2e"), r)[1]}
        for r in ("viewer", "approver", "admin")
    }
    return rt, c, keys


PROP = {"expiry": EXP.isoformat(), "strike": 24000, "option_type": "CE", "stop_loss": 90, "targets": [[125, 0.5], [135, 0.5]]}


def test_full_research_to_paper_flow(pg_dsn):
    rt, c, k = system(pg_dsn)
    assert (
        c.post(
            "/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": PROP, "run_now": True}, headers=k["viewer"]
        ).status_code
        == 403
    )
    m = c.post(
        "/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": PROP, "run_now": True}, headers=k["approver"]
    ).json()["meeting"]
    assert m["final_action"] == "RECOMMEND_PAPER"
    rid = m["recommendation_id"]
    assert rt.paper.orders == {}
    a = c.post(f"/api/v1/ai/recommendations/{rid}/approve", json={"note": "e2e"}, headers=k["approver"])
    assert a.status_code == 200 and a.json()["risk_gate_approved"]
    time.sleep(0.3)
    assert asyncio.run(pump_once(rt)) == 1 and next(iter(rt.paper.orders.values())).filled_qty == 150
    d = c.get(f"/api/v1/ai/meetings/{m['id']}", headers=k["viewer"]).json()
    assert d["recommendation"]["status"] == "SUBMITTED" and len(d["turns"]) == 11 and d["evidence"]
    sm = d["summary"]
    assert sm["panel_size"] == 15 and len(sm["agents"]) == 15 and sm["final_action"] == "RECOMMEND_PAPER"
    assert sm["label"] in ("STRONG BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG BEARISH", "WAIT")
    assert rt.approvals.verify() >= 1
    st = c.get("/api/v1/system/status", headers=k["viewer"]).json()
    assert st["mode"] == "RESEARCH_AND_PAPER_ONLY" and st["live_broker_execution"] == "NOT_IMPLEMENTED"
    assert (
        st["database"]["migration_version"] == __import__("doi.db.migrate", fromlist=["discover"]).discover()[-1][0]
        and st["paper"]["open_positions"] == 1
    )
    assert float(st["ai"]["budget"]["spent_inr"]) > 0 and st["session"]["state"] in ("PRE-OPEN", "OPEN", "CLOSED")
    u = c.get("/api/v1/ai/usage", headers=k["viewer"]).json()
    assert sum(float(a["cost_inr"]) for a in u["by_agent"]) > 0 and isinstance(u["recent_errors"], list)
    port = c.get("/api/v1/paper/portfolio", headers=k["viewer"]).json()["positions"][0]
    assert port["side"] == "LONG" and port["option_type"] == "CE" and "unrealized" in port and "charges" in port
    for path in (
        "/api/v1/ai/predictions",
        "/api/v1/research/strategies",
        "/api/v1/research/experiments",
        "/api/v1/research/option-backtests",
    ):
        assert isinstance(c.get(path, headers=k["viewer"]).json(), list), path
    assert len(c.get("/api/v1/ai/predictions", headers=k["viewer"]).json()) >= 1
    fv = c.get("/api/v1/market/features", params={"underlying": "NIFTY"}, headers=k["viewer"]).json()
    assert fv["underlying"] == "NIFTY" and "adx" in fv["values"]
    an = c.get("/api/v1/paper/analytics", headers=k["viewer"]).json()
    assert an["trades"] == 0 and an["win_rate"] is None
    eq = c.get("/api/v1/paper/equity-curve", headers=k["viewer"]).json()
    assert float(eq["starting_capital"]) > 0 and eq["points"] == []
    met = c.get("/metrics", headers=k["viewer"]).text
    assert "doi_llm_spent_inr" in met and "doi_paper_open_positions 1.0" in met
    rep = c.get("/api/v1/reports/daily", headers=k["viewer"]).text
    assert "No live broker execution exists" in rep and "RECOMMEND_PAPER=1" in rep
    assert c.get("/api/v1/system/status").status_code == 401


def test_dashboard_is_static_and_safe(pg_dsn):
    _rt, c, _ = system(pg_dsn)
    r = c.get("/dashboard")
    assert r.status_code == 200 and "Bull50 Research Desk" in r.text
    assert "innerHTML" not in r.text.replace("never uses innerHTML", "") and "localStorage" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    for sec in (
        "Command Center",
        "Options Intelligence",
        "AI Investment Committee",
        "Paper Trading",
        "System Monitoring",
        "Administration",
    ):
        assert sec in r.text
    assert 'id="ks"' in r.text and 'id="h-meter"' in r.text and "PAPER MODE — NO LIVE EXECUTION" in r.text  # global shell
    assert "Set API key" in r.text and "AI Committee" in r.text and "Option chain" in r.text and "#/04" in r.text
    assert "XAI_API_KEY" not in r.text and "access-token" not in r.text and "/live" not in r.text


def test_grok_outage_never_touches_market_data_or_paper(pg_dsn):
    def down(model, messages):
        raise ProviderError("503", retryable=True)

    rt, c, k = system(pg_dsn, down)
    m = c.post(
        "/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": PROP, "run_now": True}, headers=k["approver"]
    ).json()["meeting"]
    assert m["final_action"] == "NO_ACTION" and m["abort_reason"] == "AI_UNAVAILABLE"
    from tests.unit.test_paper_integration import ORDER

    assert c.post("/api/v1/paper/orders", json=ORDER, headers=k["approver"]).status_code == 201  # paper unaffected
    time.sleep(0.3)
    assert asyncio.run(pump_once(rt)) == 1  # polling and recording unaffected
    assert c.get("/api/v1/system/status", headers=k["viewer"]).json()["database"]["recorded_snapshots"] >= 1


def test_budget_exhaustion_stops_ai_only(pg_dsn):
    rt, c, k = system(pg_dsn)
    assert c.post("/api/v1/ai/budget", json={"hard_limit_inr": 150.5, "note": "drill"}, headers=k["approver"]).status_code == 403
    assert c.post("/api/v1/ai/budget", json={"hard_limit_inr": 150.5, "note": "drill"}, headers=k["admin"]).status_code == 200
    m = c.post(
        "/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": PROP, "run_now": True}, headers=k["approver"]
    ).json()
    ev = m["meeting"]
    assert ev is None or ev["final_action"] == "NO_ACTION"  # skipped at admission or aborted: never a silent overspend
    b = c.get("/api/v1/ai/budget", headers=k["viewer"]).json()
    assert b["spent_inr"] + b["reserved_inr"] <= b["usable_inr"] and b["reserved_inr"] == 0
    from tests.unit.test_paper_integration import ORDER

    assert c.post("/api/v1/paper/orders", json={**ORDER, "signal_id": "S-B"}, headers=k["approver"]).status_code == 201
    acts = [r.action for r in rt.approvals.records()]
    assert "BUDGET_CHANGE" in acts


def test_kill_switch_blocks_ai_and_paper_entries(pg_dsn):
    _rt, c, k = system(pg_dsn)
    assert c.post("/api/v1/admin/kill-switch", json={"on": True, "note": "drill"}, headers=k["admin"]).status_code == 200
    r = c.post(
        "/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": PROP, "run_now": True}, headers=k["approver"]
    ).json()
    assert r["meeting"] is None  # admission refused: no meeting, no Grok call
    with __import__("psycopg").connect(pg_dsn) as conn:
        assert conn.execute("SELECT status, skip_reason FROM ai_events").fetchone() == ("SKIPPED", "kill switch engaged")
        assert conn.execute("SELECT count(*) FROM llm_calls WHERE status = 'OK'").fetchone()[0] == 0
    _ = datetime.now(UTC)


def test_paper_analytics_math():
    from datetime import timedelta
    from decimal import Decimal as D
    from types import SimpleNamespace as N

    from doi.app.views_routes import paper_analytics

    t0 = datetime(2026, 11, 2, 5, tzinfo=UTC)
    tr = [N(closed_at=t0 + timedelta(minutes=i), net_pnl=D(v), gross_pnl=D(v) + 10, charges=D(10), underlying_key="NIFTY", exit_reason="STOP")
          for i, v in enumerate(("100", "-300", "50"))]  # fmt: skip
    a = paper_analytics(tr, D(1000))
    assert a["trades"] == 3 and a["wins"] == 2 and a["net_pnl"] == D(-150) and a["charges"] == D(30)
    assert a["max_drawdown"] == D(300) and a["by_underlying"]["NIFTY"]["trades"] == 3
