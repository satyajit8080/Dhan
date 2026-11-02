"""Option-premium backtester (Phase 8).

It replaces the index-point approximation with rupee P&L on real option prices:
  - Signals: StrategySpec conditions on causal underlying features (same engine as live).
  - Contract: the ATM strike of the chosen side (LONG -> CE, SHORT -> PE) at the next bar.
  - Pricing: the held contract is priced bar by bar from ATM-relative rolling series
    (Dhan /charts/rollingoption) by matching its fixed strike across the offset series.
    If no series carries that strike any more, the trade is force-closed (COVERAGE_EXIT).
  - Fills: Dhan's expired-options data has no historical bid/ask, so fills are conservative.
    Buys pay open + assumed half-spread + slippage. A stop exits at the bar LOW minus costs.
    Target and time exits use the bar close, never the high.
  - Costs: the date-effective charge schedule (brokerage per order, STT, exchange charges, SEBI fee,
    stamp duty, GST) is applied to every fill.
  - Every run carries a data class: HISTORICAL, SIMULATED or LIVE_RECORDED.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from doi.core.canonical import canonical
from doi.core.market import SessionCalendar
from doi.core.models import Bar, OptionBar
from doi.paper.charges import compute_charges, schedule_for
from doi.paper.fills import round_to_tick
from doi.research.lab import LabError, StrategySpec, feature_series

D = Decimal
DataClass = Literal["HISTORICAL", "SIMULATED", "LIVE_RECORDED"]


@dataclass(frozen=True)
class PremiumParams:
    lot_size: int
    lots: int = 1
    exchange: str = "NSE"
    tick: Decimal = D("0.05")
    half_spread_bps: Decimal = D("50")  # assumed half bid/ask spread (no historical quotes available)
    slippage_bps: Decimal = D("25")
    coverage_exit_penalty_bps: Decimal = D("100")


@dataclass(frozen=True)
class PremiumTrade:
    option_type: str
    strike: float
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    gross: Decimal
    charges: Decimal
    net: Decimal
    reason: str


def _buy(px: float, p: PremiumParams) -> Decimal:
    raw = D(str(px)) * (1 + (p.half_spread_bps + p.slippage_bps) / D(10_000))
    return round_to_tick(raw, p.tick, up=True)


def _sell(px: float, p: PremiumParams, extra_bps: Decimal = D(0)) -> Decimal:
    raw = D(str(px)) * (1 - (p.half_spread_bps + p.slippage_bps + extra_bps) / D(10_000))
    return max(round_to_tick(raw, p.tick, up=False), p.tick)


def _index(series: Mapping[int, Sequence[OptionBar]]) -> dict[datetime, dict[float, OptionBar]]:
    """ts -> {strike: bar} across all offset series of one option type."""
    out: dict[datetime, dict[float, OptionBar]] = {}
    for bars in series.values():
        for b in bars:
            if b.strike is not None:
                out.setdefault(b.ts, {})[round(b.strike, 4)] = b
    return out


def backtest_premium(
    spec: StrategySpec,
    underlying: Sequence[Bar],
    calls: Mapping[int, Sequence[OptionBar]],
    puts: Mapping[int, Sequence[OptionBar]],
    params: PremiumParams,
    cal: SessionCalendar,
    *,
    features: dict | None = None,
) -> list[PremiumTrade]:
    underlying = sorted(underlying, key=lambda b: b.ts)
    fs = features if features is not None else feature_series(underlying, spec.timeframe_min, cal)
    tf = timedelta(minutes=spec.timeframe_min)
    side = "CE" if spec.direction == "LONG" else "PE"
    book = _index(calls if side == "CE" else puts)
    atm = {b.ts: b for b in (calls if side == "CE" else puts).get(0, [])}
    qty = params.lot_size * params.lots
    trades: list[PremiumTrade] = []
    per_day: dict = {}
    i = 0
    while i < len(underlying) - 1:
        b = underlying[i]
        day = cal.session_date(b.ts)
        nxt = underlying[i + 1]
        if per_day.get(day, 0) >= spec.max_trades_per_day or cal.session_date(nxt.ts) != day:
            i += 1
            continue
        fv = fs.get(b.ts + tf)
        if fv is None or not all(c.holds(fv.value(c.feature)) for c in spec.entry_all) or not fv.value("atr"):
            i += 1
            continue
        a0 = atm.get(nxt.ts)
        if a0 is None or a0.strike is None or a0.spot is None:
            i += 1
            continue
        strike = round(a0.strike, 4)
        entry_px = _buy(a0.open, params)
        sign = 1 if side == "CE" else -1
        atr = fv.value("atr")
        stop_lvl = a0.spot - sign * spec.stop_atr * atr
        tgt_lvl = a0.spot + sign * spec.target_atr * atr
        exit_px, reason, exit_ts, j = None, "", nxt.ts, i + 1
        last_seen: OptionBar | None = a0
        held = 0
        while j < len(underlying):
            u = underlying[j]
            if cal.session_date(u.ts) != day:
                break
            ob = book.get(u.ts, {}).get(strike)
            if ob is None:
                exit_px, reason, exit_ts = _sell(last_seen.close, params, params.coverage_exit_penalty_bps), "COVERAGE_EXIT", u.ts
                break
            last_seen = ob
            held += 1
            hit_stop = (u.low <= stop_lvl) if sign == 1 else (u.high >= stop_lvl)
            hit_tgt = (u.high >= tgt_lvl) if sign == 1 else (u.low <= tgt_lvl)
            if hit_stop:  # stop first (even on the entry bar), filled at the option's worst price of the bar
                exit_px, reason, exit_ts = _sell(ob.low, params), "STOP", u.ts
                break
            if j > i + 1 and hit_tgt:  # a target on the entry bar is not credited (intrabar order unknown)
                exit_px, reason, exit_ts = _sell(ob.close, params), "TARGET", u.ts
                break
            if held >= spec.exit_after_bars:
                exit_px, reason, exit_ts = _sell(ob.close, params), "TIME", u.ts
                break
            j += 1
        if exit_px is None:
            exit_px, reason, exit_ts = _sell(last_seen.close, params), "SESSION_END", last_seen.ts
        sched = schedule_for(day)
        ch = (
            compute_charges("BUY", entry_px, qty, params.exchange, sched, include_brokerage=True).total
            + compute_charges("SELL", exit_px, qty, params.exchange, sched, include_brokerage=True).total
        )
        gross = (exit_px - entry_px) * qty
        trades.append(PremiumTrade(side, strike, nxt.ts, exit_ts, entry_px, exit_px, qty, gross, ch, gross - ch, reason))
        per_day[day] = per_day.get(day, 0) + 1
        i = max(j, i + 1)
    return trades


def premium_metrics(trades: Sequence[PremiumTrade], cal: SessionCalendar, sessions: int) -> dict:
    net = [float(t.net) for t in trades]
    gross = sum(float(t.gross) for t in trades)
    charges = sum(float(t.charges) for t in trades)
    wins, losses = [x for x in net if x > 0], [-x for x in net if x < 0]
    eq = peak = mdd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    daily: dict = {}
    for t in trades:
        k = cal.session_date(t.exit_ts)
        daily[k] = daily.get(k, 0.0) + float(t.net)
    days = list(daily.values()) + [0.0] * max(0, sessions - len(daily))
    sd = statistics.pstdev(days) if len(days) > 1 else 0.0
    down = math.sqrt(sum(min(0.0, d) ** 2 for d in days) / len(days)) if days else 0.0
    reasons: dict = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    return {
        "trades": len(net),
        "net_pnl_inr": round(sum(net), 2),
        "gross_pnl_inr": round(gross, 2),
        "charges_inr": round(charges, 2),
        "charges_share_of_gross": round(charges / abs(gross), 4) if gross else None,
        "win_rate": round(len(wins) / len(net), 4) if net else None,
        "profit_factor": round(sum(wins) / sum(losses), 4) if losses else None,
        "avg_trade_inr": round(sum(net) / len(net), 2) if net else None,
        "max_drawdown_inr": round(mdd, 2),
        "sharpe_daily": round(statistics.mean(days) / sd * math.sqrt(252), 4) if sd > 0 else None,
        "sortino_daily": round(statistics.mean(days) / down * math.sqrt(252), 4) if down > 0 else None,
        "exit_reasons": reasons,
        "sessions": sessions,
    }


def run_and_record(lab, version_id: str, underlying: Sequence[Bar], calls, puts, params: PremiumParams, *,
                   data_class: DataClass, datasets: dict) -> dict:  # fmt: skip
    """Run a premium backtest and store it immutably with its data class and inputs."""
    if data_class not in ("HISTORICAL", "SIMULATED", "LIVE_RECORDED"):
        raise LabError("data_class must be HISTORICAL, SIMULATED or LIVE_RECORDED")
    if data_class == "HISTORICAL" and not datasets:
        raise LabError("HISTORICAL runs must name their approved dataset ids")
    spec = lab.spec(version_id)
    trades = backtest_premium(spec, underlying, calls, puts, params, lab.calendar)
    n = len({lab.calendar.session_date(b.ts) for b in underlying})
    m = premium_metrics(trades, lab.calendar, n)
    m["data_class"] = data_class
    pdict = {k: str(v) for k, v in params.__dict__.items()}
    rid = "OBT-" + secrets.token_hex(5)
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(lab.dsn) as c:
        c.execute(
            "INSERT INTO research_option_backtests (id, strategy_version_id, data_class, datasets, params, metrics, metrics_sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                rid,
                version_id,
                data_class,
                Jsonb(datasets),
                Jsonb(pdict),
                Jsonb(json.loads(canonical(m))),
                hashlib.sha256(canonical(m).encode()).hexdigest(),
            ),
        )
    return {"id": rid, **m}
