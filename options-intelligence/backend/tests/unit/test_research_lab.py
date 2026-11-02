"""Phase 7.4: strategy DSL, causal backtester, sealed holdout, reproducibility, paper validation, promotion and rollback."""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest
from pydantic import ValidationError

from doi.core.auth import ApprovalLog, Principal
from doi.core.market import SessionCalendar
from doi.core.models import Bar
from doi.llm.budget import MemoryBudget
from doi.llm.gateway import FakeGrokClient, LLMGateway, MemoryCallLog
from doi.research.lab import Condition, Lab, LabError, StrategySpec, backtest, feature_series, generate_hypothesis

CAL = SessionCalendar()
START = datetime(2026, 9, 1, 3, 45, tzinfo=UTC)  # Tuesday 09:15 IST


def sessions(n_days=10, drift=1.0, wiggle=6.0):
    bars, d, made = [], START, 0
    level = 24000.0
    while made < n_days:
        if CAL.is_trading_day(CAL.session_date(d)):
            for i in range(75):
                c = level + drift * i + wiggle * math.sin(i / 2)
                bars.append(Bar(d + timedelta(minutes=5 * i), c - 1, c + 3, c - 3, c + 0.5))
            level = bars[-1].close
            made += 1
        d += timedelta(days=1)
    return bars


LONG = StrategySpec(name="trend-long", direction="LONG", entry_all=(Condition(feature="trend_regime", op="==", value="TREND_UP"),
                    Condition(feature="rsi", op=">", value=50)), stop_atr=2.0, target_atr=2.0, exit_after_bars=6, cost_points=1.0,
                    slippage_points=0.5)  # fmt: skip
SHORT = LONG.model_copy(update={"name": "trend-short", "direction": "SHORT"})


def test_spec_validation_and_hash():
    with pytest.raises(ValidationError):
        Condition(feature="moon_phase", op=">", value=1)
    with pytest.raises(ValidationError):
        StrategySpec(name="x" * 3, direction="LONG", entry_all=())
    assert LONG.sha256 == StrategySpec.model_validate(json.loads(LONG.model_dump_json())).sha256 and LONG.sha256 != SHORT.sha256


def test_backtest_is_causal_and_conservative():
    bars = sessions(4)
    fs = feature_series(bars, 5, CAL)
    full = backtest(LONG, bars, CAL, features=fs)
    cut = bars[: len(bars) - 75]
    part = backtest(LONG, cut, CAL, features=feature_series(cut, 5, CAL))
    done_before = [t for t in full if t.exit_ts < cut[-1].ts]
    assert done_before == part[: len(done_before)] and done_before  # future bars never change earlier trades
    t = full[0]
    entry_bar = next(b for b in bars if b.ts == t.entry_ts)
    assert t.entry == entry_bar.open + 0.5  # next-bar open plus slippage, not the signal bar's close
    # stop and target inside one bar: the stop wins
    spike = [Bar(START + timedelta(minutes=5 * i), 100, 101, 99, 100) for i in range(40)]
    spike.append(Bar(START + timedelta(minutes=200), 100, 200, 0, 100))
    bt = backtest(StrategySpec(name="always", direction="LONG", entry_all=(Condition(feature="session_open", op="==", value=True),),
                               stop_atr=1, target_atr=1, exit_after_bars=50), spike, CAL)  # fmt: skip
    assert any(x.reason == "STOP" for x in bt) and not any(x.reason == "TARGET" for x in bt)


