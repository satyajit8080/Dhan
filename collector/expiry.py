"""Monthly stock-option expiry mapping.

Dhan's rolling data does not carry an expiry date, so it is derived:
  rule date  = last Thursday of the month (last Tuesday from Sep 2025, NSE change)
  actual     = last trading day on or before the rule date, where trading days are
               the days Dhan returned bars for (any symbol) — this absorbs holidays
               without a hard-coded calendar. An optional holidays list is also honoured.
The mapping is then validated in report.py (expiry-day ATM straddle check) and
against the live expiries in the instrument master.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from typing import Iterable, Optional

from . import config

_TUE_FROM = date(*config.TUESDAY_EXPIRY_FROM)


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    ny, nm = _next_month(y, m)
    d = date(ny, nm, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def rule_expiry(y: int, m: int) -> date:
    thu = _last_weekday(y, m, 3)
    return _last_weekday(y, m, 1) if thu >= _TUE_FROM else thu


class ExpiryMapper:
    def __init__(self, trading_days: Iterable[date] = (), holidays: Iterable[date] = (),
                 expiry_code: int = 1):
        self.days = sorted(set(trading_days))
        self.holidays = set(holidays)
        self.code = expiry_code
        self._cache: dict[tuple[int, int], date] = {}
        self.adjusted: dict[str, str] = {}      # rule date -> actual, when shifted

    def month_expiry(self, y: int, m: int) -> date:
        key = (y, m)
        if key in self._cache:
            return self._cache[key]
        rule = d = rule_expiry(y, m)
        while d in self.holidays or d.weekday() >= 5:
            d -= timedelta(days=1)
        if self.days and self.days[0] <= d <= self.days[-1]:
            i = bisect_right(self.days, d)
            if i and self.days[i - 1] != d and (d - self.days[i - 1]).days < 7:
                d = self.days[i - 1]            # rule day had no trading -> holiday
        if d != rule:
            self.adjusted[rule.isoformat()] = d.isoformat()
        self._cache[key] = d
        return d

    def expiry_for(self, day: date) -> date:
        y, m = day.year, day.month
        if day > self.month_expiry(y, m):
            y, m = _next_month(y, m)
        for _ in range(self.code - 1):
            y, m = _next_month(y, m)
        return self.month_expiry(y, m)

    def check_listed(self, listed: Iterable[str]) -> list[str]:
        """Compare the rule against live expiries from the instrument master."""
        problems = []
        for e in listed:
            d = date.fromisoformat(e)
            want = self.month_expiry(d.year, d.month)
            if want != d:
                problems.append(f"{e}: rule gives {want.isoformat()}")
        return problems


def parse_holidays(text: Optional[str]) -> set[date]:
    out = set()
    for tok in (text or "").replace(",", " ").split():
        if not tok.startswith("#"):
            out.add(date.fromisoformat(tok))
    return out
