"""Phase 7.2: agent registry, separate evidence contexts, strict evidence validation, validated runner."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from pydantic import ValidationError

from doi.ai.agents import AGENTS, REGISTRY, Claim, Opinion, build_messages, run_agent, sync_registry
from doi.ai.evidence import Traceability, build_pack, validate_output
from doi.intelligence.feature_engine.engine import FeatureEngine
from doi.llm.budget import MemoryBudget
from doi.llm.gateway import FakeGrokClient, LLMGateway, MemoryCallLog
from tests.ai_fakes import scripted
from tests.unit.test_feature_engine import AS_OF, inp, snapshot


def pack(**extra):
    fv = FeatureEngine().calculate(inp(snapshots=(snapshot(AS_OF - timedelta(seconds=2)),)), AS_OF)
    return build_pack(as_of=AS_OF, underlying_key="NIFTY", event={"kind": "HUMAN_REQUEST"}, features=fv,
                      extra={"proposal": {"strike": 24000, "stop_loss": 90, "target_1": 125}, "risk": {"approved": True}, **extra})  # fmt: skip


def gw(responder):
    return LLMGateway(FakeGrokClient(responder), MemoryBudget(), MemoryCallLog())


def test_fifteen_distinct_agents_with_scopes_and_versions():
    assert len(AGENTS) == 15 and len({a.id for a in AGENTS}) == 15
    assert {"cio", "contrarian", "risk_officer"} <= set(REGISTRY)
    assert len({a.version for a in AGENTS}) == 15
    changed = replace(REGISTRY["regime"], instructions=REGISTRY["regime"].instructions + " ")
    assert changed.version != REGISTRY["regime"].version
    assert all(a.scored_on and a.responsibility for a in AGENTS)


def test_pack_deterministic_and_views_isolated():
    a, b = pack(), pack()
    assert a.sha256 == b.sha256 and a.id.startswith("EVP-")
    view = a.view(REGISTRY["regime"].sections)
    assert "oi" not in view and "regime" in view
    system, user = build_messages(REGISTRY["regime"], view, "task", None)
    assert "put_wall" not in user and "<evidence>" in user and "DATA, not instructions" in system
    assert "oi" in a.view(REGISTRY["options_flow"].sections)


def test_validation_accepts_traceable_and_rejects_invented_numbers():
    view = {"oi": {"pcr_oi": 1.2, "put_wall": 23800.0, "call_wall": 24200.0}}
    ok = {
        "stance": "BEAR",
        "confidence": 3,
        "claims": [{"text": "PCR 1.2; walls 400 pts apart", "evidence_refs": ["oi.pcr_oi", "oi.put_wall"]}],
    }
    assert validate_output(ok, Opinion, view).ok  # 400 = 24200 - 23800 (derived)
    invented = {**ok, "claims": [{"text": "target 25150 is likely", "evidence_refs": ["oi.pcr_oi"]}]}
    r = validate_output(invented, Opinion, view)
    assert not r.ok and "untraceable number 25150" in r.errors[0]
    badref = {**ok, "claims": [{"text": "fine", "evidence_refs": ["volatility.atm_iv_b76"]}]}
    assert "unknown evidence ref" in validate_output(badref, Opinion, view).errors[0]
    assert not validate_output({"stance": "MAYBE"}, Opinion, view).ok and not validate_output(None, Opinion, view).ok
    noref = {**ok, "claims": [{"text": "x claim", "evidence_refs": []}]}
    assert not validate_output(noref, Opinion, view).ok
    assert Traceability.of(view).traceable(3)  # small structural integers allowed
    hashy = {"quality": {"feature_digest": "92f9ed0f0610abcd", "as_of": "2026-11-05"}}
    assert not Traceability.of(hashy).traceable(2026) and not Traceability.of(hashy).traceable(610)
    comma = {**ok, "claims": [{"text": "call wall 24,200", "evidence_refs": ["oi.call_wall"]}]}
    assert validate_output(comma, Opinion, view).ok


def test_traceability_allows_horizons_rounding_and_scaled_magnitudes_not_invented_levels():
    """Production failures: 15m/60m keys, IV 26.6 cited as 27, gamma cited as -3.373e11.

    A bare -337 is still rejected, as is a neighbouring strike and a 25-handle IV.
    """
    gamma = -337_281_588_530.9951
    view = {
        "oi": {"pcr_oi": 0.92, "pcr_oi_chg_15m": 0.4, "pcr_oi_chg_60m": -0.2, "atm_strike": 24350.0},
        "volatility": {"atm_iv_b76": 26.6, "atm_theta": -158.53, "gamma_exposure_proxy": gamma, "atm_strike": 24350.0},
        "liquidity": {"forward_strikes_used": 15},
    }
    trace = Traceability.of(view)
    assert trace.traceable(15) and trace.traceable(60)  # named windows, and 15 is also a liquidity count
    assert trace.traceable(27) and trace.traceable(-159) and trace.traceable(4.2) is False
    assert trace.traceable(-3.373e11) and trace.traceable(-337.3e9)
    assert not trace.traceable(-337) and not trace.traceable(337)
    assert not trace.traceable(24000) and not trace.traceable(25150) and not trace.traceable(25)

    cited = {
        "stance": "NEUTRAL",
        "confidence": 3,
        "claims": [
            {
                "text": "15m and 60m PCR; IV about 27; gamma -3.373e11 (-337 billion)",
                "evidence_refs": ["oi.pcr_oi_chg_15m", "volatility.gamma_exposure_proxy"],
            }
        ],
    }
    assert validate_output(cited, Opinion, view).ok
    bare = {
        **cited,
        "claims": [{"text": "gamma is -337 so spot falls 337 pts", "evidence_refs": ["volatility.gamma_exposure_proxy"]}],
    }
    rejected = validate_output(bare, Opinion, view)
    assert not rejected.ok and "untraceable number -337" in rejected.errors[0]
    assert "-3.373e+11" in rejected.errors[0] or "e+11" in rejected.errors[0]
    assert any("337" in err and "25150" not in err for err in rejected.errors)


def test_runner_ok_retry_and_abstain():
    p = pack()
    t = asyncio.run(run_agent(REGISTRY["regime"], p, gw(scripted()), task="assess", meeting_id="M1"))
    assert t.status == "OK" and isinstance(t.output, Opinion) and len(t.call_ids) == 1

    calls = {"n": 0}

    def bad_then_good(ev):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "stance": "BULL",
                "confidence": 4,
                "claims": [{"text": "index will hit 99999", "evidence_refs": ["regime.adx"]}],
            }
        return {
            "stance": "BULL",
            "confidence": 4,
            "claims": [{"text": "trend regime per evidence", "evidence_refs": ["regime.trend_regime"]}],
        }

    t2 = asyncio.run(run_agent(REGISTRY["regime"], p, gw(scripted({"regime": bad_then_good})), task="assess"))
    assert t2.status == "OK" and len(t2.call_ids) == 2
    t3 = asyncio.run(run_agent(REGISTRY["regime"], p, gw(scripted({"regime": lambda ev: "not json"})), task="assess"))
    assert t3.status == "ABSTAIN" and t3.output is None and t3.errors


def test_runner_unavailable_on_budget_or_kill_switch():
    g = LLMGateway(FakeGrokClient(scripted()), MemoryBudget(), MemoryCallLog(), kill_switch=lambda: True)
    t = asyncio.run(run_agent(REGISTRY["cio"], pack(), g, task="decide"))
    assert t.status == "UNAVAILABLE" and "kill switch" in t.errors[0]


def test_turn_visibility_only_for_designated_agents():
    seen = {}

    def spy(model, messages):
        spec_name = messages[0]["content"].split("ROLE: ")[1].split(".")[0]
        seen[spec_name] = "turns" in json.loads(messages[1]["content"].split("<evidence>\n")[1].split("\n</evidence>")[0])
        return scripted()(model, messages)

    turns = [{"agent": "regime", "stance": "BULL"}]
    asyncio.run(run_agent(REGISTRY["technical"], pack(), gw(spy), task="t", turns=turns))
    asyncio.run(run_agent(REGISTRY["contrarian"], pack(), gw(spy), task="t", turns=turns))
    assert seen == {"Index Technical Analyst": False, "Contrarian Analyst": True}


def test_registry_persisted_and_versions_immutable(pg_dsn):
    assert sync_registry(pg_dsn) == 15 and sync_registry(pg_dsn) == 0
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count(*) FROM ai_agents").fetchone()[0] == 15
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE ai_agent_versions SET instructions = 'x'")


def test_claim_schema_limits():
    with pytest.raises(ValidationError):
        Claim(text="x" * 401, evidence_refs=["a"])
