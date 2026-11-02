"""Risk-gate inputs and outputs (blueprint section 9)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from doi.core.errors import ConfigError
from doi.core.market import Quote
from doi.core.models import Instrument

D = Decimal


class Direction(StrEnum):
    BUY_CE = "BUY_CE_CANDIDATE"
    BUY_PE = "BUY_PE_CANDIDATE"


@dataclass(frozen=True, slots=True)
class Target:
    price: float
    fraction: float  # share of the entered quantity to exit at this target


@dataclass(frozen=True, slots=True)
class Candidate:
    signal_id: str
    underlying_key: str
    direction: Direction
    contract: Instrument
    created_at: datetime
    valid_until: datetime
    stop_loss: float  # option premium level
    targets: tuple[Target, ...]
    config_version: str
    underlying_invalidation: float | None = None  # CE: spot <= level exits; PE: spot >= level exits
    feature_snapshot_id: str | None = None
    strategy: str = "manual"

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ConfigError("candidate needs a signal_id")
        if not self.targets:
            raise ConfigError("candidate needs at least one target")
        prices = [t.price for t in self.targets]
        if prices != sorted(prices) or len(set(prices)) != len(prices):
            raise ConfigError("targets must be strictly ascending")
        if any(t.fraction <= 0 for t in self.targets) or abs(sum(t.fraction for t in self.targets) - 1) > 1e-9:
            raise ConfigError("target fractions must be positive and sum to 1")
        if self.stop_loss <= 0 or self.stop_loss >= prices[0]:
            raise ConfigError("stop must be positive and below the first target")
        if self.valid_until <= self.created_at:
            raise ConfigError("candidate validity window is empty")


_GROUPS = MappingProxyType(
    {"NIFTY": "broad", "SENSEX": "broad", "BANKNIFTY": "banking", "BANKEX": "banking", "FINNIFTY": "financials"}
)


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Initial paper research defaults, not recommendations for live capital."""

    version: str = "risk-v1"
    risk_per_trade_pct: Decimal = D("0.50")
    daily_loss_cap_pct: Decimal = D("2.00")
    aggregate_open_risk_pct: Decimal = D("1.50")
    max_concurrent_positions: int = 3
    max_positions_per_group: int = 1
    correlation_groups: Mapping[str, str] = field(default_factory=lambda: _GROUPS)
    loss_streak_limit: int = 3
    loss_streak_cooldown_s: float = 3600.0
    min_reward_risk: Decimal = D("1.5")
    stop_gap_buffer_pct: Decimal = D("10")  # extra loss allowance for gaps through the stop
    min_quality_score: float = 80.0
    max_lots: int = 20
    last_entry_time: time = time(15, 0)  # IST; no new entries after this
    allowed_coverage: frozenset[str] = frozenset({"LIVE_OK"})


@dataclass(frozen=True, slots=True)
class LiquidityLimits:
    max_spread_pct: float
    min_oi: int
    min_volume: int
    max_quote_age_s: float


@dataclass(frozen=True, slots=True)
class MarketState:
    quote: Quote | None
    data_quality_score: float
    data_stale: bool
    coverage_status: str = "LIVE_OK"
    contract_ambiguous: bool = False
    halted: bool = False


@dataclass(frozen=True, slots=True)
class OpenExposure:
    underlying_key: str
    security_id: str


@dataclass(frozen=True, slots=True)
class AccountRiskState:
    equity: Decimal
    available_cash: Decimal
    daily_start_equity: Decimal
    open_risk: Decimal
    exposures: tuple[OpenExposure, ...]  # open positions and working entry orders
    consecutive_losses: int
    last_loss_at: datetime | None
    kill_switch: bool = False


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    lots: int
    quantity: int
    entry_price_est: Decimal | None
    risk_per_lot: Decimal | None
    risk_budget: Decimal | None
    checks: tuple[Check, ...]
    config_version: str

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{c.name}: {c.detail}" for c in self.checks if not c.passed)
