"""Historical coverage audit (blueprint section 6). A dataset is usable for
backtests only when its status is APPROVED (or APPROVED_WITH_GAPS by policy)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from doi.core.models import OptionBar

IST = ZoneInfo("Asia/Kolkata")
AUDIT_VERSION = "1"


class CoverageStatus(StrEnum):
    APPROVED = "APPROVED"
    APPROVED_WITH_GAPS = "APPROVED_WITH_GAPS"
    QUARANTINED = "QUARANTINED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    allow_gaps: bool = False
    max_missing_pct: float = 0.0
    require_spot: bool = True
    require_strike: bool = True


@dataclass(frozen=True, slots=True)
class AuditReport:
    status: CoverageStatus
    expected_count: int
    actual_count: int
    missing_count: int
    missing_pct: float
    duplicate_count: int
    conflicting_duplicates: int
    invalid_ohlc: int
    unexpected_ts: int
    missing_spot: int
    missing_strike: int
    audit_version: str = AUDIT_VERSION
    reasons: tuple[str, ...] = field(default_factory=tuple)


def expected_session_timestamps(
    trading_days: Iterable[date],
    interval_minutes: int = 1,
    session_open: time = time(9, 15),
    session_close: time = time(15, 30),
) -> list[datetime]:
    """Bar-start timestamps (UTC) for each trading day. Trading days must come
    from an exchange calendar; weekends/holidays are not guessed here."""
    step = timedelta(minutes=interval_minutes)
    out = []
    for d in trading_days:
        t = datetime.combine(d, session_open, IST)
        close = datetime.combine(d, session_close, IST)
        while t + step <= close:
            out.append(t.astimezone(UTC))
            t += step
    return out


def _ohlc_ok(b: OptionBar) -> bool:
    vals = (b.open, b.high, b.low, b.close)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals):
        return False
    if b.high < max(b.open, b.close) or b.low > min(b.open, b.close) or b.low <= 0:
        return False
    return all(v is None or v >= 0 for v in (b.volume, b.oi))


def audit_series(
    bars: Sequence[OptionBar],
    expected: Iterable[datetime],
    policy: AuditPolicy | None = None,
    *,
    supported: bool = True,
) -> AuditReport:
    policy = policy or AuditPolicy()
    exp = set(expected)
    seen: dict[datetime, OptionBar] = {}
    dups = conflicts = invalid = 0
    for b in bars:
        prev = seen.get(b.ts)
        if prev is not None:
            dups += 1
            if prev != b:
                conflicts += 1
            continue
        seen[b.ts] = b
        if not _ohlc_ok(b):
            invalid += 1
    present = set(seen)
    missing = len(exp - present)
    unexpected = len(present - exp)
    missing_spot = sum(1 for b in seen.values() if b.spot is None or b.spot <= 0)
    missing_strike = sum(1 for b in seen.values() if b.strike is None or b.strike <= 0)
    pct = 100.0 * missing / len(exp) if exp else 100.0

    reasons = []
    if not supported:
        reasons.append("instrument/date coverage not piloted")
    if not exp:
        reasons.append("no expected timestamps")
    if conflicts:
        reasons.append(f"{conflicts} conflicting duplicate bars")
    if invalid:
        reasons.append(f"{invalid} invalid OHLC/volume/OI bars")
    if unexpected:
        reasons.append(f"{unexpected} bars outside expected session grid")
    if policy.require_spot and missing_spot:
        reasons.append(f"{missing_spot} bars missing underlying spot")
    if policy.require_strike and missing_strike:
        reasons.append(f"{missing_strike} bars missing resolved strike")
    hard_fail = bool(reasons)

    if not supported:
        status = CoverageStatus.UNSUPPORTED
    elif hard_fail:
        status = CoverageStatus.QUARANTINED
    elif missing == 0:
        status = CoverageStatus.APPROVED
    elif policy.allow_gaps and pct <= policy.max_missing_pct:
        status = CoverageStatus.APPROVED_WITH_GAPS
        reasons.append(f"{missing} missing bars ({pct:.2f}%) within policy")
    else:
        status = CoverageStatus.QUARANTINED
        reasons.append(f"{missing} missing bars ({pct:.2f}%)")

    return AuditReport(
        status,
        len(exp),
        len(present),
        missing,
        round(pct, 4),
        dups,
        conflicts,
        invalid,
        unexpected,
        missing_spot,
        missing_strike,
        reasons=tuple(reasons),
    )
