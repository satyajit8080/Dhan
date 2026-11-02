"""Phase 8: team-size study harness and judgement scorecard (SIMULATED agent replies; tests the harness only)."""

import asyncio
from datetime import timedelta

import pytest

from doi.llm.budget import MemoryBudget
from doi.llm.gateway import FakeGrokClient, LLMGateway, MemoryCallLog
from doi.research.team_study import TEAMS, StudyCase, run_study
from tests.ai_fakes import scripted
from tests.unit.test_feature_engine import AS_OF
from tests.unit.test_meetings import ctx


def cases(n=4):
    return [
        StudyCase(ctx(), AS_OF + timedelta(minutes=10 * i), 23990.0, regime_shift_to="TREND_UP" if i % 2 == 0 else None)
        for i in range(n)
    ]


def factory(overrides=None):
    return lambda: LLMGateway(FakeGrokClient(scripted(overrides)), MemoryBudget(), MemoryCallLog())


def test_compares_single_trio_full_on_same_cases():
    up = lambda case, horizon: case.ref_value * 1.002
    rep = asyncio.run(run_study(factory(), cases(), up, data_class="SIMULATED"))
    assert rep["data_class"] == "SIMULATED" and set(rep["teams"]) == {"single", "trio", "full"}
    full, trio, single = rep["teams"]["full"], rep["teams"]["trio"], rep["teams"]["single"]
    assert full["meetings"] == trio["meetings"] == single["meetings"] == 4
    assert full["llm_cost_inr"] > trio["llm_cost_inr"] > single["llm_cost_inr"] > 0  # team size drives cost
    assert full["cio_hit_rate"] == 1.0 and full["useful_recommendations"] == 4
    assert full["cost_per_useful_recommendation_inr"] == pytest.approx(full["llm_cost_inr"] / 4, abs=1e-3)
    assert full["regime_recognition_rate"] == 1.0 and full["evidence_cited_claims"] > single["evidence_cited_claims"]
    assert single["team"]["contrarian"] is False and full["calibration"]["3"]["n"] == 4


def test_good_rejections_and_losses_measured():
    down = lambda case, horizon: case.ref_value * 0.995
    veto = {"contrarian": {"veto": True}}
    rep = asyncio.run(run_study(factory(veto), cases(2), down, teams=[TEAMS[2]], data_class="SIMULATED"))
    full = rep["teams"]["full"]
    assert full["recommendations"] == 0 and full["good_rejection_rate"] == 1.0  # the contrarian veto avoided two misses
    rep2 = asyncio.run(run_study(factory(), cases(2), down, teams=[TEAMS[2]], data_class="SIMULATED"))
    assert rep2["teams"]["full"]["worst_recommendation_return"] == pytest.approx(-0.005, abs=1e-6)
    assert rep2["teams"]["full"]["cost_per_useful_recommendation_inr"] is None


def test_data_class_required():
    with pytest.raises(ValueError):
        asyncio.run(run_study(factory(), cases(1), lambda c, h: None, data_class="LIVE"))
