"""Phase 7.1: pricing, budget (memory and PostgreSQL), gateway behaviour and event orchestration."""

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest

from doi.ai.events import AdmissionPolicy, DetectorConfig, Event, EventKind, MemoryEventQueue, PgEventQueue, admit, detect
from doi.core.market import SessionCalendar
from doi.intelligence.feature_engine.engine import FeatureEngine, FeatureValue, Provenance, Status
from doi.llm.budget import BudgetExhausted, MemoryBudget, PgBudget
from doi.llm.gateway import (
    AIUnavailable,
    FakeGrokClient,
    LLMGateway,
    LLMRequest,
    MemoryCallLog,
    PgCallLog,
    ProviderError,
    RoutingPolicy,
    extract_json,
)
from doi.llm.pricing import (
    SEED_PRICES,
    CostModel,
    Usage,
    estimate_prompt_tokens,
    parse_usage,
    prices_from_listing,
    usd_actual,
    usd_estimate,
)
from tests.unit.test_feature_engine import AS_OF, inp

NOW = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
FAST = SEED_PRICES["grok-4.20-0309-non-reasoning"]


def req(**kw):
    base = dict(
        purpose="test", system="You are a test agent. Reply in JSON.", user="evidence: {}", agent_id="regime", max_tokens=300
    )
    return LLMRequest(**{**base, **kw})


def gw(responder=lambda m, msgs: '{"ok": true}', budget=None, **kw):
    return LLMGateway(FakeGrokClient(responder), budget or MemoryBudget(), MemoryCallLog(), **kw)


# ---------------- pricing ----------------


def test_usage_parsing_and_provider_cost_preferred():
    u = parse_usage({"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 400},
                     "completion_tokens_details": {"reasoning_tokens": 50}})  # fmt: skip
    assert (u.cached_tokens, u.reasoning_tokens, u.cost_ticks) == (400, 50, None)
    assert parse_usage({"prompt_tokens": 1, "cached_prompt_text_tokens": 1, "reasoning_tokens": 2}).reasoning_tokens == 2
    usd, src = usd_actual(FAST, Usage(1000, 200, 0, 0, cost_ticks=12_345_000_000))
    assert src == "PROVIDER_TICKS" and usd == D("1.2345")  # 1 tick = 1e-10 USD (xai-proto)
    usd2, src2 = usd_actual(FAST, u)
    assert src2 == "PRICE_BOOK" and usd2 == (D(600) * D("1.25") + D(400) * D("0.20") + D(250) * D("2.50")) / D(1_000_000)


def test_listing_unit_conversion():
    p = prices_from_listing([{"name": "grok-x", "aliases": ["grok-x-latest"], "prompt_text_token_price": 2000,
                              "completion_text_token_price": 5000, "cached_prompt_token_price": 500}])  # fmt: skip
    assert (
        p["grok-x"].input_per_m == D("0.2")
        and p["grok-x"].output_per_m == D("0.5")
        and p["grok-x"].cached_input_per_m == D("0.05")
    )
    assert p["grok-x-latest"].verified and prices_from_listing([{"name": "bad"}]) == {}
    # the official REST example (GET /v1/models): USD cents per 100M tokens
    official = prices_from_listing([{"id": "latest", "prompt_text_token_price": 12500, "cached_prompt_text_token_price": 2000,
                                     "completion_text_token_price": 25000}])  # fmt: skip
    o = official["latest"]
    assert (o.input_per_m, o.cached_input_per_m, o.output_per_m) == (D("1.25"), D("0.2"), D("2.5"))


