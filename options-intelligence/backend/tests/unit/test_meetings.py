"""Phase 7.3: meetings, vetoes, disagreements, resume, permanent memory, human approval bridge."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest
from fastapi.testclient import TestClient

from doi.ai.meetings import MeetingContext, MeetingEngine, MemoryMeetingStore, PgMeetingStore
from doi.ai.repository import meeting_detail
from doi.intelligence.feature_engine.engine import FeatureEngine
from doi.llm.budget import MemoryBudget
from doi.llm.gateway import FakeGrokClient, LLMGateway, MemoryCallLog
from tests.ai_fakes import claim, scripted
from tests.unit.test_feature_engine import AS_OF, inp, snapshot

NOW = AS_OF
PROPOSAL = {
    "expiry": "2026-11-05",
    "strike": 24000,
    "option_type": "CE",
    "stop_loss": 90,
    "targets": [[125, 0.5], [135, 0.5]],
    "valid_for_s": 300,
}
RISK_OK = {"approved": True, "lots": 2, "reasons": []}


def features(stale=False):
    s = snapshot(AS_OF - timedelta(seconds=60 if stale else 2))
    return FeatureEngine().calculate(inp(snapshots=(s,)), AS_OF)


def ctx(**kw):
    base = dict(
        event={"kind": "HUMAN_REQUEST"},
        underlying_key="NIFTY",
        features=features(),
        proposal=PROPOSAL,
        risk_dry_run=RISK_OK,
        mode="REPLAY",
    )
    return MeetingContext(**{**base, **kw})


def engine(overrides=None, budget=None, store=None, kill=lambda: False):
    g = LLMGateway(FakeGrokClient(scripted(overrides)), budget or MemoryBudget(), MemoryCallLog(), kill_switch=kill)
    return MeetingEngine(g, store or MemoryMeetingStore())


def run(e, c=None, mid=None):
    return asyncio.run(e.run(c or ctx(), NOW, mid))


def test_full_meeting_recommends_and_records_everything():
    e = engine()
    r = run(e)
    assert r.status == "DECIDED" and r.final_action == "RECOMMEND_PAPER" and r.recommendation_id and not r.overrides
    rounds = [rnd for rnd, _ in e.store.turn_rows[r.meeting_id]]
    assert rounds == ["R1"] * 7 + ["CONTRARIAN", "RISK", "CIO", "COMPLIANCE"]
    assert len(r.prediction_ids) >= 5 and all(p["created_at"] < p["resolve_at"] for p in e.store.predictions)
    rec = e.store.recommendations[r.recommendation_id]
    assert rec["strike"] == 24000 and rec["valid_until"] == NOW + timedelta(seconds=300)
    assert r.pack.id in e.store.packs and r.cost_inr > 0
    d = meeting_detail(e.store, r.meeting_id)
    assert d["decision"]["final_action"] == "RECOMMEND_PAPER" and d["evidence"]["sha256"] == r.pack.sha256


def test_live_ok_meeting_passes_quorum_when_specialists_paraphrase_evidence():
    """NIFTY committee: horizons, rounded IV/theta, and scientific gamma are traceable.

    A bare truncated gamma prefix is not. The retry that quotes scientific notation
    is what lets volatility finish OK, so the meeting is not aborted for QUORUM.
    """
    from doi.intelligence.feature_engine.engine import FeatureValue, FeatureVector, Provenance, Status

    prov = Provenance("t", (), ("t",))

    def fv(v):
        return FeatureValue(v, Status.OK, prov)

    vector = FeatureVector(
        underlying_key="NIFTY",
        timeframe_min=5,
        as_of=NOW,
        engine_version="fe-1",
        config_version="fc-1",
        config_hash="abc",
        inputs_digest="d",
        data_quality_score=100.0,
        coverage_status="LIVE_OK",
        values={
            "pcr_oi": fv(0.92),
            "pcr_oi_chg_15m": fv(0.4),
            "pcr_oi_chg_60m": fv(-0.2),
            "atm_strike": fv(24350.0),
            "atm_iv_b76": fv(26.6),
            "atm_theta": fv(-158.53),
            "gamma_exposure_proxy": fv(-337_281_588_530.9951),
            "forward_strikes_used": fv(15),
            "atm_spread_pct": fv(0.8),
            "trend_regime": fv("RANGE"),
            "adx": fv(22.0),
        },
    )
    calls = {"vol": 0}

    def flow(_ev):
        return {
            "stance": "BULL",
            "confidence": 3,
            "claims": [{"text": "15-minute and 60m PCR are context, not a magnet", "evidence_refs": ["oi.pcr_oi_chg_15m"]}],
            "prediction": {"target": "INDEX", "direction": "UP", "horizon_min": 30},
        }

    def vol(_ev):
        calls["vol"] += 1
        text = "gamma is -337" if calls["vol"] == 1 else "gamma proxy -3.373e11, IV about 27, theta -159"
        return {
            "stance": "NEUTRAL",
            "confidence": 3,
            "claims": [{"text": text, "evidence_refs": ["volatility.gamma_exposure_proxy", "volatility.atm_iv_b76"]}],
            "prediction": {"target": "INDEX", "direction": "FLAT", "horizon_min": 30},
        }

    e = engine({"options_flow": flow, "volatility": vol})
    r = run(e, ctx(features=vector))
    r1 = {t.agent_id: t for rnd, t in e.store.turn_rows[r.meeting_id] if rnd == "R1"}
    assert r1["options_flow"].status == "OK"
    assert r1["volatility"].status == "OK" and len(r1["volatility"].call_ids) == 2
    assert {r1[a].status for a in ("regime", "options_flow", "volatility", "liquidity")} == {"OK"}
    assert r.abort_reason != "QUORUM"


def test_data_quality_gate_aborts_without_llm_calls():
    e = engine()
    r = run(e, ctx(features=features(stale=True)))
    assert r.status == "ABORTED" and r.abort_reason == "DATA_QUALITY" and e.gateway.client.calls == []
    assert run(e, ctx(features=None)).abort_reason == "DATA_QUALITY"


def test_quorum_failure():
    r = run(engine({"options_flow": lambda ev: "garbage"}))
    assert r.abort_reason == "QUORUM" and r.final_action == "NO_ACTION"


def test_deterministic_risk_gate_veto_overrides_cio():
    r = run(engine(), ctx(risk_dry_run={"approved": False, "reasons": ["spread"]}))
    assert r.final_action == "NO_ACTION" and any("RiskGate" in o for o in r.overrides) and r.recommendation_id is None


def test_ai_risk_officer_veto_overrides_cio():
    r = run(engine({"risk_officer": {"veto": True}}))
    assert r.final_action == "NO_ACTION" and any("Risk Officer veto" in o for o in r.overrides)


def test_contrarian_veto_needs_cio_rebuttal():
    r = run(engine({"contrarian": {"veto": True}}))
    assert r.final_action == "WATCH" and any("contrarian veto" in o for o in r.overrides)
    assert any(d["topic"] == "contrarian_veto" for d in r.disagreements)

    def cio_with_rebuttal(ev):
        c = claim(ev, ("regime",))
        return {
            "action": "RECOMMEND_PAPER",
            "confidence": 3,
            "rationale": [c],
            "dissent_addressed": [c],
            "rebuttal_to_contrarian": c,
        }

    r2 = run(engine({"contrarian": {"veto": True}, "cio": cio_with_rebuttal}))
    assert r2.final_action == "RECOMMEND_PAPER"


def test_compliance_failure_and_missing_proposal():
    assert run(engine({"compliance": {"passed": False, "violations": ["no evidence"]}})).final_action == "WATCH"
    r = run(engine(), ctx(proposal=None, risk_dry_run=None))
    assert r.final_action == "WATCH" and any("no Python proposal" in o for o in r.overrides)


def test_disagreement_triggers_rebuttal_round():
    def bear(ev):
        return {
            "stance": "BEAR",
            "confidence": 4,
            "claims": [claim(ev, ("oi",))],
            "prediction": {"target": "INDEX", "direction": "DOWN", "horizon_min": 30},
        }

    e = engine({"options_flow": bear})
    r = run(e)
    d = next(x for x in r.disagreements if x["topic"] == "direction")
    assert "options_flow" in d["agents"] and "regime" in d["agents"] and d["resolution"]
    assert "R2" in [rnd for rnd, _ in e.store.turn_rows[r.meeting_id]]


def test_independence_of_contexts():
    seen = {}

    def spy(model, messages):
        role = messages[0]["content"].split("ROLE: ")[1].split(".")[0]
        ev = json.loads(messages[1]["content"].split("<evidence>\n")[1].split("\n</evidence>")[0])
        seen.setdefault(role, []).append(ev)
        return scripted()(model, messages)

    g = LLMGateway(FakeGrokClient(spy), MemoryBudget(), MemoryCallLog())
    asyncio.run(MeetingEngine(g, MemoryMeetingStore()).run(ctx(), NOW))
    assert all("turns" not in ev for ev in seen["Market Regime Analyst"])  # specialists never see each other
    risk_turns = (
        [t["agent"] for t in seen["AI Risk Officer"][0].get("turns", {}).values()]
        if "turns" in seen["AI Risk Officer"][0]
        else []
    )
    assert "cio" not in risk_turns  # the risk officer decides before, and independently of, the CIO
    con_turns = [t["agent"] for t in seen["Contrarian Analyst"][0]["turns"].values()]
    assert "cio" not in con_turns and "regime" in con_turns
    assert "oi" not in seen["Market Regime Analyst"][0]


def test_budget_exhaustion_mid_meeting_is_graceful():
    tiny = MemoryBudget(D("0.6"), D("0.05"))
    r = run(engine(budget=tiny))
    assert (
        r.status == "ABORTED"
        and r.abort_reason in ("AI_UNAVAILABLE", "QUORUM", "CIO_UNAVAILABLE")
        and r.final_action == "NO_ACTION"
    )
    st = tiny.status()
    assert st.spent_inr + st.reserved_inr <= st.usable_inr


def test_kill_switch_stops_meeting():
    r = run(engine(kill=lambda: True))
    assert r.status == "ABORTED" and r.abort_reason == "AI_UNAVAILABLE"


def test_resume_reuses_recorded_turns():
    store = MemoryMeetingStore()
    down = {"cio": lambda ev: "nonsense"}
    first = run(engine(down, store=store), mid="MTG-RESUME")
    assert first.abort_reason == "CIO_UNAVAILABLE"
    e2 = engine(store=store)
    second = run(e2, mid="MTG-RESUME")
    assert second.final_action == "RECOMMEND_PAPER"
    roles = [m[0]["content"].split("ROLE: ")[1].split(".")[0] for m in (c["messages"] for c in e2.gateway.client.calls)]
    assert roles == ["Chief AI Investment Officer", "Compliance & Audit Officer"]  # nothing else re-billed


def test_pg_meeting_memory_is_permanent(pg_dsn):
    store = PgMeetingStore(pg_dsn)
    r = run(engine(store=store))
    d = meeting_detail(store, r.meeting_id)
    assert d["meeting"]["status"] == "DECIDED" and len(d["turns"]) == 11 and d["recommendation"]["status"] == "PENDING_HUMAN"
    assert d["decision"]["final_action"] == "RECOMMEND_PAPER" and len(d["predictions"]) == len(r.prediction_ids)
    assert d["evidence"]["sha256"].strip() == r.pack.sha256
    for table in (
        "ai_meeting_turns",
        "ai_decisions",
        "ai_evidence_packs",
        "ai_predictions",
        "ai_disagreements",
        "approval_records",
        "paper_ledger_events",
    ):
        for stmt in (f"DELETE FROM {table}", f"TRUNCATE {table} CASCADE"):
            with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(stmt)


def test_prediction_timing_rules(pg_dsn):
    store = PgMeetingStore(pg_dsn)
    r = run(engine(store=store), ctx(mode="LIVE"), mid="MTG-LIVE") if False else None  # LIVE needs real clock; covered below
    now = datetime.now(UTC)
    with psycopg.connect(pg_dsn) as c:
        with pytest.raises(psycopg.errors.CheckViolation):  # LIVE predictions can't be backdated
            c.execute(
                "INSERT INTO ai_predictions (id, agent_id, agent_version, underlying_key, target, direction, confidence, horizon_min, ref_value,"
                " created_at, resolve_at, mode) VALUES ('P1','regime','v','NIFTY','INDEX','UP',3,30,24000,%s,%s,'LIVE')",
                (now - timedelta(days=1), now - timedelta(days=1) + timedelta(minutes=30)),
            )
    with psycopg.connect(pg_dsn) as c:
        c.execute(
            "INSERT INTO ai_predictions (id, agent_id, agent_version, underlying_key, target, direction, confidence, horizon_min, ref_value,"
            " created_at, resolve_at, mode) VALUES ('P2','regime','v','NIFTY','INDEX','UP',3,30,24000,%s,%s,'LIVE')",
            (now, now + timedelta(minutes=30)),
        )
        with pytest.raises(psycopg.errors.RaiseException):  # outcome observed before resolve_at
            c.execute(
                "INSERT INTO ai_prediction_outcomes (prediction_id, resolved_at, realized_value, observed_at, signed_return, hit, brier)"
                " VALUES ('P2', %s, 24010, %s, 0.0004, true, 0.16)",
                (now + timedelta(minutes=31), now + timedelta(minutes=10)),
            )
    assert r is None


# ---------------- API + human approval bridge ----------------


def _api_runtime():
    from doi.ai.service import build_ai
    from doi.core.config import Settings
    from tests.unit.test_paper_integration import runtime

    rt = runtime()
    rt.ai = build_ai(Settings(), None, kill_switch=lambda: rt.kill_switch, client=FakeGrokClient(scripted()))
    fv = features()
    rt.ai.features_fn = lambda u, now: replace(fv, as_of=now)
    rt.ai.snapshot_fn = lambda u, exp, now: asyncio.run(rt.provider.get_option_chain(rt.universe[u], exp))
    return rt


def test_api_meeting_to_human_approved_paper_order():
    from tests.unit.test_paper_integration import EXP

    rt = _api_runtime()
    c = TestClient(create := __import__("doi.app.main", fromlist=["create_app"]).create_app(runtime=rt))
    prop = {"expiry": EXP.isoformat(), "strike": 24000, "option_type": "CE", "stop_loss": 90, "targets": [[125, 0.5], [135, 0.5]]}
    r = c.post("/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": prop, "run_now": True})
    assert r.status_code == 202, r.text
    m = r.json()["meeting"]
    assert m["final_action"] == "RECOMMEND_PAPER", m
    rid = m["recommendation_id"]
    assert [x["id"] for x in c.get("/api/v1/ai/recommendations", params={"status": "PENDING_HUMAN"}).json()] == [rid]
    assert rt.paper.orders == {}  # nothing reaches the paper engine without a human
    a = c.post(f"/api/v1/ai/recommendations/{rid}/approve", json={"note": "looks fine"})
    assert a.status_code == 200 and a.json()["risk_gate_approved"] is True
    order = next(iter(rt.paper.orders.values()))
    assert order.signal_id == rid  # recommendation id is the paper idempotency key
    assert c.post(f"/api/v1/ai/recommendations/{rid}/approve").status_code == 409  # can't approve twice
    acts = [x.action for x in rt.approvals.records()]
    assert "APPROVE" in acts and "PAPER_ORDER" not in acts
    detail = c.get(f"/api/v1/ai/meetings/{m['id']}").json()
    assert detail["decision"]["final_action"] == "RECOMMEND_PAPER" and len(detail["turns"]) == 11
    assert create is not None


def test_api_reject_and_budget_and_agents():
    rt = _api_runtime()
    from doi.app.main import create_app
    from tests.unit.test_paper_integration import EXP

    c = TestClient(create_app(runtime=rt))
    prop = {"expiry": EXP.isoformat(), "strike": 24000, "option_type": "CE", "stop_loss": 90, "targets": [[125, 1.0]]}
    rid = c.post("/api/v1/ai/meetings", json={"underlying": "NIFTY", "proposal": prop, "run_now": True}).json()["meeting"][
        "recommendation_id"
    ]
    assert c.post(f"/api/v1/ai/recommendations/{rid}/reject", json={"note": "no"}).status_code == 200
    assert c.post(f"/api/v1/ai/recommendations/{rid}/approve").status_code == 409
    b = c.get("/api/v1/ai/budget").json()
    assert b["hard_limit_inr"] == 3000.0 and b["spent_inr"] > 0 and b["provider_live"] is True
    assert len(c.get("/api/v1/ai/agents").json()) == 15
    assert c.post("/api/v1/ai/budget", json={"hard_limit_inr": 5000}).status_code == 422


def test_worker_detects_admits_and_runs_meetings():
    from doi.ai.service import AIWorker
    from doi.intelligence.feature_engine.engine import FeatureValue, Provenance, Status

    rt = _api_runtime()
    base = features()
    prov = Provenance("t", (), ("t",))
    from itertools import chain, repeat

    seq = chain(["TREND_UP", "RANGE", "RANGE"], repeat("RANGE"))

    def fv(u, now):
        vals = dict(base.values)
        vals["trend_regime"] = FeatureValue(next(seq), Status.OK, prov)
        return replace(base, as_of=now, values=vals)

    w = AIWorker(rt, features_fn=fv)
    t0 = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # inside market hours (calendar always open in this runtime)
    ids = []
    for i in range(3):
        ids += asyncio.run(w.observe(t0 + timedelta(minutes=5 * i)))
    assert len(ids) == 1
    res = asyncio.run(w.process_one(t0 + timedelta(minutes=10)))
    assert (
        res is not None and res.status == "DECIDED" and res.final_action in ("WATCH", "NO_ACTION")
    )  # no proposal: never a trade
    assert rt.ai.queue.rows[ids[0]]["status"] == "DONE"
    rt.kill_switch = True
    from doi.ai.events import Event, EventKind

    eid = w.enqueue(Event(EventKind.HUMAN_REQUEST, "NIFTY", t0, {"key": "k"}), 100.0, t0)
    assert rt.ai.queue.rows[eid]["status"] == "SKIPPED" and rt.ai.queue.rows[eid]["skip_reason"] == "kill switch engaged"


def test_worker_isolates_meeting_failures():
    from doi.ai.events import Event, EventKind
    from doi.ai.service import AIWorker

    rt = _api_runtime()

    def boom(u, now):
        raise RuntimeError("feature source down")

    w = AIWorker(rt, features_fn=boom)
    t0 = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    eid = w.enqueue(Event(EventKind.HUMAN_REQUEST, "NIFTY", t0, {"key": "x"}), 100.0, t0)
    assert asyncio.run(w.process_one(t0)) is None and rt.ai.queue.rows[eid]["status"] == "FAILED"
