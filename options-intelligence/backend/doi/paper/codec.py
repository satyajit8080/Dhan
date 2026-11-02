"""Lossless dict <-> object conversion for persisted paper entities.
Decimals travel as strings and datetimes as ISO-8601, so round trips are exact."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from doi.core.models import Instrument
from doi.paper.charges import ChargeBreakdown
from doi.paper.ledger import canonical
from doi.paper.models import (
    ExitReason,
    Fill,
    OrderStatus,
    PaperOrder,
    Position,
    PositionState,
    Purpose,
    Side,
    TargetLeg,
    Trade,
)
from doi.risk.models import Check, RiskDecision


def plain(obj: Any) -> Any:
    return json.loads(canonical(obj))


def _dt(v: str | None) -> datetime | None:
    return None if v is None else datetime.fromisoformat(v)


def _dec(v: str | None) -> Decimal | None:
    return None if v is None else Decimal(v)


def instrument_from(d: dict) -> Instrument:
    return Instrument(**{**d, "expiry": date.fromisoformat(d["expiry"]) if d.get("expiry") else None})


def order_to(o: PaperOrder) -> dict:
    return plain({f: getattr(o, f) for f in PaperOrder.__slots__})


def order_from(d: dict) -> PaperOrder:
    return PaperOrder(
        id=d["id"],
        account_id=d["account_id"],
        idempotency_key=d["idempotency_key"],
        signal_id=d["signal_id"],
        position_id=d["position_id"],
        purpose=Purpose(d["purpose"]),
        side=Side(d["side"]),
        contract=instrument_from(d["contract"]),
        quantity=d["quantity"],
        submitted_at=_dt(d["submitted_at"]),  # type: ignore[arg-type]
        eligible_at=_dt(d["eligible_at"]),  # type: ignore[arg-type]
        expires_at=_dt(d["expires_at"]),
        limit_price=_dec(d["limit_price"]),
        reason=d["reason"],
        status=OrderStatus(d["status"]),
        filled_qty=d["filled_qty"],
        fills=list(d["fills"]),
    )


def position_to(p: Position, *, planned: bool = False) -> dict:
    d = plain({f: getattr(p, f) for f in Position.__slots__})
    if planned:
        d["state"] = "PLANNED"
    return d


def position_from(d: dict) -> tuple[Position, bool]:
    planned = d["state"] == "PLANNED"
    p = Position(
        id=d["id"],
        account_id=d["account_id"],
        signal_id=d["signal_id"],
        underlying_key=d["underlying_key"],
        contract=instrument_from(d["contract"]),
        stop_loss=Decimal(d["stop_loss"]),
        targets=[TargetLeg(Decimal(t["price"]), t["fraction"], t["done"]) for t in d["targets"]],
        underlying_invalidation=d["underlying_invalidation"],
        is_call=d["is_call"],
        config_version=d["config_version"],
        risk_snapshot=d["risk_snapshot"],
        state=PositionState.OPEN if planned else PositionState(d["state"]),
        opened_at=_dt(d["opened_at"]),
        closed_at=_dt(d["closed_at"]),
        qty_entered=d["qty_entered"],
        qty_exited=d["qty_exited"],
        entry_cost=Decimal(d["entry_cost"]),
        exit_proceeds=Decimal(d["exit_proceeds"]),
        realized_gross=Decimal(d["realized_gross"]),
        charges=Decimal(d["charges"]),
        mark=_dec(d["mark"]),
        mark_ts=_dt(d["mark_ts"]),
        mark_stale=d["mark_stale"],
        mfe=Decimal(d["mfe"]),
        mae=Decimal(d["mae"]),
        exit_reason=ExitReason(d["exit_reason"]) if d["exit_reason"] else None,
        entry_order_id=d["entry_order_id"],
        exit_order_id=d["exit_order_id"],
    )
    return p, planned


def fill_to(f: Fill) -> dict:
    return plain({k: getattr(f, k) for k in Fill.__slots__})


def fill_from(d: dict) -> Fill:
    c = d["charges"]
    return Fill(
        id=d["id"],
        order_id=d["order_id"],
        idempotency_key=d["idempotency_key"],
        ts=_dt(d["ts"]),  # type: ignore[arg-type]
        side=Side(d["side"]),
        price=Decimal(d["price"]),
        quantity=d["quantity"],
        reference_price=Decimal(d["reference_price"]),
        slippage=Decimal(d["slippage"]),
        charges=ChargeBreakdown(
            *(Decimal(c[k]) for k in ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst")), c["schedule_version"]
        ),
        fill_model_version=d["fill_model_version"],
    )


def trade_to(t: Trade) -> dict:
    return plain(t)


def trade_from(d: dict) -> Trade:
    dec = {"avg_entry", "avg_exit", "gross_pnl", "charges", "net_pnl", "mfe", "mae"}
    dts = {"opened_at", "closed_at"}
    return Trade(**{k: Decimal(v) if k in dec else _dt(v) if k in dts else v for k, v in d.items()})


def decision_to(dec: RiskDecision | None) -> dict | None:
    return None if dec is None else plain(dec)


def decision_from(d: dict | None) -> RiskDecision | None:
    if d is None:
        return None
    return RiskDecision(
        approved=d["approved"],
        lots=d["lots"],
        quantity=d["quantity"],
        entry_price_est=_dec(d["entry_price_est"]),
        risk_per_lot=_dec(d["risk_per_lot"]),
        risk_budget=_dec(d["risk_budget"]),
        checks=tuple(Check(**c) for c in d["checks"]),
        config_version=d["config_version"],
    )
