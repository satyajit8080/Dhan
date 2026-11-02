"""Canonical, broker-neutral data models. Timestamps are timezone-aware UTC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

OptionType = Literal["CE", "PE"]
ExchangeFamily = Literal["NSE", "BSE"]


@dataclass(frozen=True, slots=True)
class Instrument:
    security_id: str
    symbol: str
    exchange: str
    exchange_segment: str
    instrument: str
    underlying_symbol: str | None = None
    underlying_security_id: str | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    lot_size: int | None = None
    tick_size: float | None = None


@dataclass(frozen=True, slots=True)
class ExpiryProfile:
    expiry: date
    lot_size: int
    strike_step: float
    contracts: int


@dataclass(frozen=True, slots=True)
class UnderlyingRef:
    """An index resolved from the instrument master. No hard-coded ids."""

    key: str
    exchange_family: ExchangeFamily
    security_id: str
    segment: str  # underlying segment for option-chain calls (IDX_I)
    derivative_segment: str  # NSE_FNO / BSE_FNO
    master_version: str
    expiries: tuple[ExpiryProfile, ...]

    def profile(self, expiry: date) -> ExpiryProfile | None:
        return next((p for p in self.expiries if p.expiry == expiry), None)


@dataclass(frozen=True, slots=True)
class OptionLeg:
    security_id: str | None
    ltp: float | None
    oi: int | None
    prev_oi: int | None
    volume: int | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    bid: float | None
    bid_qty: int | None
    ask: float | None
    ask_qty: int | None

    @property
    def change_oi(self) -> int | None:
        return None if self.oi is None or self.prev_oi is None else self.oi - self.prev_oi

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.ask >= self.bid


@dataclass(frozen=True, slots=True)
class ChainRow:
    strike: float
    ce: OptionLeg | None
    pe: OptionLeg | None


@dataclass(frozen=True, slots=True)
class OptionChainSnapshot:
    underlying_key: str
    expiry: date
    request_key: str
    requested_at: datetime
    received_at: datetime
    underlying_ltp: float | None
    rows: tuple[ChainRow, ...]
    master_version: str
    source_ts: datetime | None = None  # Dhan chain carries no source timestamp

    def age_s(self, now: datetime) -> float:
        return (now - (self.source_ts or self.received_at)).total_seconds()


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    oi: float | None = None


@dataclass(frozen=True, slots=True)
class OptionBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    oi: float | None = None
    iv: float | None = None
    strike: float | None = None
    spot: float | None = None
    extra: dict = field(default_factory=dict, compare=False, hash=False)
