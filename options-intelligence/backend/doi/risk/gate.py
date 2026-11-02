"""Deterministic, independent risk gate. A candidate only becomes a paper order
if every check passes. All checks are always evaluated, so a rejection lists
every reason, not just the first.

Sizing (blueprint section 9):
    risk_budget  = equity * risk_pct
    risk_per_lot = est_max_loss_per_lot + est_round_trip_charges_per_lot
    lots         = floor(min(risk_budget, aggregate headroom, daily headroom) / risk_per_lot)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from doi.core.market import SessionCalendar
from doi.paper.charges import ChargeSchedule, compute_charges, schedule_for
from doi.paper.fills import FillModel, to_dec
from doi.risk.models import (
    AccountRiskState,
    Candidate,
    Check,
    Direction,
    LiquidityLimits,
    MarketState,
    RiskConfig,
    RiskDecision,
)

D = Decimal


class RiskGate:
    def __init__(
        self,
        config: RiskConfig,
        fill_model: FillModel,
        calendar: SessionCalendar,
        liquidity: dict[str, LiquidityLimits],
        schedule_lookup=schedule_for,
    ) -> None:
        self.config = config
        self.fill_model = fill_model
        self.calendar = calendar
        self.liquidity = liquidity
        self._schedule = schedule_lookup

    def round_trip_charges_per_lot(
        self, entry: Decimal, exit_: Decimal, lot: int, exchange: str, sched: ChargeSchedule
    ) -> Decimal:
        buy = compute_charges("BUY", entry, lot, exchange, sched, include_brokerage=True)
        sell = compute_charges("SELL", exit_, lot, exchange, sched, include_brokerage=True)
        return buy.total + sell.total

    def approve(self, cand: Candidate, account: AccountRiskState, market: MarketState, now: datetime) -> RiskDecision:
        cfg, fm = self.config, self.fill_model
        checks: list[Check] = []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        c = cand.contract
        lot = c.lot_size or 0
        tick = to_dec(c.tick_size or 0.05)
        lim = self.liquidity.get(cand.underlying_key)

        check("kill_switch", not account.kill_switch, "kill switch engaged" if account.kill_switch else "off")
        session_ok = self.calendar.is_open(now) and now < self.calendar.at(self.calendar.session_date(now), cfg.last_entry_time)
        check(
            "market_open", session_ok, "open" if session_ok else f"session closed or after {cfg.last_entry_time} IST entry cutoff"
        )
        check(
            "signal_validity",
            cand.created_at <= now <= cand.valid_until,
            f"valid {cand.created_at.isoformat()}..{cand.valid_until.isoformat()}",
        )
        want = "CE" if cand.direction is Direction.BUY_CE else "PE"
        expired = c.expiry is None or c.expiry < self.calendar.session_date(now)
        contract_ok = (
            lot > 0
            and c.option_type == want
            and not expired
            and not market.contract_ambiguous
            and not market.halted
            and c.security_id != ""
        )
        check(
            "contract",
            contract_ok,
            f"lot={lot} type={c.option_type}/{want} expiry={c.expiry} ambiguous={market.contract_ambiguous} halted={market.halted}",
        )
        check("index_config", lim is not None, "liquidity limits configured" if lim else f"no limits for {cand.underlying_key}")
        check(
            "data_quality",
            not market.data_stale
            and market.data_quality_score >= cfg.min_quality_score
            and market.coverage_status in cfg.allowed_coverage,
            f"stale={market.data_stale} score={market.data_quality_score:.1f}/{cfg.min_quality_score} coverage={market.coverage_status}",
        )

        q = market.quote
        quote_ok = q is not None and q.two_sided and bool(q.ask_qty) and bool(q.bid_qty) and q.security_id == c.security_id
        check(
            "quote", quote_ok, "two-sided top of book" if quote_ok else "missing, one-sided, crossed, zero-qty or wrong contract"
        )
        if q is not None and lim is not None:
            age = (now - q.ts).total_seconds()
            check("quote_age", 0 <= age <= lim.max_quote_age_s, f"{age:.1f}s (max {lim.max_quote_age_s}s)")
            sp = q.spread_pct
            check(
                "spread",
                sp is not None and sp <= lim.max_spread_pct,
                f"{'n/a' if sp is None else f'{sp:.2f}'}% (max {lim.max_spread_pct}%)",
            )
            check("open_interest", (q.oi or 0) >= lim.min_oi, f"{q.oi} (min {lim.min_oi})")
            check("volume", (q.volume or 0) >= lim.min_volume, f"{q.volume} (min {lim.min_volume})")

        exposures = account.exposures
        check(
            "duplicate_contract", all(e.security_id != c.security_id for e in exposures), "no open/working exposure in contract"
        )
        check(
            "concurrent_positions",
            len(exposures) < cfg.max_concurrent_positions,
            f"{len(exposures)}/{cfg.max_concurrent_positions}",
        )
        group = cfg.correlation_groups.get(cand.underlying_key, cand.underlying_key)
        in_group = sum(1 for e in exposures if cfg.correlation_groups.get(e.underlying_key, e.underlying_key) == group)
        check(
            "correlated_exposure",
            in_group < cfg.max_positions_per_group,
            f"group {group}: {in_group}/{cfg.max_positions_per_group}",
        )
        cooling = (
            account.consecutive_losses >= cfg.loss_streak_limit
            and account.last_loss_at is not None
            and now - account.last_loss_at < timedelta(seconds=cfg.loss_streak_cooldown_s)
        )
        check("loss_streak", not cooling, f"{account.consecutive_losses} consecutive losses")

        daily_dd = max(D(0), account.daily_start_equity - account.equity)
        daily_cap = account.daily_start_equity * cfg.daily_loss_cap_pct / 100
        check("daily_loss_cap", daily_dd < daily_cap, f"drawdown {daily_dd} / cap {daily_cap:.2f}")

        entry = risk_per_lot = budget = None
        lots = 0
        if quote_ok and lot > 0 and lim is not None:
            sched = self._schedule(self.calendar.session_date(now))
            ask, bid = to_dec(q.ask), to_dec(q.bid)  # type: ignore[union-attr]
            entry = fm.buy_price(ask, tick)
            stop = to_dec(cand.stop_loss)
            stop_exit = max(stop - fm.slippage(stop, tick), tick)
            target_exit = to_dec(cand.targets[0].price)
            target_exit = target_exit - fm.slippage(target_exit, tick)
            geometry = stop < bid and target_exit > entry  # type: ignore[operator]
            check("stop_target_geometry", geometry, f"stop {stop} < bid {bid}; target exit {target_exit} > entry {entry}")
            if geometry:
                loss_unit = min((entry - stop_exit) * (1 + cfg.stop_gap_buffer_pct / 100), entry)  # type: ignore[operator]
                charges = self.round_trip_charges_per_lot(entry, stop_exit, lot, c.exchange, sched)  # type: ignore[arg-type]
                risk_per_lot = (loss_unit * lot + charges).quantize(D("0.01"))
                budget = account.equity * cfg.risk_per_trade_pct / 100
                agg_room = account.equity * cfg.aggregate_open_risk_pct / 100 - account.open_risk
                daily_room = daily_cap - daily_dd
                cash_lots = int(account.available_cash // (entry * lot + charges))  # type: ignore[operator]
                capacity = min(budget, agg_room, daily_room)
                lots = max(0, min(int(capacity // risk_per_lot), cfg.max_lots, cash_lots))
                check(
                    "sizing",
                    lots >= 1,
                    f"budget {budget:.2f} agg_room {agg_room:.2f} daily_room {daily_room:.2f} cash_lots {cash_lots} "
                    f"risk/lot {risk_per_lot} -> {lots} lot(s)",
                )
                reward = (target_exit - entry) * lot - charges  # type: ignore[operator]
                rr = reward / risk_per_lot if risk_per_lot > 0 else D(0)
                check("reward_risk", rr >= cfg.min_reward_risk, f"net {rr:.2f} (min {cfg.min_reward_risk})")

        approved = all(ch.passed for ch in checks) and lots >= 1
        return RiskDecision(
            approved=approved,
            lots=lots if approved else 0,
            quantity=lots * lot if approved else 0,
            entry_price_est=entry,
            risk_per_lot=risk_per_lot,
            risk_budget=budget,
            checks=tuple(checks),
            config_version=cfg.version,
        )
