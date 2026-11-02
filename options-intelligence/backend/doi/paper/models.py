"""Paper orders, fills, positions and trades, with guarded state machines.

Lifecycle (blueprint section 10):
  CANDIDATE -> RISK_APPROVED -> PAPER_ORDER_PENDING
    -> PARTIALLY_FILLED / FILLED / REJECTED / EXPIRED / CANCELLED
    -> POSITION_OPEN -> EXIT_PENDING -> CLOSED -> EVALUATED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from doi.core.errors import DoiError
from doi.core.models import Instrument
from doi.paper.charges import ChargeBreakdown


class InvalidTransition(DoiError):
    pass


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Purpose(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class OrderStatus(StrEnum):
    PENDING = "PAPER_ORDER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PositionState(StrEnum):
    OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    EVALUATED = "EVALUATED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    UNDERLYING_INVALIDATION = "UNDERLYING_INVALIDATION"
    TARGET = "TARGET"
    TIME_SQUARE_OFF = "TIME_SQUARE_OFF"
    MAX_HOLDING = "MAX_HOLDING"
    EXPIRY = "EXPIRY"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"


_ORDER_NEXT = {
    OrderStatus.PENDING: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELLED,
    },
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.CANCELLED},
}
_POS_NEXT = {
    PositionState.OPEN: {PositionState.EXIT_PENDING},
    PositionState.EXIT_PENDING: {PositionState.OPEN, PositionState.CLOSED},
    PositionState.CLOSED: {PositionState.EVALUATED},
}
ACTIVE_ORDER = frozenset({OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED})
LIVE_POSITION = frozenset({PositionState.OPEN, PositionState.EXIT_PENDING})


@dataclass(slots=True)
class Fill:
    id: str
    order_id: str
    idempotency_key: str
    ts: datetime
    side: Side
    price: Decimal
    quantity: int
    reference_price: Decimal  # touch (bid/ask) the fill was priced from
    slippage: Decimal  # per unit, adverse
    charges: ChargeBreakdown
    fill_model_version: str


@dataclass(slots=True)
class PaperOrder:
    id: str
    account_id: str
    idempotency_key: str
    signal_id: str
    position_id: str
    purpose: Purpose
    side: Side
    contract: Instrument
    quantity: int
    submitted_at: datetime
    eligible_at: datetime
    expires_at: datetime | None
    limit_price: Decimal | None = None
    reason: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    fills: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_qty

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_ORDER

    def transition(self, new: OrderStatus) -> None:
        if new not in _ORDER_NEXT.get(self.status, set()):
            raise InvalidTransition(f"order {self.id}: {self.status} -> {new}")
        self.status = new


@dataclass(slots=True)
class TargetLeg:
    price: Decimal
    fraction: float  # of entered quantity; floored to whole lots, last target takes the remainder
    done: bool = False


@dataclass(slots=True)
class Position:
    id: str
    account_id: str
    signal_id: str
    underlying_key: str
    contract: Instrument
    stop_loss: Decimal
    targets: list[TargetLeg]
    underlying_invalidation: float | None
    is_call: bool
    config_version: str
    risk_snapshot: dict
    state: PositionState = PositionState.OPEN
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    qty_entered: int = 0
    qty_exited: int = 0
    entry_cost: Decimal = Decimal(0)  # sum(price * qty) of entry fills
    exit_proceeds: Decimal = Decimal(0)
    realized_gross: Decimal = Decimal(0)
    charges: Decimal = Decimal(0)
    mark: Decimal | None = None
    mark_ts: datetime | None = None
    mark_stale: bool = False
    mfe: Decimal = Decimal(0)  # per unit, vs average entry, at executable bid
    mae: Decimal = Decimal(0)
    exit_reason: ExitReason | None = None
    entry_order_id: str | None = None
    exit_order_id: str | None = None  # at most one working exit order

    @property
    def qty_open(self) -> int:
        return self.qty_entered - self.qty_exited

    @property
    def avg_entry(self) -> Decimal:
        return (self.entry_cost / self.qty_entered).quantize(Decimal("0.0001")) if self.qty_entered else Decimal(0)

    def transition(self, new: PositionState) -> None:
        if new not in _POS_NEXT.get(self.state, set()):
            raise InvalidTransition(f"position {self.id}: {self.state} -> {new}")
        self.state = new


@dataclass(frozen=True, slots=True)
class Trade:
    position_id: str
    signal_id: str
    underlying_key: str
    security_id: str
    quantity: int
    avg_entry: Decimal
    avg_exit: Decimal
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    mfe: Decimal
    mae: Decimal
    exit_reason: str
    opened_at: datetime
    closed_at: datetime
    config_version: str
