"""Phase 7.5: independent scoring, timing rules, leaderboard."""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from doi.ai.meetings import MeetingEngine, PgMeetingStore
from doi.ai.scoring import Outcome, PgScoring, contrarian_save_rate, leaderboard, score_one, spearman, wilson_lower
from doi.data.recorder import MarketRecorder
from doi.llm.budget import MemoryBudget
from doi.llm.gateway import FakeGrokClient, LLMGateway, MemoryCallLog
from tests.ai_fakes import scripted
from tests.unit.test_feature_engine import AS_OF, snapshot
from tests.unit.test_meetings import ctx


def test_score_math():
    ret, hit, brier = score_one("UP", 5, 100.0, 101.0)
    assert ret == pytest.approx(0.01) and hit and brier == pytest.approx(0.0225)
    assert score_one("UP", 1, 100.0, 100.01)[1] is False  # inside the flat band counts as FLAT
    assert score_one("FLAT", 3, 100.0, 100.01)[1] is True
    assert wilson_lower(0, 0) == 0.0 and 0.4 < wilson_lower(60, 100) < 0.6 and wilson_lower(6, 10) < 0.6
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0) and spearman([1, 2], [1, 2]) is None


def _o(aid, hit, conf=3, ret=0.01, d="UP", mid=None):
    return Outcome("p", aid, "v1", mid, d, conf, ret if hit else -ret, hit, (0.65 - (1 if hit else 0)) ** 2)


def test_leaderboard_ranks_with_sample_penalty_and_provisional():
    outs = (
        [_o("a", True)] * 25
        + [_o("a", False)] * 15
        + [_o("b", True)] * 8
        + [_o("b", False)] * 2
        + [_o("c", True)] * 40
        + [_o("c", False)] * 40
    )
    lb = leaderboard(outs, min_n=30)
    assert [r["agent_id"] for r in lb] == ["a", "c", "b"] and lb[2]["provisional"] and lb[2]["rank"] is None
    a = lb[0]
    assert a["n"] == 40 and a["hit_rate"] == 0.625 and a["calibration"]["3"]["n"] == 40 and a["follow_ratio"] > 0
    assert contrarian_save_rate({"m1", "m2", "m3"}, {"m1": False, "m2": True}) == {"n": 2, "rate": 0.5}


def test_pg_resolution_uses_only_post_resolve_observations(pg_dsn):
    store = PgMeetingStore(pg_dsn)
    g = LLMGateway(FakeGrokClient(scripted()), MemoryBudget(), MemoryCallLog())
    r = asyncio.run(MeetingEngine(g, store).run(ctx(), AS_OF))
    assert r.prediction_ids
    rec, sc = MarketRecorder(pg_dsn), PgScoring(pg_dsn)
    rec.record_snapshot(snapshot(AS_OF + timedelta(minutes=10), ltp=25000.0))  # before every resolve_at (30 min): unusable
    assert sc.resolve_due(AS_OF + timedelta(minutes=40)) == 0
    rec.record_snapshot(snapshot(AS_OF + timedelta(minutes=31), ltp=24100.0))
    n = sc.resolve_due(AS_OF + timedelta(minutes=40))
    assert n == len(r.prediction_ids) and sc.resolve_due(AS_OF + timedelta(minutes=50)) == 0
    outs = sc.outcomes("REPLAY")
    assert outs and sc.outcomes("LIVE") == []  # replay meetings never leak into the live leaderboard
    up = [o for o in outs if o.direction == "UP"]
    assert up and all(o.hit for o in up) and all(o.signed_return == pytest.approx((24100 - 23990) / 23990) for o in up)
    lb = sc.leaderboard("REPLAY", min_n=1)
    assert {x["agent_id"] for x in lb} >= {"regime", "cio"} and lb[0]["rank"] == 1
    assert sc.snapshot(datetime.now(UTC), "REPLAY", min_n=1) > 0
    with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE ai_agent_scorecards SET rank = 1")


def test_leaderboard_api(pg_dsn):
    from fastapi.testclient import TestClient

    from doi.ai.service import build_ai
    from doi.app.main import create_app
    from doi.core.config import Settings
    from tests.unit.test_auth_controls import _boot

    rt = _boot(pg_dsn)
    rt.ai = build_ai(Settings(), pg_dsn, kill_switch=lambda: False, client=FakeGrokClient(scripted()))
    c = TestClient(create_app(runtime=rt))
    _, adm = rt.auth.create_key(rt.auth.create_user("sc@x"), "admin")
    h = {"Authorization": f"Bearer {adm}"}
    body = c.get("/api/v1/ai/leaderboard", headers=h).json()
    assert body["agents"] == [] and len(body["unscored_agents"]) == 15
    assert c.post("/api/v1/ai/scoring/run", headers=h).json()["resolved"] == 0
    assert c.get("/api/v1/ai/leaderboard", params={"mode": "BOGUS"}, headers=h).status_code == 422
