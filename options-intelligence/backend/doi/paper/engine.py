"""Deterministic paper-execution engine (blueprint sections 9-10, roadmap phase 6).

This class simulates only. It has no broker client, and nothing in it can forward an
order to Dhan or any other broker. Every method takes an explicit timestamp,
and ids come from counters, so the same inputs always produce the same ledger
(down to the final hash).

Concurrency: all methods are synchronous with no awaits, so under asyncio each
call is atomic. Callers must not share an instance across threads.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from doi.core.market import Quote, SessionCalendar
from doi.core.models import Instrument
from doi.paper.charges import compute_charges, money, schedule_for
from doi.paper.fills import round_to_tick, to_dec
from doi.paper.ledger import Ledger
from doi.paper.models import (
    ExitReason,
    Fill,
    InvalidTransition,
    OrderStatus,
    PaperOrder,
    Position,
    PositionState,
    Purpose,
    Side,
    TargetLeg,
    Trade,
)
from doi.risk.gate import RiskGate
from doi.risk.models import AccountRiskState, Candidate, Direction, MarketState, OpenExposure, RiskDecision

D = Decimal
FULL_EXITS = frozenset(set(ExitReason) - {ExitReason.TARGET})


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    square_off: time | None = time(15, 20)  # IST; intraday system: flatten before the close (None disables)
    max_holding_s: float | None = None
    entry_limit_buffer_pct: Decimal = D("2")  # protective limit above the risk-sized entry estimate


@dataclass(frozen=True, slots=True)
class SubmitResult:
    decision: RiskDecision | None
    order: PaperOrder | None
    duplicate: bool = False


class PaperExecution:
    def __init__(
        self,
        *,
        account_id: str,
        starting_capital: Decimal,
        gate: RiskGate,
        opened_at: datetime,
        ledger: Ledger | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        if starting_capital <= 0:
            raise ValueError("starting capital must be positive")
        self.account_id = account_id
        self.gate = gate
        self.fm = gate.fill_model
        self.cal: SessionCalendar = gate.calendar
        self.policy = policy or ExecutionPolicy()
        self.ledger = ledger or Ledger()
        self.starting_capital = starting_capital
        self.orders: dict[str, PaperOrder] = {}
        self.positions: dict[str, Position] = {}  # opened (>= 1 entry fill)
        self.fills: dict[str, Fill] = {}
        self.trades: list[Trade] = []
        self.kill_switch = False
        self.consecutive_losses = 0
        self.last_loss_at: datetime | None = None
        self.ignored_events = 0
        self._planned: dict[str, Position] = {}  # approved, entry working, no fill yet
        self._submissions: dict[str, SubmitResult] = {}
        self._fill_keys: set[str] = set()
        self._last_ts: dict[str, datetime] = {}
        self._n = defaultdict(int)
        self.ledger.append(
            "ACCOUNT_OPENED",
            opened_at,
            {
                "account_id": account_id,
                "starting_capital": starting_capital,
                "fill_model": self.fm,
                "risk_config": gate.config.version,
            },
        )
        self.ledger.post_cash(opened_at, account_id, "DEPOSIT", starting_capital, "opening")
        self._session: date = self.cal.session_date(opened_at)
        self.daily_start_equity = starting_capital

    # ---------- identity / accounting helpers ----------

    def _id(self, prefix: str) -> str:
        self._n[prefix] += 1
        return f"{prefix}-{self._n[prefix]:06d}"

    @property
    def cash(self) -> Decimal:
        return self.ledger.cash_balance()

    def _live(self) -> list[Position]:
        return [p for p in self.positions.values() if p.state in (PositionState.OPEN, PositionState.EXIT_PENDING)]

    def _pos(self, pid: str) -> Position:
        return self.positions.get(pid) or self._planned[pid]

    def equity(self) -> Decimal:
        return self.cash + sum(((p.mark if p.mark is not None else p.avg_entry) * p.qty_open for p in self._live()), D(0))

    def open_risk(self) -> Decimal:
        gap = 1 + self.gate.config.stop_gap_buffer_pct / 100
        total = D(0)
        for p in self._live():
            m = p.mark if p.mark is not None else p.avg_entry
            total += min(max(D(0), m - p.stop_loss) * gap, m) * p.qty_open
        for p in self._planned.values():
            o = self.orders[p.entry_order_id]  # type: ignore[index]
            if o.active and o.limit_price is not None:
                total += min((o.limit_price - p.stop_loss) * gap, o.limit_price) * o.remaining
        return money(total)

    def account_state(self, now: datetime) -> AccountRiskState:
        self._roll_day(now)
        exposures = [OpenExposure(p.underlying_key, p.contract.security_id) for p in self._live()]
        exposures += [OpenExposure(p.underlying_key, p.contract.security_id) for p in self._planned.values()]
        return AccountRiskState(
            equity=self.equity(),
            available_cash=self.cash,
            daily_start_equity=self.daily_start_equity,
            open_risk=self.open_risk(),
            exposures=tuple(exposures),
            consecutive_losses=self.consecutive_losses,
            last_loss_at=self.last_loss_at,
            kill_switch=self.kill_switch,
        )

    def _roll_day(self, ts: datetime) -> None:
        d = self.cal.session_date(ts)
        if d > self._session:
            self._session = d
            self.daily_start_equity = self.equity()
            self.ledger.append("DAY_ROLLOVER", ts, {"session": d, "daily_start_equity": self.daily_start_equity})

    # ---------- submission ----------

    def submit_simulated(self, cand: Candidate, market: MarketState, now: datetime) -> SubmitResult:
        key = f"{self.account_id}:{cand.signal_id}"
        prior = self._submissions.get(key)
        if prior is not None:
            self.ledger.append(
                "DUPLICATE_SUBMISSION_IGNORED",
                now,
                {"signal_id": cand.signal_id, "order_id": prior.order.id if prior.order else None},
            )
            return SubmitResult(prior.decision, prior.order, duplicate=True)

        state = self.account_state(now)
        self.ledger.append(
            "CANDIDATE_RECEIVED",
            now,
            {
                "signal_id": cand.signal_id,
                "underlying": cand.underlying_key,
                "direction": cand.direction,
                "security_id": cand.contract.security_id,
                "stop_loss": cand.stop_loss,
                "targets": cand.targets,
                "config_version": cand.config_version,
                "feature_snapshot_id": cand.feature_snapshot_id,
                "quote": market.quote,
            },
        )
        decision = self.gate.approve(cand, state, market, now)
        if not decision.approved:
            self.ledger.append(
                "RISK_REJECTED", now, {"signal_id": cand.signal_id, "reasons": decision.reasons, "checks": decision.checks}
            )
            self._submissions[key] = res = SubmitResult(decision, None)
            return res
        self.ledger.append("RISK_APPROVED", now, {"signal_id": cand.signal_id, "decision": decision})

        c = cand.contract
        tick = to_dec(c.tick_size or 0.05)
        limit = round_to_tick(decision.entry_price_est * (1 + self.policy.entry_limit_buffer_pct / 100), tick, up=True)  # type: ignore[operator]
        pid, oid = self._id("POS"), self._id("ORD")
        pos = Position(
            id=pid,
            account_id=self.account_id,
            signal_id=cand.signal_id,
            underlying_key=cand.underlying_key,
            contract=c,
            stop_loss=to_dec(cand.stop_loss),
            targets=[TargetLeg(to_dec(t.price), t.fraction) for t in cand.targets],
            underlying_invalidation=cand.underlying_invalidation,
            is_call=cand.direction is Direction.BUY_CE,
            config_version=cand.config_version,
            risk_snapshot={
                "lots": decision.lots,
                "risk_per_lot": str(decision.risk_per_lot),
                "risk_config": decision.config_version,
            },
            entry_order_id=oid,
        )
        order = PaperOrder(
            id=oid,
            account_id=self.account_id,
            idempotency_key=key,
            signal_id=cand.signal_id,
            position_id=pid,
            purpose=Purpose.ENTRY,
            side=Side.BUY,
            contract=c,
            quantity=decision.quantity,
            submitted_at=now,
            eligible_at=now + self.fm.latency,
            expires_at=cand.valid_until,
            limit_price=limit,
            reason="entry",
        )
        self._planned[pid] = pos
        self.orders[oid] = order
        self.ledger.append("ORDER_PENDING", now, {"order": self._order_view(order)})
        self._submissions[key] = res = SubmitResult(decision, order)
        return res

    # ---------- market events ----------

    def process_market_event(self, q: Quote) -> None:
        last = self._last_ts.get(q.security_id)
        if last is not None and q.ts <= last:
            self.ignored_events += 1  # duplicate or out-of-order tick: never re-fills
            return
        self._last_ts[q.security_id] = q.ts
        self._roll_day(q.ts)
        avail = {Side.BUY: q.ask_qty or 0, Side.SELL: q.bid_qty or 0}
        for o in sorted(
            (o for o in self.orders.values() if o.active and o.contract.security_id == q.security_id), key=lambda o: o.id
        ):
            if o.expires_at is not None and q.ts > o.expires_at:
                self._expire(o, q.ts)
            elif q.ts >= o.eligible_at:
                self._try_fill(o, q, avail)
        for p in self._live():
            if p.contract.security_id == q.security_id:
                self._mark(p, q)
                self._check_price_exits(p, q)
        self.advance_time(q.ts)

    def advance_time(self, now: datetime) -> None:
        self._roll_day(now)
        for o in sorted((o for o in self.orders.values() if o.active), key=lambda o: o.id):
            if o.expires_at is not None and now > o.expires_at:
                self._expire(o, now)
        today = self.cal.session_date(now)
        square = self.cal.at(today, self.policy.square_off) if self.policy.square_off else None
        for p in self._live():
            if p.contract.expiry is not None and p.contract.expiry < today:
                self._exit_all(p, ExitReason.EXPIRY, now)
            elif square is not None and now >= square:
                self._exit_all(p, ExitReason.TIME_SQUARE_OFF, now)
            elif self.policy.max_holding_s is not None and now - p.opened_at >= timedelta(seconds=self.policy.max_holding_s):  # type: ignore[operator]
                self._exit_all(p, ExitReason.MAX_HOLDING, now)

    def close_position(self, position_id: str, now: datetime, reason: ExitReason = ExitReason.MANUAL) -> PaperOrder | None:
        p = self.positions.get(position_id)
        if p is None or p.state not in (PositionState.OPEN, PositionState.EXIT_PENDING):
            raise InvalidTransition(f"position {position_id} is not open")
        self._exit_all(p, reason, now)
        return self.orders.get(p.exit_order_id) if p.exit_order_id else None

    def set_kill_switch(self, on: bool, now: datetime, *, flatten: bool = False) -> None:
        if on != self.kill_switch:
            self.kill_switch = on
            self.ledger.append("KILL_SWITCH", now, {"on": on, "flatten": flatten})
        if on:
            for p in list(self._planned.values()):
                self._cancel(self.orders[p.entry_order_id], now, "kill switch")  # type: ignore[index]
            if flatten:
                for p in self._live():
                    self._exit_all(p, ExitReason.KILL_SWITCH, now)

    # ---------- internals ----------

    def _try_fill(self, o: PaperOrder, q: Quote, avail: dict[Side, int]) -> None:
        c = o.contract
        tick, lot = to_dec(c.tick_size or 0.05), c.lot_size or 1
        if o.side is Side.BUY:
            ref = to_dec(q.ask) if q.ask and q.ask > 0 else None
            price = self.fm.buy_price(ref, tick, o.limit_price) if ref is not None else None
        else:
            ref = to_dec(q.bid) if q.bid and q.bid > 0 else None
            price = self.fm.sell_price(ref, tick, o.limit_price) if ref is not None else None
        if price is None:
            return  # no executable touch (or limit not reachable): stay pending, never fabricate
        qty = self.fm.fillable_qty(o.remaining, avail[o.side], lot)
        if qty <= 0:
            return
        sched = schedule_for(self.cal.session_date(q.ts))
        first = o.filled_qty == 0

        def charges_for(n: int):
            return compute_charges(o.side.value, price, n, c.exchange, sched, include_brokerage=first)

        if o.side is Side.BUY:
            while qty > 0 and price * qty + charges_for(qty).total > self.cash:
                qty -= lot
            if qty <= 0:
                if first:
                    self._finish(o, OrderStatus.REJECTED, q.ts, "insufficient paper cash at fill")
                else:
                    self._cancel(o, q.ts, "insufficient paper cash for remainder")
                return
        else:
            p = self._pos(o.position_id)
            if qty > p.qty_open:
                raise InvalidTransition(f"exit fill {qty} exceeds open quantity {p.qty_open} on {p.id}")

        fkey = f"{o.id}:{q.event_key}"
        if fkey in self._fill_keys:
            return
        ch = charges_for(qty)
        slip = (price - ref) if o.side is Side.BUY else (ref - price)
        fill = Fill(self._id("FIL"), o.id, fkey, q.ts, o.side, price, qty, ref, slip, ch, self.fm.version)
        self._fill_keys.add(fkey)
        self.fills[fill.id] = fill
        avail[o.side] -= qty
        o.filled_qty += qty
        o.fills.append(fill.id)
        o.transition(OrderStatus.FILLED if o.remaining == 0 else OrderStatus.PARTIALLY_FILLED)

        notional = price * qty
        if o.side is Side.BUY:
            self.ledger.post_cash(q.ts, self.account_id, "PREMIUM_DEBIT", -notional, fill.id)
        else:
            self.ledger.post_cash(q.ts, self.account_id, "PREMIUM_CREDIT", notional, fill.id)
        self.ledger.post_cash(q.ts, self.account_id, "CHARGES", -ch.total, fill.id)
        self.ledger.append("FILL", q.ts, {"fill": fill, "order_status": o.status, "cash_after": self.cash})

        p = self._pos(o.position_id)
        p.charges += ch.total
        if o.side is Side.BUY:
            if p.id in self._planned:
                del self._planned[p.id]
                self.positions[p.id] = p
                p.opened_at = q.ts
            p.qty_entered += qty
            p.entry_cost += notional
            self.ledger.append(
                "POSITION_OPEN" if p.qty_entered == qty else "POSITION_INCREASED",
                q.ts,
                {"position_id": p.id, "qty_open": p.qty_open, "avg_entry": p.avg_entry},
            )
        else:
            p.qty_exited += qty
            p.exit_proceeds += notional
            self.ledger.append("POSITION_REDUCED", q.ts, {"position_id": p.id, "qty_open": p.qty_open, "fill_id": fill.id})
            if o.status is OrderStatus.FILLED:
                p.exit_order_id = None
                if p.qty_open == 0:
                    self._close(p, q.ts)
                else:
                    p.transition(PositionState.OPEN)

    def _mark(self, p: Position, q: Quote) -> None:
        if q.bid is None or q.bid <= 0:
            p.mark_stale = True
            return
        bid = to_dec(q.bid)
        p.mark, p.mark_ts, p.mark_stale = bid, q.ts, False
        move = bid - p.avg_entry
        p.mfe, p.mae = max(p.mfe, move), min(p.mae, move)

    def _check_price_exits(self, p: Position, q: Quote) -> None:
        observed = q.bid if q.bid and q.bid > 0 else q.ltp  # trigger may observe LTP; fills never use it
        if observed is not None and to_dec(observed) <= p.stop_loss:
            self._exit_all(p, ExitReason.STOP_LOSS, q.ts)
            return
        inv, spot = p.underlying_invalidation, q.underlying_ltp
        if inv is not None and spot is not None and ((p.is_call and spot <= inv) or (not p.is_call and spot >= inv)):
            self._exit_all(p, ExitReason.UNDERLYING_INVALIDATION, q.ts)
            return
        if p.state is not PositionState.OPEN or not q.bid:
            return
        lot = p.contract.lot_size or 1
        for i, t in enumerate(p.targets):
            if t.done:
                continue
            if to_dec(q.bid) < t.price:
                break
            t.done = True
            last = all(x.done for x in p.targets[i + 1 :])
            qty = p.qty_open if last else min(p.qty_open, int(t.fraction * p.qty_entered) // lot * lot)
            if qty > 0:
                self._submit_exit(p, qty, ExitReason.TARGET, q.ts, f"target {i + 1} @ {t.price}")
                return

    def _exit_all(self, p: Position, reason: ExitReason, now: datetime) -> None:
        working = self.orders.get(p.exit_order_id) if p.exit_order_id else None
        if working is not None and working.active:
            if p.exit_reason in FULL_EXITS:
                return  # a full exit is already working
            self._cancel(working, now, f"superseded by {reason}")
        if p.qty_open > 0:
            self._submit_exit(p, p.qty_open, reason, now, reason.value)

    def _submit_exit(self, p: Position, qty: int, reason: ExitReason, now: datetime, note: str) -> None:
        if p.entry_order_id and self.orders[p.entry_order_id].active:
            self._cancel(self.orders[p.entry_order_id], now, "exit triggered")
        if qty <= 0 or qty > p.qty_open:
            raise InvalidTransition(f"exit qty {qty} invalid for open {p.qty_open} on {p.id}")
        oid = self._id("ORD")
        o = PaperOrder(
            id=oid,
            account_id=self.account_id,
            idempotency_key=f"{p.id}:exit:{oid}",
            signal_id=p.signal_id,
            position_id=p.id,
            purpose=Purpose.EXIT,
            side=Side.SELL,
            contract=p.contract,
            quantity=qty,
            submitted_at=now,
            eligible_at=now + self.fm.latency,
            expires_at=None,  # exits keep working until filled
            reason=note,
        )
        self.orders[oid] = o
        p.exit_order_id, p.exit_reason = oid, reason
        if p.state is PositionState.OPEN:
            p.transition(PositionState.EXIT_PENDING)
        self.ledger.append("EXIT_TRIGGERED", now, {"position_id": p.id, "reason": reason, "note": note, "qty": qty})
        self.ledger.append("ORDER_PENDING", now, {"order": self._order_view(o)})

    def _expire(self, o: PaperOrder, now: datetime) -> None:
        self._finish(o, OrderStatus.EXPIRED, now, "validity window elapsed")

    def _cancel(self, o: PaperOrder, now: datetime, why: str) -> None:
        if o.active:
            self._finish(o, OrderStatus.CANCELLED, now, why)

    def _finish(self, o: PaperOrder, status: OrderStatus, now: datetime, why: str) -> None:
        o.transition(status)
        self.ledger.append(f"ORDER_{status.value}", now, {"order_id": o.id, "filled_qty": o.filled_qty, "reason": why})
        if o.purpose is Purpose.ENTRY and o.position_id in self._planned:
            del self._planned[o.position_id]  # no fill ever happened: nothing to unwind
            self.ledger.append("ENTRY_ABANDONED", now, {"position_id": o.position_id, "signal_id": o.signal_id})

    def _close(self, p: Position, ts: datetime) -> None:
        p.transition(PositionState.CLOSED)
        p.closed_at = ts
        gross = p.exit_proceeds - p.entry_cost
        p.realized_gross = gross
        net = gross - p.charges
        trade = Trade(
            position_id=p.id,
            signal_id=p.signal_id,
            underlying_key=p.underlying_key,
            security_id=p.contract.security_id,
            quantity=p.qty_entered,
            avg_entry=p.avg_entry,
            avg_exit=(p.exit_proceeds / p.qty_exited).quantize(D("0.0001")),
            gross_pnl=money(gross),
            charges=money(p.charges),
            net_pnl=money(net),
            mfe=p.mfe,
            mae=p.mae,
            exit_reason=(p.exit_reason or ExitReason.MANUAL).value,
            opened_at=p.opened_at,  # type: ignore[arg-type]
            closed_at=ts,
            config_version=p.config_version,
        )
        self.trades.append(trade)
        self.ledger.append("POSITION_CLOSED", ts, {"trade": trade})
        if net < 0:
            self.consecutive_losses += 1
            self.last_loss_at = ts
        elif net > 0:
            self.consecutive_losses = 0
        p.transition(PositionState.EVALUATED)
        self.ledger.append(
            "TRADE_EVALUATED", ts, {"position_id": p.id, "net_pnl": trade.net_pnl, "streak": self.consecutive_losses}
        )

    # ---------- views ----------

    @staticmethod
    def _order_view(o: PaperOrder) -> dict:
        return {
            "id": o.id,
            "signal_id": o.signal_id,
            "position_id": o.position_id,
            "purpose": o.purpose,
            "side": o.side,
            "security_id": o.contract.security_id,
            "quantity": o.quantity,
            "filled_qty": o.filled_qty,
            "limit_price": o.limit_price,
            "status": o.status,
            "submitted_at": o.submitted_at,
            "eligible_at": o.eligible_at,
            "expires_at": o.expires_at,
            "reason": o.reason,
        }

    def watch(self) -> dict[tuple[str, date], list[Instrument]]:
        """Contracts needing quotes: working orders and live positions."""
        out: dict[tuple[str, date], dict[str, Instrument]] = defaultdict(dict)
        for o in self.orders.values():
            if o.active and o.contract.expiry:
                out[(self._pos(o.position_id).underlying_key, o.contract.expiry)][o.contract.security_id] = o.contract
        for p in self._live():
            if p.contract.expiry:
                out[(p.underlying_key, p.contract.expiry)][p.contract.security_id] = p.contract
        return {k: list(v.values()) for k, v in out.items()}

    def portfolio(self, now: datetime) -> dict:
        state = self.account_state(now)
        live = self._live()
        unreal = sum(((p.mark - p.avg_entry) * p.qty_open for p in live if p.mark is not None), D(0))
        return {
            "account_id": self.account_id,
            "paper_only": True,
            "cash": self.cash,
            "equity": state.equity,
            "daily_start_equity": self.daily_start_equity,
            "day_pnl": state.equity - self.daily_start_equity,
            "realized_net": sum((t.net_pnl for t in self.trades), D(0)),
            "unrealized": money(unreal),
            "open_risk": state.open_risk,
            "consecutive_losses": self.consecutive_losses,
            "kill_switch": self.kill_switch,
            "positions": [
                {
                    "id": p.id,
                    "signal_id": p.signal_id,
                    "underlying": p.underlying_key,
                    "security_id": p.contract.security_id,
                    "symbol": p.contract.symbol,
                    "state": p.state,
                    "qty_open": p.qty_open,
                    "avg_entry": p.avg_entry,
                    "mark": p.mark,
                    "mark_stale": p.mark_stale,
                    "stop_loss": p.stop_loss,
                    "targets": [{"price": t.price, "fraction": t.fraction, "done": t.done} for t in p.targets],
                    "mfe": p.mfe,
                    "mae": p.mae,
                    "exit_order_id": p.exit_order_id,
                }
                for p in live
            ],
            "working_orders": [self._order_view(o) for o in self.orders.values() if o.active],
            "ledger_head": self.ledger.head,
            "ledger_events": len(self.ledger.events),
        }
