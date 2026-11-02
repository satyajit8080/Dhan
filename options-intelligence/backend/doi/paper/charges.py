"""Date-effective charge schedules for long index-option trades.

Values are research defaults and must be reconciled against real contract notes
before any result is trusted. Every fill stores the schedule version it used.
Brokerage is per executed order (charged once, on the order's first fill)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from doi.core.errors import ConfigError

D = Decimal
PAISA = D("0.01")


def money(x: Decimal) -> Decimal:
    return x.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ChargeSchedule:
    version: str
    effective_from: date
    brokerage_per_order: Decimal
    stt_sell_pct: Decimal  # % of premium turnover, sell side
    stamp_buy_pct: Decimal  # % of premium turnover, buy side
    exchange_txn_pct: Mapping[str, Decimal] = field(default_factory=dict)  # by exchange
    sebi_per_crore: Decimal = D("10")
    gst_pct: Decimal = D("18")


SCHEDULES: tuple[ChargeSchedule, ...] = (
    ChargeSchedule(
        version="in-idxopt-2024-10-01",
        effective_from=date(2024, 10, 1),
        brokerage_per_order=D("20"),
        stt_sell_pct=D("0.1"),
        stamp_buy_pct=D("0.003"),
        exchange_txn_pct={"NSE": D("0.03503"), "BSE": D("0.0325")},
    ),
    # NSE circular NSE/FA/73061 (27 Feb 2026) revised transaction charges from 1 Mar 2026.
    # 0.03553% of options premium per published broker schedules (Zerodha; Oquilia ₹3,552.99/crore).
    # The official PDF was not reachable from the build environment: secondary-source verified.
    ChargeSchedule(
        version="in-idxopt-2026-03-01",
        effective_from=date(2026, 3, 1),
        brokerage_per_order=D("20"),
        stt_sell_pct=D("0.1"),
        stamp_buy_pct=D("0.003"),
        exchange_txn_pct={"NSE": D("0.03553"), "BSE": D("0.0325")},
    ),
    # Union Budget 2026-27: STT on sold option premium 0.10% -> 0.15% from 1 Apr 2026.
    ChargeSchedule(
        version="in-idxopt-2026-04-01",
        effective_from=date(2026, 4, 1),
        brokerage_per_order=D("20"),
        stt_sell_pct=D("0.15"),
        stamp_buy_pct=D("0.003"),
        exchange_txn_pct={"NSE": D("0.03553"), "BSE": D("0.0325")},
    ),
)


def schedule_for(d: date, schedules: tuple[ChargeSchedule, ...] = SCHEDULES) -> ChargeSchedule:
    eligible = [s for s in schedules if s.effective_from <= d]
    if not eligible:
        raise ConfigError(f"no charge schedule effective on {d}")
    return max(eligible, key=lambda s: s.effective_from)


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal
    schedule_version: str

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp + self.gst

    def __add__(self, other: ChargeBreakdown) -> ChargeBreakdown:
        return ChargeBreakdown(
            self.brokerage + other.brokerage,
            self.stt + other.stt,
            self.exchange_txn + other.exchange_txn,
            self.sebi + other.sebi,
            self.stamp + other.stamp,
            self.gst + other.gst,
            self.schedule_version if self.schedule_version == other.schedule_version else "mixed",
        )


def compute_charges(
    side: Literal["BUY", "SELL"],
    price: Decimal,
    quantity: int,
    exchange: str,
    schedule: ChargeSchedule,
    *,
    include_brokerage: bool,
) -> ChargeBreakdown:
    if quantity <= 0 or price <= 0:
        raise ValueError("charges need positive price and quantity")
    try:
        txn_pct = schedule.exchange_txn_pct[exchange.upper()]
    except KeyError:
        raise ConfigError(f"{schedule.version}: no exchange charge for {exchange}") from None
    turnover = price * quantity
    brokerage = money(schedule.brokerage_per_order) if include_brokerage else D("0")
    stt = money(turnover * schedule.stt_sell_pct / 100) if side == "SELL" else D("0")
    stamp = money(turnover * schedule.stamp_buy_pct / 100) if side == "BUY" else D("0")
    txn = money(turnover * txn_pct / 100)
    sebi = money(turnover * schedule.sebi_per_crore / D("10000000"))
    gst = money((brokerage + txn + sebi) * schedule.gst_pct / 100)
    return ChargeBreakdown(brokerage, stt, txn, sebi, stamp, gst, schedule.version)
