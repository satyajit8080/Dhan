"""Broker-neutral data provider interface (blueprint section 14)."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from doi.core.models import Bar, OptionBar, OptionChainSnapshot, UnderlyingRef
from doi.data.historical_ingestion.planner import RollingOptionRequest


class MarketDataProvider(Protocol):
    async def get_expiries(self, underlying: UnderlyingRef) -> tuple[date, ...]: ...

    async def get_option_chain(
        self, underlying: UnderlyingRef, expiry: date, *, max_age_s: float | None = None
    ) -> OptionChainSnapshot: ...

    async def get_candles(
        self, security_id: str, exchange_segment: str, instrument: str, interval: str, start: date, end: date
    ) -> tuple[Bar, ...]: ...

    async def get_expired_options(self, req: RollingOptionRequest) -> tuple[OptionBar, ...]: ...
