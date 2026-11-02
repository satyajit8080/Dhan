"""Offline tests for the phase 6 paper simulator: charges, fill model, risk gate,
engine lifecycle, exits, idempotency, ledger integrity and determinism."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal as D

import pytest

from doi.core.errors import ConfigError
from doi.core.market import Quote, SessionCalendar
from doi.core.models import Instrument
from doi.paper.charges import compute_charges, schedule_for
from doi.paper.engine import ExecutionPolicy, PaperExecution
from doi.paper.fills import FillModel, PartialFillPolicy
from doi.paper.ledger import LedgerCorrupt
from doi.paper.models import ExitReason, InvalidTransition, OrderStatus, PositionState
from doi.risk.gate import RiskGate
from doi.risk.models import AccountRiskState, Candidate, Direction, LiquidityLimits, MarketState, OpenExposure, RiskConfig, Target

T0 = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # Monday 10:00 IST
CE = Instrument(
    "50001", "NIFTY 10 NOV 24000 CE", "NSE", "NSE_FNO", "OPTIDX", "NIFTY", "13", date(2026, 11, 10), 24000.0, "CE", 75, 0.05
)
PE = replace(CE, security_id="50002", symbol="NIFTY 10 NOV 24000 PE", option_type="PE")
BNF = replace(CE, security_id="60001", underlying_symbol="BANKNIFTY", lot_size=35)
LIMITS = {k: LiquidityLimits(2.0, 1000, 100, 15.0) for k in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX")}
FM = FillModel(latency_ms=250, slippage_ticks=1, slippage_bps=D(0))


def t(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def q(s, bid, ask, bq=750, aq=750, *, sec="50001", ltp=None, spot=24010.0, oi=50000, vol=10000):
    return Quote(sec, t(s), bid, bq, ask, aq, ltp, spot, oi, vol)


def cand(sig="S1", contract=CE, stop=90.0, targets=((125.0, 0.5), (135.0, 0.5)), valid_s=60, key="NIFTY", inv=None, at=0):
    return Candidate(
        sig,
        key,
        Direction.BUY_CE if contract.option_type == "CE" else Direction.BUY_PE,
        contract,
        t(at),
        t(at + valid_s),
        stop,
        tuple(Target(p, f) for p, f in targets),
        "0.1.0",
        underlying_invalidation=inv,
    )


def mkt(quote, **kw):
    return MarketState(quote=quote, data_quality_score=kw.pop("score", 100.0), data_stale=kw.pop("stale", False), **kw)


def gate(risk=None, fm=FM, cal=None):
    return RiskGate(risk or RiskConfig(), fm, cal or SessionCalendar(), LIMITS)


def engine(capital="500000", risk=None, fm=FM, policy=None):
    return PaperExecution(account_id="A", starting_capital=D(capital), gate=gate(risk, fm), opened_at=T0, policy=policy)


def opened(e=None, qty_quote=750):
    """Engine with a 2-lot NIFTY CE position entered at 100.05."""
    e = e or engine()
    res = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0))
    assert res.decision.approved and res.decision.lots == 2
    e.process_market_event(q(1, 99.5, 100.0, aq=qty_quote))
    return e, res.order


def acct(**kw):
    base = dict(
        equity=D(500000),
        available_cash=D(500000),
        daily_start_equity=D(500000),
        open_risk=D(0),
        exposures=(),
        consecutive_losses=0,
        last_loss_at=None,
    )
    return AccountRiskState(**{**base, **kw})


# ---------------- charges ----------------


def test_charge_components_and_rounding():
    s = schedule_for(date(2026, 11, 2))
    buy = compute_charges("BUY", D("100.05"), 150, "NSE", s, include_brokerage=True)
    assert (buy.brokerage, buy.stt, buy.stamp) == (D("20.00"), D("0"), D("0.45"))
    assert buy.exchange_txn == D("5.33") and buy.sebi == D("0.02") and buy.gst == D("4.56")  # NSE 0.03553% from 1 Mar 2026
    sell = compute_charges("SELL", D("125"), 150, "BSE", s, include_brokerage=False)
    assert sell.brokerage == 0 and sell.stamp == 0 and sell.stt == D("28.13")  # 0.15% after 1 Apr 2026
    assert buy.schedule_version == "in-idxopt-2026-04-01"


def test_schedule_is_date_effective():
    assert schedule_for(date(2026, 3, 31)).stt_sell_pct == D("0.1")
    assert schedule_for(date(2026, 4, 1)).stt_sell_pct == D("0.15")
    with pytest.raises(ConfigError):
        schedule_for(date(2020, 1, 1))


# ---------------- fill model ----------------


def test_fill_prices_use_touch_plus_adverse_slippage():
    fm = FillModel(slippage_ticks=1, slippage_bps=D("10"))
    tick = D("0.05")
    assert fm.buy_price(D("100"), tick) == D("100.10")  # 10 bps = 0.10 > 1 tick
    assert fm.sell_price(D("100"), tick) == D("99.90")
    assert fm.buy_price(D("100.01"), tick) == D("100.15")  # rounded up to tick, never favourable
    assert fm.sell_price(D("0.05"), tick) == D("0.05")  # floor at one tick
    assert fm.buy_price(D("101"), tick, limit=D("100.5")) is None
    assert fm.buy_price(D("100.45"), tick, limit=D("100.5")) == D("100.50")


def test_fillable_qty_whole_lots_and_policies():
    assert FM.fillable_qty(150, 100, 75) == 75
    assert FM.fillable_qty(150, 60, 75) == 0
    assert FM.fillable_qty(150, None, 75) == 0
    aon = FillModel(partial_policy=PartialFillPolicy.ALL_OR_NONE)
    assert aon.fillable_qty(150, 100, 75) == 0 and aon.fillable_qty(150, 150, 75) == 150
    assert FillModel(participation=D("0.5")).fillable_qty(150, 300, 75) == 150


# ---------------- risk gate ----------------


def test_gate_approves_and_sizes_from_risk_budget():
    d = gate().approve(cand(), acct(), mkt(q(0, 99.5, 100.0)), t(0))
    assert d.approved and d.lots == 2 and d.quantity == 150
    assert d.entry_price_est == D("100.05") and d.risk_per_lot > D("833")  # stop distance + gap buffer + charges


def test_gate_collects_every_failure_reason():
    bad = q(-30, 95.0, 100.0, oi=10, vol=1)  # stale, wide, thin
    d = gate().approve(cand(), acct(kill_switch=True), mkt(bad, stale=True, score=10), t(0))
    names = {c.name for c in d.checks if not c.passed}
    assert not d.approved and d.quantity == 0
    assert {"kill_switch", "data_quality", "quote_age", "spread", "open_interest", "volume"} <= names


@pytest.mark.parametrize(
    "now_s,expect",
    [(0, True), (-3600 * 2, False), (3600 * 5.1, False), (3600 * 24 * 5, False)],  # 08:00, 15:06 IST, Saturday
)
def test_gate_session_and_entry_cutoff(now_s, expect):
    c = cand(at=now_s)
    d = gate().approve(c, acct(), mkt(q(now_s, 99.5, 100.0)), t(now_s))
    assert any(ch.name == "market_open" and ch.passed == expect for ch in d.checks)


def test_gate_exposure_streak_and_daily_cap():
    g = gate()
    m = mkt(q(0, 99.5, 100.0))
    assert "duplicate_contract" in str(g.approve(cand(), acct(exposures=(OpenExposure("NIFTY", "50001"),)), m, t(0)).reasons)
    assert "correlated_exposure" in str(g.approve(cand(), acct(exposures=(OpenExposure("SENSEX", "999"),)), m, t(0)).reasons)
    streak = acct(consecutive_losses=3, last_loss_at=t(-60))
    assert "loss_streak" in str(g.approve(cand(), streak, m, t(0)).reasons)
    assert g.approve(cand(), replace(streak, last_loss_at=t(-7200)), m, t(0)).approved
    capped = acct(equity=D(490000), available_cash=D(490000))
    assert "daily_loss_cap" in str(g.approve(cand(), capped, m, t(0)).reasons)


def test_gate_reward_risk_and_cash():
    g = gate()
    m = mkt(q(0, 99.5, 100.0))
    assert "reward_risk" in str(g.approve(cand(targets=((105.0, 1.0),)), acct(), m, t(0)).reasons)
    assert "sizing" in str(g.approve(cand(), acct(available_cash=D(5000)), m, t(0)).reasons)
    assert "stop_target_geometry" in str(g.approve(cand(stop=99.6), acct(), m, t(0)).reasons)


def test_gate_contract_direction_mismatch():
    c = replace(cand(), contract=PE)
    assert "contract" in str(gate().approve(c, acct(), mkt(q(0, 99.5, 100.0, sec="50002")), t(0)).reasons)


# ---------------- engine: entries ----------------


def test_entry_waits_for_latency_and_fills_at_ask_not_ltp():
    e = engine()
    res = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0))
    assert res.order.status is OrderStatus.PENDING
    e.process_market_event(q(0.1, 99.5, 100.0, ltp=50.0))  # inside latency window
    assert res.order.filled_qty == 0
    e.process_market_event(q(1, 99.5, 100.0, ltp=50.0))
    f = e.fills[res.order.fills[0]]
    assert res.order.status is OrderStatus.FILLED and f.price == D("100.05") and f.reference_price == D("100.0")
    p = e.positions[res.order.position_id]
    assert p.state is PositionState.OPEN and p.qty_open == 150 and p.avg_entry == D("100.05")
    assert e.cash == D(500000) - D("100.05") * 150 - f.charges.total
    e.ledger.verify()


def test_partial_fill_then_complete_brokerage_once():
    e, o = opened(qty_quote=100)  # top-of-book shows 1 lot only
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 75
    e.process_market_event(q(2, 99.5, 100.1, aq=750))
    assert o.status is OrderStatus.FILLED
    f1, f2 = (e.fills[i] for i in o.fills)
    assert f1.charges.brokerage == D(20) and f2.charges.brokerage == 0 and f2.price == D("100.15")
    assert e.positions[o.position_id].avg_entry == D("100.1000")


def test_missing_ask_never_fills_and_entry_expires():
    e = engine()
    o = e.submit_simulated(cand(valid_s=30), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(1, 99.5, None))
    e.process_market_event(q(2, None, None))
    assert o.filled_qty == 0
    e.advance_time(t(31))
    assert o.status is OrderStatus.EXPIRED and not e.positions
    kinds = [ev.kind for ev in e.ledger.events]
    assert "ORDER_EXPIRED" in kinds and "ENTRY_ABANDONED" in kinds
    assert e.cash == D(500000)


def test_protective_limit_blocks_chasing():
    e = engine()
    o = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(1, 103.0, 103.5))  # > 2% above risk-sized entry
    assert o.filled_qty == 0 and o.limit_price == D("102.10")  # 100.05 * 1.02 rounded up to tick


def test_partially_filled_entry_expiry_keeps_position():
    e = engine()
    o = e.submit_simulated(cand(valid_s=10), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(1, 99.5, 100.0, aq=75))
    e.advance_time(t(11))
    assert o.status is OrderStatus.EXPIRED and e.positions[o.position_id].qty_open == 75


# ---------------- engine: exits ----------------


def test_stop_loss_gap_fills_at_real_bid():
    e, o = opened()
    pid = o.position_id
    e.process_market_event(q(5, 80.0, 81.0))  # gaps through 90 stop
    p = e.positions[pid]
    assert p.state is PositionState.EXIT_PENDING and p.exit_reason is ExitReason.STOP_LOSS
    e.process_market_event(q(6, 79.0, 80.0))
    assert p.state is PositionState.EVALUATED
    tr = e.trades[-1]
    assert tr.avg_exit == D("78.9500") and tr.exit_reason == "STOP_LOSS" and tr.net_pnl < 0
    assert tr.net_pnl == tr.gross_pnl - tr.charges and e.consecutive_losses == 1
    assert e.cash == D(500000) + tr.net_pnl
    e.ledger.verify()


def test_targets_scale_out_then_close():
    e, o = opened()
    p = e.positions[o.position_id]
    e.process_market_event(q(5, 125.0, 125.5))
    assert p.state is PositionState.EXIT_PENDING
    e.process_market_event(q(6, 125.0, 125.5))
    assert p.state is PositionState.OPEN and p.qty_open == 75 and p.targets[0].done
    e.process_market_event(q(7, 136.0, 136.5))
    e.process_market_event(q(8, 136.0, 136.5))
    tr = e.trades[-1]
    assert p.state is PositionState.EVALUATED and tr.exit_reason == "TARGET" and tr.net_pnl > 0
    assert tr.mfe == D("35.95") and e.consecutive_losses == 0


def test_stop_supersedes_pending_target_exit():
    e, o = opened()
    p = e.positions[o.position_id]
    e.process_market_event(q(5, 125.0, 125.5))
    target_order = e.orders[p.exit_order_id]
    e.process_market_event(q(6, 89.0, 90.0, bq=0))  # stop hit, no bid size: nothing fills
    assert target_order.status is OrderStatus.CANCELLED
    full = e.orders[p.exit_order_id]
    assert full.quantity == 150 and p.exit_reason is ExitReason.STOP_LOSS
    e.process_market_event(q(7, 89.0, 90.0))
    assert p.state is PositionState.EVALUATED and p.qty_exited == 150


def test_underlying_invalidation_exit():
    e = engine()
    o = e.submit_simulated(cand(inv=23950.0), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(1, 99.5, 100.0))
    e.process_market_event(q(2, 99.0, 99.5, spot=23940.0))
    assert e.positions[o.position_id].exit_reason is ExitReason.UNDERLYING_INVALIDATION


def test_time_square_off_and_max_holding():
    e, o = opened()
    e.advance_time(T0.replace(hour=9, minute=50))  # 15:20 IST
    p = e.positions[o.position_id]
    assert p.exit_reason is ExitReason.TIME_SQUARE_OFF
    e.process_market_event(Quote("50001", T0.replace(hour=9, minute=51), 99.0, 750, 99.5, 750))
    assert p.state is PositionState.EVALUATED

    e2, o2 = opened(engine(policy=ExecutionPolicy(max_holding_s=60)))
    e2.advance_time(t(62))
    assert e2.positions[o2.position_id].exit_reason is ExitReason.MAX_HOLDING


def test_exit_without_bid_stays_pending():
    e, o = opened()
    pid = o.position_id
    e.close_position(pid, t(5))
    e.process_market_event(q(6, None, 99.0))
    assert e.positions[pid].state is PositionState.EXIT_PENDING and e.positions[pid].mark_stale


# ---------------- idempotency and consistency ----------------


def test_duplicate_submission_returns_same_order():
    e = engine()
    a = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0))
    b = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0.5))
    assert b.duplicate and b.order is a.order and len(e.orders) == 1
    assert [ev.kind for ev in e.ledger.events].count("ORDER_PENDING") == 1
    r1 = e.submit_simulated(cand("S2", contract=PE), mkt(None), t(1))
    r2 = e.submit_simulated(cand("S2", contract=PE), mkt(None), t(2))
    assert not r1.decision.approved and r2.duplicate and r2.order is None


def test_second_signal_same_contract_rejected():
    e, _ = opened()
    d = e.submit_simulated(cand("S9"), mkt(q(2, 99.5, 100.0)), t(2)).decision
    assert not d.approved and "duplicate_contract" in str(d.reasons)


def test_duplicate_and_out_of_order_quotes_ignored():
    e = engine()
    o = e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)).order
    e.process_market_event(q(1, 99.5, 100.0, aq=75))
    e.process_market_event(q(1, 99.5, 100.0, aq=75))  # exact duplicate
    e.process_market_event(q(0.9, 99.5, 100.0, aq=75))  # stale replay
    assert o.filled_qty == 75 and e.ignored_events == 2 and len(e.fills) == 1


def test_invalid_transitions_raise():
    e, o = opened()
    with pytest.raises(InvalidTransition):
        o.transition(OrderStatus.PENDING)
    p = e.positions[o.position_id]
    e.close_position(p.id, t(5))
    e.process_market_event(q(6, 99.0, 99.5))
    with pytest.raises(InvalidTransition):
        p.transition(PositionState.OPEN)
    with pytest.raises(InvalidTransition):
        e.close_position(p.id, t(7))


def test_kill_switch_cancels_and_flattens():
    e, o = opened()
    w = e.submit_simulated(cand("S3", contract=BNF, key="BANKNIFTY"), mkt(q(2, 99.5, 100.0, sec="60001")), t(2)).order
    assert w is not None and w.active
    e.set_kill_switch(True, t(3), flatten=True)
    assert w.status is OrderStatus.CANCELLED and e.positions[o.position_id].exit_reason is ExitReason.KILL_SWITCH
    d = e.submit_simulated(cand("S4", contract=PE), mkt(q(4, 99.5, 100.0, sec="50002")), t(4)).decision
    assert "kill_switch" in str(d.reasons)


def test_day_rollover_resets_daily_start():
    e, _ = opened()
    e.process_market_event(q(5, 125.0, 125.5))
    e.process_market_event(q(6, 125.0, 125.5))
    e.advance_time(T0 + timedelta(days=1))
    assert e.daily_start_equity == e.equity() and "DAY_ROLLOVER" in [ev.kind for ev in e.ledger.events]


# ---------------- ledger ----------------


def scenario() -> PaperExecution:
    e, _ = opened()
    e.process_market_event(q(5, 125.0, 125.5))
    e.process_market_event(q(6, 125.0, 125.5))
    e.process_market_event(q(7, 80.0, 80.5))
    e.process_market_event(q(8, 80.0, 80.5))
    return e


def test_ledger_is_deterministic_and_reconciles():
    a, b = scenario(), scenario()
    assert a.ledger.head == b.ledger.head and len(a.ledger.events) == len(b.ledger.events)
    a.ledger.verify()
    assert a.cash == D(500000) + sum(tr.net_pnl for tr in a.trades)
    fills_cash = sum((f.price * f.quantity * (-1 if f.side == "BUY" else 1) - f.charges.total for f in a.fills.values()), D(0))
    assert a.cash == D(500000) + fills_cash


def test_ledger_tamper_detected():
    e = scenario()
    ev = e.ledger.events[3]
    e.ledger.events[3] = replace(ev, payload={**ev.payload, "tampered": True})
    with pytest.raises(LedgerCorrupt):
        e.ledger.verify()


def test_ledger_jsonl_sink(tmp_path):
    from doi.paper.ledger import Ledger

    path = tmp_path / "ledger.jsonl"
    e = PaperExecution(account_id="A", starting_capital=D(500000), gate=gate(), opened_at=T0, ledger=Ledger(path))
    e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0))
    assert len(path.read_text().splitlines()) == len(e.ledger.events)


def test_no_broker_capability_in_paper_package():
    import inspect

    import doi.paper.driver as drv
    import doi.paper.engine as eng

    src = inspect.getsource(eng) + inspect.getsource(drv)
    for banned in ("DhanTransport", "dhanhq", "place_order", "/orders", "httpx"):
        assert banned not in src
    assert not [m for m in dir(PaperExecution) if "broker" in m.lower() or ("live" in m.lower() and m != "_live")]


def test_cutoff_config_is_overridable():
    g = gate(RiskConfig(last_entry_time=time(15, 30)))
    assert g.approve(cand(at=3600 * 5.1), acct(), mkt(q(3600 * 5.1, 99.5, 100.0)), t(3600 * 5.1)).approved