def test_pipeline_holdout_once_paper_promote_rollback(pg_dsn):
    lab = Lab(pg_dsn, policy=__import__("doi.research.lab", fromlist=["Policy"]).Policy(min_trades=5, min_paper_trades=2))
    bars = sessions(10)
    v1 = lab.create_version(LONG, "human:test")
    assert lab.create_version(LONG, "human:test") == v1  # identical spec = same version
    t0 = time.time()
    r = lab.run_experiment(v1, bars)
    assert time.time() - t0 < 60
    assert r["passed"], r
    again = Lab(pg_dsn, policy=lab.policy).run_experiment(v1, bars)
    assert (
        again["development"] == r["development"] and again["manifest"]["data_sha256"] == r["manifest"]["data_sha256"]
    )  # reproducible
    h = lab.open_holdout(r["experiment_id"], "human:test")
    assert h["passed"]
    with pytest.raises(LabError, match="already opened"):
        lab.open_holdout(r["experiment_id"], "human:test")

    @dataclass
    class T:
        config_version: str
        net_pnl: D

    ap = ApprovalLog(pg_dsn)
    who = Principal(None, "admin@x", "admin")
    with pytest.raises(LabError, match="paper validation"):
        lab.promote(r["experiment_id"], who, ap)
    pv = lab.record_paper_validation(r["experiment_id"], [T(v1, D("150")), T(v1, D("-20")), T("other", D("-999"))])
    assert pv["passed"] and pv["paper_trades"] == 2
    lab.promote(r["experiment_id"], who, ap)

    v2 = lab.create_version(LONG.model_copy(update={"target_atr": 3.0}), "agent:quant_lead", parent_version_id=v1)
    r2 = lab.run_experiment(v2, bars)
    sid = v1.rsplit("-v", 1)[0]
    if r2["passed"] and lab.open_holdout(r2["experiment_id"], "h")["passed"]:
        lab.record_paper_validation(r2["experiment_id"], [T(v2, D("10")), T(v2, D("10"))])
        lab.promote(r2["experiment_id"], who, ap)
    with pytest.raises(LabError, match="previously promoted"):
        lab.rollback(sid, "STR-none-v9", who, ap)
    lab.rollback(sid, v1, who, ap, "back to v1")
    hist = lab.history(sid)
    assert hist["active_version_id"] == v1 and [a["action"] for a in hist["activations"]][-1] == "ROLLBACK"
    assert v1 in hist["comparison"] and v2 in hist["comparison"] and len(hist["versions"]) == 2
    assert [x.action for x in ap.records()].count("PROMOTE") >= 1 and ap.verify() >= 2
    with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE research_results SET passed = true")


def test_failing_strategy_never_reaches_holdout(pg_dsn):
    lab = Lab(pg_dsn)
    v = lab.create_version(SHORT, "human:test")
    r = lab.run_experiment(v, sessions(8))
    assert not r["passed"]
    with pytest.raises(LabError, match="holdout locked"):
        lab.open_holdout(r["experiment_id"], "human:test")


def test_hypothesis_generation_schema_only():
    good = json.dumps(LONG.model_dump(mode="json"))
    g = LLMGateway(FakeGrokClient(lambda m, msgs: good), MemoryBudget(), MemoryCallLog())
    spec = asyncio.run(generate_hypothesis(g, {"recent": "uptrend sessions"}))
    assert spec == LONG
    bad = LLMGateway(FakeGrokClient(lambda m, msgs: '{"name": "x", "direction": "UP"}'), MemoryBudget(), MemoryCallLog())
    assert asyncio.run(generate_hypothesis(bad, {})) is None


def test_research_api_requires_approved_dataset(pg_dsn):
    from fastapi.testclient import TestClient

    from doi.app.main import create_app
    from tests.unit.test_auth_controls import _boot

    rt = _boot(pg_dsn)
    c = TestClient(create_app(runtime=rt))
    _, adm = rt.auth.create_key(rt.auth.create_user("lab@x"), "admin")
    h = {"Authorization": f"Bearer {adm}"}
    vid = c.post("/api/v1/research/strategies", json={"spec": json.loads(LONG.model_dump_json())}, headers=h).json()["version_id"]
    r = c.post("/api/v1/research/experiments", json={"version_id": vid, "dataset_id": "DS-missing"}, headers=h)
    assert r.status_code == 422 and "unknown dataset" in r.json()["detail"]
    assert c.get(f"/api/v1/research/strategies/{vid.rsplit('-v', 1)[0]}", headers=h).json()["versions"][0]["id"] == vid
    assert c.get("/api/v1/research/coverage", headers=h).status_code == 200
