"""Atomic monthly budget for all Grok usage (hard cap ₹3,000 by default).

Every call reserves its worst-case cost first. Reservation is a single
`UPDATE … WHERE frozen = false AND spent + reserved + estimate <= usable` under the
row lock, and the table CHECK constraint enforces the same rule. So agents, retries,
parallel requests and meetings all draw from one pool and can't oversubscribe it.

`usable` = hard limit - safety buffer. The buffer absorbs a provider-reported
cost above the estimate. Such an overrun is recorded (`overrun_inr`) and freezes
the month. Nothing is ever silently exceeded.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import psycopg

from doi.core.errors import DoiError

D = Decimal


class BudgetExhausted(DoiError):
    pass


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    month: date
    hard_limit_inr: Decimal
    usable_inr: Decimal
    spent_inr: Decimal
    reserved_inr: Decimal
    overrun_inr: Decimal
    frozen: bool
    frozen_reason: str | None

    @property
    def utilisation(self) -> float:
        return float((self.spent_inr + self.reserved_inr) / self.usable_inr) if self.usable_inr else 1.0

    @property
    def remaining_inr(self) -> Decimal:
        return max(D(0), self.usable_inr - self.spent_inr - self.reserved_inr)


def month_of(ts: datetime | None = None) -> date:
    t = (ts or datetime.now(UTC)).astimezone(UTC)
    return date(t.year, t.month, 1)


class PgBudget:
    def __init__(self, dsn: str, hard_limit_inr: Decimal = D("3000"), safety_buffer_inr: Decimal = D("150")) -> None:
        if safety_buffer_inr < 0 or safety_buffer_inr >= hard_limit_inr:
            raise ValueError("safety buffer must be within the hard limit")
        self.dsn, self.limit, self.buffer = dsn, hard_limit_inr, safety_buffer_inr

    def _ensure(self, c, m: date) -> None:
        c.execute(
            "INSERT INTO llm_budget_months (month, hard_limit_inr, usable_inr) VALUES (%s, %s, %s) ON CONFLICT (month) DO NOTHING",
            (m, self.limit, self.limit - self.buffer),
        )

    def status(self, now: datetime | None = None) -> BudgetStatus:
        m = month_of(now)
        with psycopg.connect(self.dsn) as c:
            self._ensure(c, m)
            r = c.execute(
                "SELECT month, hard_limit_inr, usable_inr, spent_inr, reserved_inr, overrun_inr, frozen, frozen_reason"
                " FROM llm_budget_months WHERE month = %s",
                (m,),
            ).fetchone()
        return BudgetStatus(*r)

    def reserve(
        self,
        estimate_inr: Decimal,
        *,
        purpose: str,
        agent_id: str | None = None,
        meeting_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if estimate_inr <= 0:
            raise ValueError("estimate must be positive")
        m = month_of(now)
        rid = "RSV-" + secrets.token_hex(8)
        with psycopg.connect(self.dsn) as c, c.transaction():
            self._ensure(c, m)
            row = c.execute(
                "UPDATE llm_budget_months SET reserved_inr = reserved_inr + %s, updated_at = now()"
                " WHERE month = %s AND NOT frozen AND spent_inr + reserved_inr + %s <= usable_inr RETURNING month",
                (estimate_inr, m, estimate_inr),
            ).fetchone()
            if row is None:
                st = c.execute(
                    "SELECT spent_inr, reserved_inr, usable_inr, frozen FROM llm_budget_months WHERE month = %s", (m,)
                ).fetchone()
                raise BudgetExhausted(
                    f"budget refused ₹{estimate_inr}: spent {st[0]}, reserved {st[1]}, usable {st[2]}, frozen {st[3]}"
                )
            c.execute(
                "INSERT INTO llm_budget_reservations (id, month, estimate_inr, status, purpose, agent_id, meeting_id)"
                " VALUES (%s, %s, %s, 'RESERVED', %s, %s, %s)",
                (rid, m, estimate_inr, purpose, agent_id, meeting_id),
            )
        return rid

    def settle(self, rid: str, actual_inr: Decimal) -> None:
        with psycopg.connect(self.dsn) as c, c.transaction():
            r = c.execute(
                "SELECT month, estimate_inr, status FROM llm_budget_reservations WHERE id = %s FOR UPDATE", (rid,)
            ).fetchone()
            if r is None or r[2] != "RESERVED":
                raise DoiError(f"reservation {rid} not open")
            m, est, _ = r
            c.execute("SELECT 1 FROM llm_budget_months WHERE month = %s FOR UPDATE", (m,))
            over = max(D(0), actual_inr - est)
            # The provider has already billed this amount, so record it truthfully. An overrun freezes the month.
            c.execute(
                "UPDATE llm_budget_months SET reserved_inr = reserved_inr - %s, spent_inr = spent_inr + %s,"
                " overrun_inr = overrun_inr + %s,"
                " frozen = frozen OR %s > 0 OR spent_inr + %s + reserved_inr - %s > usable_inr,"
                " frozen_reason = CASE WHEN %s > 0 THEN 'provider cost exceeded reservation' ELSE frozen_reason END,"
                " updated_at = now() WHERE month = %s",
                (est, actual_inr, over, over, actual_inr, est, over, m),
            )
            c.execute(
                "UPDATE llm_budget_reservations SET status = 'SETTLED', settled_inr = %s, settled_at = now() WHERE id = %s",
                (actual_inr, rid),
            )

    def release(self, rid: str) -> None:
        with psycopg.connect(self.dsn) as c, c.transaction():
            r = c.execute(
                "SELECT month, estimate_inr, status FROM llm_budget_reservations WHERE id = %s FOR UPDATE", (rid,)
            ).fetchone()
            if r is None or r[2] != "RESERVED":
                return
            c.execute(
                "UPDATE llm_budget_months SET reserved_inr = reserved_inr - %s, updated_at = now() WHERE month = %s", (r[1], r[0])
            )
            c.execute("UPDATE llm_budget_reservations SET status = 'RELEASED', settled_at = now() WHERE id = %s", (rid,))

    def set_limit(self, hard_limit_inr: Decimal, now: datetime | None = None, *, unfreeze: bool = False) -> None:
        """Admin-only (callers record an approval). Can't drop below what's already committed."""
        m = month_of(now)
        with psycopg.connect(self.dsn) as c, c.transaction():
            self._ensure(c, m)
            c.execute(
                "UPDATE llm_budget_months SET hard_limit_inr = %s, usable_inr = %s,"
                " frozen = CASE WHEN %s THEN false ELSE frozen END, frozen_reason = CASE WHEN %s THEN NULL ELSE frozen_reason END"
                " WHERE month = %s",
                (hard_limit_inr, hard_limit_inr - self.buffer, unfreeze, unfreeze, m),
            )


class MemoryBudget:
    """Same semantics without a database (development and offline tests)."""

    def __init__(self, hard_limit_inr: Decimal = D("3000"), safety_buffer_inr: Decimal = D("150")) -> None:
        self.limit, self.usable = hard_limit_inr, hard_limit_inr - safety_buffer_inr
        self.spent = self.reserved = self.overrun = D(0)
        self.frozen, self.reason = False, None
        self._res: dict[str, Decimal] = {}
        self._lock = threading.Lock()

    def status(self, now: datetime | None = None) -> BudgetStatus:
        with self._lock:
            return BudgetStatus(
                month_of(now), self.limit, self.usable, self.spent, self.reserved, self.overrun, self.frozen, self.reason
            )

    def reserve(self, estimate_inr: Decimal, **_) -> str:
        with self._lock:
            if self.frozen or self.spent + self.reserved + estimate_inr > self.usable:
                raise BudgetExhausted(f"budget refused ₹{estimate_inr}")
            rid = "RSV-" + secrets.token_hex(8)
            self.reserved += estimate_inr
            self._res[rid] = estimate_inr
            return rid

    def settle(self, rid: str, actual_inr: Decimal) -> None:
        with self._lock:
            est = self._res.pop(rid)
            self.reserved -= est
            self.spent += actual_inr
            if actual_inr > est:
                self.overrun += actual_inr - est
                self.frozen, self.reason = True, "provider cost exceeded reservation"

    def release(self, rid: str) -> None:
        with self._lock:
            est = self._res.pop(rid, None)
            if est is not None:
                self.reserved -= est
