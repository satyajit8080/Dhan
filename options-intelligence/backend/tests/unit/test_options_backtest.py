"""Phase 8: option-premium backtesting with full Indian charges, conservative fills and data-class labels.
All data here is SIMULATED (Black-76 prices on a synthetic path)."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import psycopg
import pytest

from doi.analytics import black76
from doi.core.market import IST, SessionCalendar
from doi.core.models import Bar, OptionBar
from doi.paper.charges import compute_charges, schedule_for
from doi.paper.fills import round_to_tick
from doi.research.lab import Condition, Lab, LabError, StrategySpec
from doi.research.options_backtest import PremiumParams, backtest_premium, premium_metrics, run_and_record

CAL = SessionCalendar()
START = datetime(2026, 9, 1, 3, 45, tzinfo=UTC)
EXPIRY_CLOSE = datetime(2026, 9, 29, 15, 30, tzinfo=IST)


def path(n_days=3, drift=2.0, shock=None):
    out, d, made, level = [], START, 0, 24000.0
    while made < n_days:
        if CAL.is_trading_day(CAL.session_date(d)):
            for i in range(75):
                c = level + drift * i + 5 * math.sin(i / 2)
                lo = c - 6 - (shock if shock and made == n_days - 1 and i == 40 else 0)
                out.append(Bar(d + timedelta(minutes=5 * i), c - 1, c + 6, lo, c))
            level = out[-1].close
            made += 1
        d += timedelta(days=1)
    return out


def chains(under, offsets=range(-2, 3), sigma=0.14):
    calls, puts = {o: [] for o in offsets}, {o: [] for o in offsets}
    for b in under:
        t = max((EXPIRY_CLOSE - b.ts).total_seconds() / (365 * 86400), 1e-4)
        atm = round(b.open / 50) * 50
        for o in offsets:
            k = atm + 50 * o
            px = lambda s, call, k=k, t=t: black76.price(s, k, t, sigma, 1.0, call)
            calls[o].append(
                OptionBar(
                    b.ts, px(b.open, True), px(b.high, True), px(b.low, True), px(b.close, True), 1000, 5000, 14.0, k, b.open
                )
            )
            puts[o].append(
                OptionBar(
                    b.ts, px(b.open, False), px(b.low, False), px(b.high, False), px(b.close, False), 1000, 5000, 14.0, k, b.open
                )
            )
    return calls, puts


LONG = StrategySpec(name="prem-long", direction="LONG", entry_all=(Condition(feature="trend_regime", op="==", value="TREND_UP"),),
                    stop_atr=3.0, target_atr=2.0, exit_after_bars=6, max_trades_per_day=2)  # fmt: skip
P = PremiumParams(lot_size=65)


def test_premium_pnl_and_exact_charges():
    u = path()
    calls, puts = chains(u)
    trades = backtest_premium(LONG, u, calls, puts, P, CAL)
    assert trades and all(t.option_type == "CE" and t.quantity == 65 for t in trades)
    t = trades[0]
    entry_bar = next(b for b in calls[0] if b.ts == t.entry_ts)
    assert t.entry_price == round_to_tick(
        D(str(entry_bar.open)) * D("1.0075"), D("0.05"), up=True
    )  # open + half-spread + slippage
    sched = schedule_for(CAL.session_date(t.entry_ts))
    ch = (
        compute_charges("BUY", t.entry_price, 65, "NSE", sched, include_brokerage=True).total
        + compute_charges("SELL", t.exit_price, 65, "NSE", sched, include_brokerage=True).total
    )
    assert t.charges == ch and t.net == t.gross - ch and t.gross == (t.exit_price - t.entry_price) * 65
    m = premium_metrics(trades, CAL, 3)
    assert (
        m["trades"] == len(trades)
        and m["charges_inr"] > 0
        and m["net_pnl_inr"] == pytest.approx(sum(float(x.net) for x in trades))
    )
    assert sum(m["exit_reasons"].values()) == len(trades)


def test_costs_can_turn_gross_profit_negative():
    u = path(drift=0.4)
    calls, puts = chains(u)
    heavy = PremiumParams(lot_size=65, half_spread_bps=D("400"), slippage_bps=D("200"))
    m_light = premium_metrics(backtest_premium(LONG, u, calls, puts, P, CAL), CAL, 3)
    m_heavy = premium_metrics(backtest_premium(LONG, u, calls, puts, heavy, CAL), CAL, 3)
    assert m_heavy["net_pnl_inr"] < m_light["net_pnl_inr"]


def test_coverage_exit_when_strike_leaves_the_series():
    u = path(drift=30.0)  # fast move: the held strike drifts beyond ATM±2
    calls, puts = chains(u)
    trades = backtest_premium(LONG.model_copy(update={"target_atr": 50.0, "exit_after_bars": 40}), u, calls, puts, P, CAL)
    assert any(t.reason == "COVERAGE_EXIT" for t in trades)


def test_stop_fills_at_option_low():
    u = path(drift=0.5, shock=400.0)
    calls, puts = chains(u)
    trades = backtest_premium(
        LONG.model_copy(update={"stop_atr": 1.0, "target_atr": 50.0, "exit_after_bars": 74}), u, calls, puts, P, CAL
    )
    stops = [t for t in trades if t.reason == "STOP"]
    assert stops
    s = stops[-1]
    bar = next(b for b in calls[0] + calls[1] + calls[-1] + calls[2] + calls[-2] if b.ts == s.exit_ts and b.strike == s.strike)
    assert s.exit_price <= D(str(bar.low))  # never better than the bar's worst price


def test_recorded_with_data_class(pg_dsn):
    lab = Lab(pg_dsn)
    vid = lab.create_version(LONG, "human:test")
    u = path()
    calls, puts = chains(u)
    with pytest.raises(LabError):
        run_and_record(lab, vid, u, calls, puts, P, data_class="GUESSED", datasets={})
    with pytest.raises(LabError, match="HISTORICAL"):
        run_and_record(lab, vid, u, calls, puts, P, data_class="HISTORICAL", datasets={})
    r = run_and_record(lab, vid, u, calls, puts, P, data_class="SIMULATED", datasets={"generator": "black76-synthetic"})
    assert r["data_class"] == "SIMULATED" and r["trades"] > 0
    with psycopg.connect(pg_dsn) as c:
        assert (
            c.execute("SELECT data_class FROM research_option_backtests WHERE id = %s", (r["id"],)).fetchone()[0] == "SIMULATED"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM research_option_backtests")


def test_option_bars_keep_strike_spot_iv(pg_dsn):
    from doi.data.recorder import MarketRecorder, load_option_bars

    u = path(1)
    calls, _ = chains(u)
    MarketRecorder(pg_dsn).record_bars("ROLL:NIFTY:WEEK1:ATM:CALL:5m", 5, calls[0], "test")
    back = load_option_bars(pg_dsn, "ROLL:NIFTY:WEEK1:ATM:CALL:5m", 5, u[-1].ts + timedelta(minutes=5))
    assert len(back) == 75 and back[0].strike == calls[0][0].strike and back[0].spot == calls[0][0].spot and back[0].iv == 14.0