def test_estimate_is_upper_bound_of_settled_cost():
    text = "x" * 3000
    est = usd_estimate(FAST, estimate_prompt_tokens(text), 600)
    actual, _ = usd_actual(FAST, Usage(len(text) // 4, 600, 0, 0))
    assert est >= actual
    cm = CostModel()
    assert cm.inr(D("1")) == D("106.200000")  # 90 INR/USD x 1.18 tax buffer


def test_routing_downgrades_then_stops():
    r = RoutingPolicy()
    assert r.tier("regime", "reasoning", 0.1) == "reasoning" and r.tier("regime", "reasoning", 0.65) == "fast"
    assert r.tier("cio", "reasoning", 0.65) == "reasoning" and r.tier("cio", "reasoning", 0.85) == "fast"
    assert r.tier("cio", "reasoning", 0.98) is None and r.tier("x", "fast", 0.0) == "fast"


def test_extract_json():
    assert extract_json('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("no json") is None and extract_json("[1,2]") is None


# ---------------- gateway ----------------


def test_gateway_success_logs_cost_and_caches():
    g = gw()
    r1 = asyncio.run(g.complete(req(), NOW))
    r2 = asyncio.run(g.complete(req(), NOW))
    assert r1.text == '{"ok": true}' and r1.cost_inr > 0 and not r1.cache_hit
    assert r2.cache_hit and r2.cost_inr == 0 and len(g.client.calls) == 1
    st = g.budget.status()
    assert st.spent_inr == r1.cost_inr and st.reserved_inr == 0
    assert [x["status"] for x in g.calls.rows] == ["OK", "CACHE_HIT"]


def test_gateway_refusals():
    with pytest.raises(AIUnavailable, match="kill switch"):
        asyncio.run(gw(kill_switch=lambda: True).complete(req(), NOW))
    with pytest.raises(AIUnavailable, match="disabled"):
        asyncio.run(gw(enabled=False).complete(req(), NOW))
    tiny = MemoryBudget(D("1.00"), D("0.10"))
    with pytest.raises(AIUnavailable, match="budget"):
        asyncio.run(gw(budget=tiny).complete(req(user="x" * 50_000, max_tokens=4000), NOW))
    assert tiny.status().spent_inr == 0
    nope = gw(tiers=replace(LLMGateway.__dataclass_fields__["tiers"].default_factory(), fast="unknown-model"))
    with pytest.raises(AIUnavailable, match="no price"):
        asyncio.run(nope.complete(req(), NOW))


def test_retries_and_failure_accounting():
    seq = iter([ProviderError("503", retryable=True), '{"ok": 1}'])

    def flaky(m, msgs):
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return x

    g = gw(flaky)
    r = asyncio.run(g.complete(req(), NOW))
    assert r.attempts == 2 and [x["status"] for x in g.calls.rows] == ["ERROR", "OK"] and g.calls.rows[0]["cost_inr"] == 0

    def timeout(m, msgs):
        raise ProviderError("timeout", retryable=True, billed_unknown=True)

    g2 = gw(timeout)
    with pytest.raises(AIUnavailable):
        asyncio.run(g2.complete(req(), NOW))
    st = g2.budget.status()
    assert st.spent_inr > 0 and st.reserved_inr == 0  # possibly billed: counted at the full estimate, twice
    assert [x["cost_source"] for x in g2.calls.rows] == ["ESTIMATE_ON_FAILURE"] * 2

    def bad(m, msgs):
        raise ProviderError("400", retryable=False)

    g3 = gw(bad)
    with pytest.raises(AIUnavailable):
        asyncio.run(g3.complete(req(), NOW))
    assert len(g3.client.calls) == 1 and g3.budget.status().spent_inr == 0


def test_circuit_breaker_stops_calls():
    def down(m, msgs):
        raise ProviderError("502", retryable=True)

    g = gw(down, breaker_threshold=2, max_retries=0)
    for _ in range(2):
        with pytest.raises(AIUnavailable):
            asyncio.run(g.complete(req(user=str(_)), NOW))
    with pytest.raises(AIUnavailable, match="circuit"):
        asyncio.run(g.complete(req(user="z"), NOW))
    assert len(g.client.calls) == 2


def test_parallel_calls_never_exceed_budget_memory():
    budget = MemoryBudget(D("2.00"), D("0.20"))
    g = gw(budget=budget, max_concurrency=8)

    async def go():
        return await asyncio.gather(
            *(g.complete(req(user=f"u{i}" * 200, max_tokens=800), NOW) for i in range(60)), return_exceptions=True
        )

    res = asyncio.run(go())
    st = budget.status()
    assert any(isinstance(r, AIUnavailable) for r in res) and any(not isinstance(r, Exception) for r in res)
    assert st.spent_inr + st.reserved_inr <= st.usable_inr and st.reserved_inr == 0


def test_provider_overrun_is_recorded_and_freezes():
    b = MemoryBudget(D("100"), D("10"))
    g = gw(budget=b)
    g.client.usage = lambda m, msgs, t: {"prompt_tokens": 10, "completion_tokens": 10, "cost_in_usd_ticks": 5_000_000_000}  # $0.5
    asyncio.run(g.complete(req(), NOW))
    st = b.status()
    assert st.frozen and st.overrun_inr > 0
    with pytest.raises(AIUnavailable, match="budget"):
        asyncio.run(g.complete(req(user="next"), NOW))


# ---------------- PostgreSQL budget ----------------


def test_pg_budget_atomic_under_thread_concurrency(pg_dsn):
    b = PgBudget(pg_dsn, D("3000"), D("150"))
    ok, refused = [], []

    def worker():
        try:
            ok.append(b.reserve(D("100"), purpose="t", now=NOW))
        except BudgetExhausted:
            refused.append(1)

    ts = [threading.Thread(target=worker) for _ in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    st = b.status(NOW)
    assert len(ok) == 28 and len(refused) == 12 and st.reserved_inr == D("2800")
    for rid in ok[:10]:
        b.settle(rid, D("60"))
    for rid in ok[10:]:
        b.release(rid)
    st = b.status(NOW)
    assert st.spent_inr == D("600") and st.reserved_inr == 0 and not st.frozen
    with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("UPDATE llm_budget_months SET spent_inr = 5000")  # the database itself enforces the cap


def test_pg_budget_overrun_freeze_and_month_rollover(pg_dsn):
    b = PgBudget(pg_dsn, D("3000"), D("150"))
    rid = b.reserve(D("10"), purpose="t", now=NOW)
    b.settle(rid, D("12.5"))
    st = b.status(NOW)
    assert st.frozen and st.overrun_inr == D("2.5") and st.spent_inr == D("12.5")
    with pytest.raises(BudgetExhausted):
        b.reserve(D("1"), purpose="t", now=NOW)
    nxt = b.status(NOW + timedelta(days=31))
    assert nxt.spent_inr == 0 and not nxt.frozen and nxt.usable_inr == D("2850")
    b.set_limit(D("3000"), NOW, unfreeze=True)
    assert not b.status(NOW).frozen


def test_pg_gateway_end_to_end_logging(pg_dsn):
    g = LLMGateway(FakeGrokClient(lambda m, msgs: '{"x": 1}'), PgBudget(pg_dsn), PgCallLog(pg_dsn))
    asyncio.run(g.complete(req(meeting_id="MTG-1"), NOW))
    asyncio.run(g.complete(req(meeting_id="MTG-1"), NOW))
    with psycopg.connect(pg_dsn) as c:
        rows = c.execute("SELECT status, cache_hit, cost_inr > 0 FROM llm_calls ORDER BY created_at").fetchall()
        assert rows == [("OK", False, True), ("CACHE_HIT", True, False)]
        spent = c.execute("SELECT spent_inr FROM llm_budget_months").fetchone()[0]
        assert spent == c.execute("SELECT sum(cost_inr) FROM llm_calls").fetchone()[0]


# ---------------- events ----------------


def _vec(**vals):
    base = FeatureEngine().calculate(inp(), AS_OF)
    prov = Provenance("t", (), ("t",))
    values = dict(base.values)
    for k, v in vals.items():
        values[k] = FeatureValue(v, Status.OK, prov)
    return replace(base, values=values)


def test_detectors_require_dwell_and_ignore_flapping():
    up, rng = _vec(trend_regime="TREND_UP"), _vec(trend_regime="RANGE")
    assert detect([up, rng]) == []  # one vector is not a regime change
    shift = detect([up, rng, rng])
    assert [e.kind for e in shift] == [EventKind.REGIME_SHIFT] and shift[0].payload["to"] == "RANGE"
    assert detect([up, rng, up, rng]) == []  # flapping: no event
    mig = detect([_vec(), _vec(call_wall_migration_steps=3, call_wall=24200.0)])
    assert [e.kind for e in mig] == [EventKind.OI_WALL_MIGRATION]
    iv = detect([_vec(atm_iv_b76=12.0), _vec(atm_iv_b76=14.5)], DetectorConfig(iv_jump_pts=2.0))
    assert [e.kind for e in iv] == [EventKind.IV_SHOCK]


def _event(kind=EventKind.REGIME_SHIFT, at=NOW, key="trend"):
    return Event(kind, "NIFTY", at, {"key": key})


def test_admission_controls():
    q, cal, pol = MemoryEventQueue(), SessionCalendar(), AdmissionPolicy()
    kw = dict(calendar=cal, kill_switch=False, quality=100.0, ai_available=True)
    assert admit(_event(), q, pol, **{**kw, "kill_switch": True}).reason == "kill switch engaged"
    assert "AI unavailable" in admit(_event(), q, pol, **{**kw, "ai_available": False}).reason
    assert admit(_event(), q, pol, **{**kw, "quality": 50.0}).admitted is False
    assert admit(_event(at=NOW.replace(hour=12)), q, pol, **kw).reason == "market closed"
    first = _event()
    assert admit(first, q, pol, **kw).admitted
    q.admit(first, admit(first, q, pol, **kw))
    assert "cooldown" in admit(_event(at=NOW + timedelta(minutes=10), key="vol"), q, pol, **kw).reason
    for i, k in enumerate((EventKind.IV_SHOCK, EventKind.OI_WALL_MIGRATION)):
        e = _event(k, NOW + timedelta(minutes=1 + i))
        q.admit(e, admit(e, q, pol, **kw))
    assert "daily cap" in admit(_event(EventKind.SIGNAL_CANDIDATE, NOW + timedelta(minutes=5)), q, pol, **kw).reason
    assert admit(_event(EventKind.HUMAN_REQUEST, NOW.replace(hour=12)), q, pol, **kw).reason.startswith("daily cap")


def _human(at, key):
    return Event(EventKind.HUMAN_REQUEST, "NIFTY", at, {"key": key})


def _assert_data_quality_aborts_spare_the_daily_cap(q):
    """Three DONE/DATA_QUALITY finishes leave the cap free; three real finishes fill it."""
    cal, pol = SessionCalendar(), AdmissionPolicy()
    assert pol.daily_trade_meetings == 3
    kw = dict(calendar=cal, kill_switch=False, quality=100.0, ai_available=True)
    day = cal.at(cal.session_date(NOW), datetime.min.time())
    for i in range(pol.daily_trade_meetings):
        e = _human(NOW + timedelta(minutes=i), f"dq-{i}")
        eid = q.admit(e, admit(e, q, pol, **kw))
        assert q.trade_meetings_today(day) == 1  # queued, so it still reserves a slot
        got = q.claim(e.at)
        assert got["id"] == eid
        assert q.trade_meetings_today(day) == 1  # RUNNING still reserves a slot
        q.finish(eid, "DONE", "DATA_QUALITY")  # worker records the zero-LLM abort as DONE
        assert q.trade_meetings_today(day) == 0  # prior aborts do not accumulate
    nxt = _human(NOW + timedelta(minutes=10), "after-dq")
    assert admit(nxt, q, pol, **{**kw, "kill_switch": True}).reason == "kill switch engaged"
    assert admit(nxt, q, pol, **{**kw, "ai_available": False}).reason.startswith("AI unavailable")
    assert admit(nxt, q, pol, **kw).admitted is True
    for i, reason in enumerate((None, "QUORUM", None)):
        e = _human(NOW + timedelta(minutes=20 + i), f"real-{i}")
        decision = admit(e, q, pol, **kw)
        assert decision.admitted, decision.reason
        q.finish(q.admit(e, decision), "DONE", reason)
    assert q.trade_meetings_today(day) == pol.daily_trade_meetings
    blocked = _human(NOW + timedelta(minutes=40), "capped")
    assert admit(blocked, q, pol, **kw).reason == f"daily cap {pol.daily_trade_meetings} trade meetings reached"


def test_data_quality_aborts_do_not_consume_daily_trade_cap():
    q = MemoryEventQueue()
    _assert_data_quality_aborts_spare_the_daily_cap(q)
    dq = [r for r in q.rows.values() if r["skip_reason"] == "DATA_QUALITY"]
    assert len(dq) == 3 and {r["status"] for r in dq} == {"DONE"}


def test_fingerprint_dedupe_and_claim_order():
    q = MemoryEventQueue()
    e = _event()
    assert q.admit(
        e, admit(e, q, AdmissionPolicy(), calendar=SessionCalendar(), kill_switch=False, quality=100, ai_available=True)
    ) == q.admit(
        replace(e, at=e.at + timedelta(seconds=20)),
        admit(
            e,
            MemoryEventQueue(),
            AdmissionPolicy(),
            calendar=SessionCalendar(),
            kill_switch=False,
            quality=100,
            ai_available=True,
        ),
    )
    assert q.claim(NOW)["status"] == "RUNNING" and q.claim(NOW) is None


def test_pg_queue_skip_locked_and_expiry(pg_dsn):
    q = PgEventQueue(pg_dsn)
    from doi.ai.events import Admission

    for i in range(6):
        q.admit(Event(EventKind.HUMAN_REQUEST, "NIFTY", NOW + timedelta(minutes=i), {"key": str(i)}), Admission(True))
    q.admit(
        Event(EventKind.HUMAN_REQUEST, "NIFTY", NOW - timedelta(hours=1), {"key": "old"}, valid_for=timedelta(minutes=1)),
        Admission(True),
    )
    got = []

    def claimer():
        while (r := q.claim(NOW + timedelta(minutes=5))) is not None:
            got.append(r["id"])

    ts = [threading.Thread(target=claimer) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == len(set(got)) == 6  # no event claimed twice; the expired one skipped
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count(*) FROM ai_events WHERE status = 'SKIPPED'").fetchone()[0] == 1


def test_pg_data_quality_aborts_do_not_consume_daily_trade_cap(pg_dsn):
    q = PgEventQueue(pg_dsn)
    _assert_data_quality_aborts_spare_the_daily_cap(q)
    with psycopg.connect(pg_dsn) as c:
        dq = c.execute(
            "SELECT status, skip_reason FROM ai_events WHERE skip_reason = 'DATA_QUALITY' ORDER BY created_at"
        ).fetchall()
        real = c.execute("SELECT count(*) FROM ai_events WHERE status = 'DONE' AND skip_reason IS NULL").fetchone()[0]
    assert dq == [("DONE", "DATA_QUALITY")] * 3 and real == 2
