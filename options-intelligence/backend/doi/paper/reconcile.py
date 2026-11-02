"""Ledger reconciliation: prove the engine's state equals what its own ledger says.

`project_ledger` replays only ledger events (ORDER_PENDING, FILL, POSITION_CLOSED, ...)
and is independent of the engine objects. `reconcile` compares that projection and
the cash book against orders, fills, positions and trades. Any problem means the
money or position state can't be trusted, and start-up must stop."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from doi.core.errors import DoiError
from doi.paper.engine import PaperExecution
from doi.paper.ledger import Ledger
from doi.paper.models import OrderStatus, PositionState, Purpose

D = Decimal


class ReconciliationError(DoiError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Projection:
    cash: Decimal = D(0)
    order_qty: dict[str, int] = field(default_factory=dict)
    order_filled: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    order_position: dict[str, str] = field(default_factory=dict)
    order_purpose: dict[str, str] = field(default_factory=dict)
    position_entered: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    position_exited: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fill_ids: set[str] = field(default_factory=set)
    closed: dict[str, Decimal] = field(default_factory=dict)  # position -> net pnl


def project_ledger(ledger: Ledger) -> Projection:
    pr = Projection()
    for ev in ledger.events:
        p = ev.payload
        if ev.kind == "ACCOUNT_OPENED":
            pr.cash += D(p["starting_capital"])
        elif ev.kind == "ORDER_PENDING":
            o = p["order"]
            pr.order_qty[o["id"]] = o["quantity"]
            pr.order_position[o["id"]] = o["position_id"]
            pr.order_purpose[o["id"]] = o["purpose"]
        elif ev.kind == "FILL":
            f = p["fill"]
            notional = D(f["price"]) * f["quantity"]
            charges = sum(D(v) for k, v in f["charges"].items() if k != "schedule_version")
            pr.cash += (-notional if f["side"] == "BUY" else notional) - charges
            pr.order_filled[f["order_id"]] += f["quantity"]
            pid = pr.order_position[f["order_id"]]
            (pr.position_entered if f["side"] == "BUY" else pr.position_exited)[pid] += f["quantity"]
            pr.fill_ids.add(f["id"])
        elif ev.kind == "POSITION_CLOSED":
            pr.closed[p["trade"]["position_id"]] = D(p["trade"]["net_pnl"])
    return pr


def reconcile(e: PaperExecution) -> list[str]:
    problems: list[str] = []
    try:
        e.ledger.verify()
    except DoiError as err:
        return [str(err)]
    pr = project_ledger(e.ledger)
    if pr.cash != e.cash:
        problems.append(f"cash: ledger projection {pr.cash} != cash book {e.cash}")

    by_ref: dict[str, Decimal] = defaultdict(D)
    for c in e.ledger.cash:
        by_ref[c.ref] += c.amount
    for f in e.fills.values():
        expect = (-f.price * f.quantity if f.side.value == "BUY" else f.price * f.quantity) - f.charges.total
        if by_ref.get(f.id) != expect:
            problems.append(f"fill {f.id}: cash entries {by_ref.get(f.id)} != {expect}")
    if set(e.fills) != pr.fill_ids:
        problems.append("fill set differs from ledger FILL events")

    for o in e.orders.values():
        fq = sum(e.fills[i].quantity for i in o.fills if i in e.fills)
        if fq != o.filled_qty or pr.order_filled.get(o.id, 0) != o.filled_qty:
            problems.append(f"order {o.id}: filled {o.filled_qty}, fills {fq}, ledger {pr.order_filled.get(o.id, 0)}")
        if (o.status is OrderStatus.FILLED) != (o.filled_qty == o.quantity):
            problems.append(f"order {o.id}: status {o.status} with {o.filled_qty}/{o.quantity}")
        if pr.order_qty.get(o.id) != o.quantity:
            problems.append(f"order {o.id}: quantity not in ledger")

    for p in list(e.positions.values()) + list(e._planned.values()):
        if p.qty_entered != pr.position_entered.get(p.id, 0) or p.qty_exited != pr.position_exited.get(p.id, 0):
            problems.append(f"position {p.id}: qty {p.qty_entered}/{p.qty_exited} != ledger")
        if not 0 <= p.qty_exited <= p.qty_entered:
            problems.append(f"position {p.id}: exited {p.qty_exited} > entered {p.qty_entered}")
        if p.state is PositionState.EVALUATED and p.id not in pr.closed:
            problems.append(f"position {p.id}: evaluated without POSITION_CLOSED")
        if p.id not in e._planned and p.state in (PositionState.OPEN, PositionState.EXIT_PENDING) and p.qty_open <= 0:
            problems.append(f"position {p.id}: live with zero quantity")
        if p.state is PositionState.EXIT_PENDING:
            xo = e.orders.get(p.exit_order_id or "")
            if xo is None or not xo.active or xo.purpose is not Purpose.EXIT:
                problems.append(f"position {p.id}: EXIT_PENDING without a working exit order")
    for t in e.trades:
        if t.net_pnl != t.gross_pnl - t.charges or pr.closed.get(t.position_id) != t.net_pnl:
            problems.append(f"trade {t.position_id}: pnl mismatch")
    return problems


def assert_consistent(e: PaperExecution) -> None:
    problems = reconcile(e)
    if problems:
        raise ReconciliationError(problems)
