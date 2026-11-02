"""Phase 6.2: interrupted-state recovery, quote ordering, cross-date and holiday
square-off, and charge-schedule verification (engine-level, no database)."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

from doi.core.market import IST, Quote, SessionCalendar
from doi.paper.charges import SCHEDULES, compute_charges, schedule_for
from doi.paper.engine import PaperExecution
from doi.paper.models import ExitReason, OrderStatus, PositionState
from doi.paper.reconcile import project_ledger, reconcile
from doi.risk.gate import RiskGate
from doi.risk.models import RiskConfig
from tests.unit.test_paper import FM, LIMITS, T0, cand, engine, mkt, opened, q, t

FRI = T0 + timedelta(days=4)  # Friday 10:00 IST
MON = T0 + timedelta(days=7)
TUE = T0 + timedelta(days=8)


def eng_with(holidays=frozenset(), risk=None):
    g = RiskGate(risk or RiskConfig(), FM, SessionCalendar(holidays=frozenset(holidays)), LIMITS)
    return PaperExecution(account_id="A", starting_capital=D(500000), gate=g, opened_at=T0)


def quote_at(ts, bid, ask, sec="50001", qty=750):
    return Quote(sec, ts, bid, qty, ask, qty, None, 24010.0, 50000, 10000)


def open_on(e, ts):
    c = replace(cand(), created_at=ts, valid_until=ts + timedelta(seconds=60))
    c = replace(c, contract=replace(c.contract, expiry=date(2026, 11, 24)))
    res = e.submit_simulated(c, mkt(quote_at(ts, 99.5, 100.0)), ts)
    assert res.decision.approved, res.decision.reasons
    e.process_market_event(quote_at(ts + timedelta(seconds=1), 99.5, 100.0))
    return e.positions[res.order.position_id]


# ---------------- quotes ----------------


def test_delayed_quote_within_latency_never_fills():
    e = engine()
    o = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(0.2, 99.5, 100.0))  # observed before submit+250ms
    assert o.filled_qty == 0
    e.process_market_event(q(0.25, 99.5, 100.0))
    assert o.filled_qty == 150


def test_late_quote_after_expiry_expires_instead_of_filling():
    e = engine()
    o = e.submit_simulated(cand(valid_s=5), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(30, 99.5, 100.0))
    assert o.status is OrderStatus.EXPIRED and not e.fills


def test_out_of_order_and_duplicate_quotes_per_security():
    e, o = opened(qty_quote=75)
    e.process_market_event(q(0.5, 99.5, 100.0))  # older than last processed for 50001
    e.process_market_event(q(1, 99.5, 100.0))  # exact duplicate timestamp
    e.process_market_event(q(0.9, 99.5, 100.0, sec="60001"))  # other security: independent clock
    assert o.filled_qty == 75 and e.ignored_events == 2
    e.process_market_event(q(1.5, 99.5, 100.0))
    assert o.filled_qty == 150


def test_stop_never_triggered_by_stale_replay():
    e, o = opened()
    e.process_market_event(q(3, 99.0, 99.5))
    e.process_market_event(q(2, 80.0, 81.0))  # stale replay through the stop
    assert e.positions[o.position_id].state is PositionState.OPEN


# ---------------- square-off across dates / holidays ----------------


def test_carried_position_squared_off_at_next_open():
    e = eng_with()
    p = open_on(e, FRI)
    e.advance_time(FRI.replace(hour=11))  # Friday 16:30 IST: same-day 15:20 rule
    eng = eng_with()  # second engine: process down from Friday 10:00 until after the weekend
    pos = open_on(eng, FRI)
    eng.advance_time(FRI + timedelta(days=1))  # Saturday: market closed, no exit
    assert pos.state is PositionState.OPEN
    eng.advance_time(MON.replace(hour=3, minute=40))  # Monday 09:10 IST, before the open
    assert pos.state is PositionState.OPEN
    eng.advance_time(MON.replace(hour=3, minute=46))  # Monday 09:16 IST
    assert pos.exit_reason is ExitReason.TIME_SQUARE_OFF and pos.state is PositionState.EXIT_PENDING
    assert p.state is PositionState.EXIT_PENDING


def test_holiday_defers_square_off_to_next_trading_day():
    holiday = MON.astimezone(IST).date()
    eng = eng_with(holidays={holiday})
    pos = open_on(eng, FRI)
    eng.advance_time(MON.replace(hour=5))  # Monday holiday 10:30 IST
    assert pos.state is PositionState.OPEN
    eng.advance_time(TUE.replace(hour=3, minute=46))
    assert pos.exit_reason is ExitReason.TIME_SQUARE_OFF
    eng.process_market_event(quote_at(TUE.replace(hour=3, minute=47), 98.0, 98.5))
    assert pos.state is PositionState.EVALUATED and eng.trades[-1].exit_reason == "TIME_SQUARE_OFF"


def test_no_entries_on_holiday():
    holiday = MON.astimezone(IST).date()
    eng = eng_with(holidays={holiday})
    c = replace(cand(), created_at=MON, valid_until=MON + timedelta(seconds=60))
    d = eng.submit_simulated(c, mkt(quote_at(MON, 99.5, 100.0)), MON).decision
    assert "market_open" in str(d.reasons)


def test_day_rollover_crosses_holiday():
    eng = eng_with(holidays={MON.astimezone(IST).date()})
    open_on(eng, FRI)
    eng.advance_time(TUE)
    rolls = [ev.payload["session"] for ev in eng.ledger.events if ev.kind == "DAY_ROLLOVER"]
    assert rolls == ["2026-11-06", "2026-11-10"]  # Friday, then straight to Tuesday (Mon holiday, weekend skipped)


def test_expired_contract_exits():
    e, o = opened()
    e.advance_time(T0 + timedelta(days=9))  # past 10 Nov expiry
    assert e.positions[o.position_id].exit_reason is ExitReason.EXPIRY


# ---------------- interrupted-state recovery ----------------


def test_recover_repairs_exit_pending_without_working_order():
    e, o = opened()
    p = e.positions[o.position_id]
    e.close_position(p.id, t(5))
    xo = e.orders[p.exit_order_id]
    xo.status = OrderStatus.CANCELLED  # simulate an exit lost mid-operation
    assert reconcile(e)  # flagged before recovery
    actions = e.recover(t(6))
    assert any("reopened" in a for a in actions) and p.state is PositionState.OPEN
    assert reconcile(e) == []


def test_recover_drops_orphan_plan_and_finalises_flat_position():
    e = engine()
    o = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)).order
    o.status = OrderStatus.CANCELLED  # planned entry whose order died without the bookkeeping
    e2, o2 = opened()
    p2 = e2.positions[o2.position_id]
    e2.close_position(p2.id, t(5))
    e2.process_market_event(q(6, 99.0, 99.5))
    assert e.recover(t(1)) and not e._planned
    p2.state = PositionState.CLOSED  # crash between CLOSED and EVALUATED
    e2.recover(t(7))
    assert p2.state is PositionState.EVALUATED and len(e2.trades) == 1


def test_recover_applies_missed_expiry():
    e = engine()
    o = e.submit_simulated(cand(valid_s=10), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.recover(t(100))
    assert o.status is OrderStatus.EXPIRED and "RECOVERY" in [ev.kind for ev in e.ledger.events]


def test_ledger_projection_is_deterministic():
    runs = []
    for _ in range(3):
        e, _o = opened(qty_quote=75)
        for x in (q(2, 99.5, 100.0), q(5, 125.0, 125.5), q(6, 125.0, 125.5), q(7, 80.0, 81.0), q(8, 80.0, 81.0)):
            e.process_market_event(x)
        pr = project_ledger(e.ledger)
        runs.append((e.ledger.head, pr.cash, dict(pr.order_filled), pr.closed))
        assert reconcile(e) == []
    assert runs[0] == runs[1] == runs[2]


# ---------------- charges ----------------


def test_charge_schedule_matches_published_rates():
    """Rates as published for index options (NSE/BSE, SEBI, Stamp Act, GST, Budget 2026-27 STT)."""
    s = schedule_for(date(2026, 11, 2))
    assert s.stt_sell_pct == D("0.15") and s.stamp_buy_pct == D("0.003") and s.gst_pct == D("18")
    assert s.exchange_txn_pct == {"NSE": D("0.03503"), "BSE": D("0.0325")} and s.sebi_per_crore == D("10")
    assert schedule_for(date(2025, 6, 1)).stt_sell_pct == D("0.1")
    assert [x.effective_from for x in SCHEDULES] == sorted(x.effective_from for x in SCHEDULES)


def test_gst_excludes_stt_and_stamp_and_brokerage_once_per_order():
    s = schedule_for(date(2026, 11, 2))
    sell = compute_charges("SELL", D("200"), 750, "NSE", s, include_brokerage=True)
    assert sell.stt == D("225.00")  # 0.15% of 150000
    assert sell.gst == ((sell.brokerage + sell.exchange_txn + sell.sebi) * D("0.18")).quantize(D("0.01"))
    buy = compute_charges("BUY", D("200"), 750, "BSE", s, include_brokerage=False)
    assert buy.brokerage == 0 and buy.stt == 0 and buy.stamp == D("4.50") and buy.exchange_txn == D("48.75")


def test_fill_charges_use_schedule_of_fill_date():
    e, o = opened()
    f = e.fills[o.fills[0]]
    assert f.charges.schedule_version == "in-idxopt-2026-04-01"
