"""Plan expired-options (POST /charts/rollingoption) downloads.

Rules from the blueprint (and DhanHQ-py issue #153): exactly ONE ATM-relative
strike offset and ONE option type per request, date range split into windows of
at most 30 days. Comma-separated offsets like "ATM,ATM+1" are never sent."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from doi.core.errors import ConfigError
from doi.core.models import UnderlyingRef

OptionSide = Literal["CALL", "PUT"]
DEFAULT_FIELDS = ("open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot")


def offset_label(offset: int) -> str:
    return "ATM" if offset == 0 else f"ATM{offset:+d}"


@dataclass(frozen=True, slots=True)
class RollingOptionRequest:
    underlying_key: str
    exchange_segment: str
    security_id: str
    expiry_flag: Literal["WEEK", "MONTH"]
    expiry_code: int
    offset: int
    option_type: OptionSide
    interval: str
    from_date: date
    to_date: date  # exclusive; windows chain end-to-start
    required_data: tuple[str, ...] = DEFAULT_FIELDS

    def body(self) -> dict:
        return {
            "exchangeSegment": self.exchange_segment,
            "interval": self.interval,
            "securityId": int(self.security_id) if self.security_id.isdigit() else self.security_id,
            "instrument": "OPTIDX",
            "expiryFlag": self.expiry_flag,
            "expiryCode": self.expiry_code,
            "strike": offset_label(self.offset),
            "drvOptionType": self.option_type,
            "requiredData": list(self.required_data),
            "fromDate": self.from_date.isoformat(),
            "toDate": self.to_date.isoformat(),
        }


def date_windows(start: date, end: date, max_days: int = 30) -> list[tuple[date, date]]:
    if end <= start:
        raise ConfigError(f"empty date range {start}..{end}")
    if not 1 <= max_days <= 30:
        raise ConfigError("window must be 1..30 days")
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=max_days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def plan_rolling_requests(
    underlying: UnderlyingRef,
    *,
    expiry_flag: Literal["WEEK", "MONTH"],
    expiry_code: int,
    offsets: Iterable[int],
    start: date,
    end: date,
    max_abs_offset: int,
    interval: str = "1",
    option_types: Iterable[OptionSide] = ("CALL", "PUT"),
    max_window_days: int = 30,
) -> list[RollingOptionRequest]:
    offs = sorted(set(offsets))
    if not offs:
        raise ConfigError("no ATM offsets requested")
    bad = [o for o in offs if abs(o) > max_abs_offset]
    if bad:
        raise ConfigError(f"{underlying.key}: offsets {bad} exceed allowed ±{max_abs_offset}")
    return [
        RollingOptionRequest(
            underlying.key,
            underlying.derivative_segment,
            underlying.security_id,
            expiry_flag,
            expiry_code,
            off,
            side,
            interval,
            a,
            b,
        )
        for off in offs
        for side in option_types
        for a, b in date_windows(start, end, max_window_days)
    ]
