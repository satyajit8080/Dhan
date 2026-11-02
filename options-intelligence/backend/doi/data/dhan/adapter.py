"""Dhan implementation of MarketDataProvider. Read-only by construction: it only
holds a DhanTransport (allowlisted POSTs) and schedulers."""

from __future__ import annotations

from datetime import date

from doi.core.models import Bar, OptionBar, OptionChainSnapshot, UnderlyingRef
from doi.data.dhan.normalize import (
    normalize_candles,
    normalize_expiry_list,
    normalize_option_chain,
    normalize_rolling_option,
)
from doi.data.dhan.transport import DhanTransport
from doi.data.historical_ingestion.planner import RollingOptionRequest
from doi.data.scheduler.chain_scheduler import ApiRequest, KeyedScheduler, Priority


def _sid(v: str) -> int | str:
    return int(v) if v.isdigit() else v


class DhanMarketData:
    def __init__(self, transport: DhanTransport, *, live: KeyedScheduler, historical: KeyedScheduler) -> None:
        self._t = transport
        self._live = live
        self._hist = historical

    @staticmethod
    def build_schedulers(
        transport: DhanTransport, *, cooldown_s: float, concurrency: int, hist_cooldown_s: float, hist_concurrency: int
    ):
        fetch = lambda r: transport.post(r.endpoint, dict(r.params))
        return (
            KeyedScheduler(fetch, cooldown_s=cooldown_s, max_concurrency=concurrency),
            KeyedScheduler(fetch, cooldown_s=hist_cooldown_s, max_concurrency=hist_concurrency),
        )

    async def get_expiries(self, underlying: UnderlyingRef) -> tuple[date, ...]:
        req = ApiRequest(
            "/optionchain/expirylist", {"UnderlyingScrip": _sid(underlying.security_id), "UnderlyingSeg": underlying.segment}
        )
        res = await self._live.get(req, priority=Priority.INTERACTIVE, max_age_s=300)
        return normalize_expiry_list(res.value)

    async def get_option_chain(
        self, underlying: UnderlyingRef, expiry: date, *, max_age_s: float | None = None
    ) -> OptionChainSnapshot:
        req = ApiRequest(
            "/optionchain",
            {"UnderlyingScrip": _sid(underlying.security_id), "UnderlyingSeg": underlying.segment, "Expiry": expiry.isoformat()},
        )
        res = await self._live.get(req, priority=Priority.LIVE, max_age_s=max_age_s)
        return normalize_option_chain(
            res.value,
            underlying_key=underlying.key,
            expiry=expiry,
            request_key=res.key,
            requested_at=res.requested_at,
            received_at=res.received_at,
            master_version=underlying.master_version,
        )

    async def get_candles(
        self, security_id: str, exchange_segment: str, instrument: str, interval: str, start: date, end: date
    ) -> tuple[Bar, ...]:
        daily = interval.upper() in ("D", "1D", "DAY")
        params = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "oi": True,
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
        }
        if not daily:
            params["interval"] = interval
        req = ApiRequest("/charts/historical" if daily else "/charts/intraday", params)
        res = await self._hist.get(req, priority=Priority.INTERACTIVE)
        return normalize_candles(res.value)

    async def get_expired_options(self, req: RollingOptionRequest) -> tuple[OptionBar, ...]:
        res = await self._hist.get(ApiRequest("/charts/rollingoption", req.body()), priority=Priority.BACKGROUND)
        return normalize_rolling_option(res.value, req.option_type)
