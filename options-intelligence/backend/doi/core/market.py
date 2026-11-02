"""Executable quotes and the Indian exchange session calendar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book observation for one contract. `ts` is the observation time
    (source timestamp when Dhan supplies one, otherwise receive time)."""

    security_id: str
    ts: datetime
    bid: float | None
    bid_qty: int | None
    ask: float | None
    ask_qty: int | None
    ltp: float | None = None
    underlying_ltp: float | None = None
    oi: int | None = None
    volume: int | None = None
    seq: int | None = None

    @property
    def event_key(self) -> str:
        return f"{self.security_id}:{self.seq if self.seq is not None else self.ts.isoformat()}"

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask >= self.bid

    @property
    def spread_pct(self) -> float | None:
        if not self.two_sided:
            return None
        mid = (self.bid + self.ask) / 2  # type: ignore[operator]
        return (self.ask - self.bid) / mid * 100  # type: ignore[operator]


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    open: time = time(9, 15)
    close: time = time(15, 30)
    holidays: frozenset[date] = field(default_factory=frozenset)

    @staticmethod
    def session_date(ts: datetime) -> date:
        return ts.astimezone(IST).date()

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_open(self, ts: datetime) -> bool:
        local = ts.astimezone(IST)
        return self.is_trading_day(local.date()) and self.open <= local.time() < self.close

    @staticmethod
    def at(d: date, t: time) -> datetime:
        return datetime.combine(d, t, IST).astimezone(UTC)


def load_holidays(path: Path | None) -> frozenset[date]:
    """Exchange trading holidays from YAML: {holidays: [{date: YYYY-MM-DD, name: ..., source: ...}]}."""
    if path is None or not Path(path).exists():
        return frozenset()
    data = yaml.safe_load(Path(path).read_text()) or {}
    return frozenset(date.fromisoformat(str(h["date"])) for h in data.get("holidays", []))
