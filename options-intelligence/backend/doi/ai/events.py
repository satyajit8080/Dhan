"""Event-driven orchestration: detectors, admission control and the queue.

Detectors are pure functions over consecutive FeatureVectors (never ticks). They
emit an event only when a state changes, so a flapping feature can't produce a
storm: regime labels must hold for `min_dwell` vectors, and wall migration needs
at least k steps. Admission control then applies fingerprint de-duplication,
per-(kind, index) cooldowns, daily caps, the kill switch, market hours and data
quality, all before any LLM call can happen.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import psycopg
from psycopg.types.json import Jsonb

from doi.core.canonical import canonical
from doi.core.market import SessionCalendar
from doi.intelligence.feature_engine.engine import FeatureVector


class EventKind(StrEnum):
    SESSION_PRE_OPEN = "SESSION_PRE_OPEN"
    SESSION_POST_CLOSE = "SESSION_POST_CLOSE"
    REGIME_SHIFT = "REGIME_SHIFT"
    OI_WALL_MIGRATION = "OI_WALL_MIGRATION"
    IV_SHOCK = "IV_SHOCK"
    SIGNAL_CANDIDATE = "SIGNAL_CANDIDATE"
    PAPER_TRADE_CLOSED = "PAPER_TRADE_CLOSED"
    BACKTEST_COMPLETED = "BACKTEST_COMPLETED"
    HUMAN_REQUEST = "HUMAN_REQUEST"


TRADE_EVENTS = frozenset(
    {EventKind.REGIME_SHIFT, EventKind.OI_WALL_MIGRATION, EventKind.IV_SHOCK, EventKind.SIGNAL_CANDIDATE, EventKind.HUMAN_REQUEST}
)


@dataclass(frozen=True)
class Event:
    kind: EventKind
    underlying_key: str | None
    at: datetime
    payload: dict
    valid_for: timedelta = timedelta(minutes=5)
    priority: int = 5

    @property
    def fingerprint(self) -> str:
        """Same real-world event gives the same fingerprint (minute resolution)."""
        base = [self.kind, self.underlying_key, self.at.replace(second=0, microsecond=0), self.payload.get("key")]
        return hashlib.sha256(canonical(base).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class DetectorConfig:
    min_dwell: int = 2  # consecutive vectors a new regime must hold
    wall_steps: int = 2
    iv_jump_pts: float = 2.0  # ATM IV change between vectors, in vol points


def detect(history: Sequence[FeatureVector], cfg: DetectorConfig | None = None) -> list[Event]:
    """Events implied by the newest vector, given earlier ones (oldest first)."""
    cfg = cfg or DetectorConfig()
    if len(history) < 2:
        return []
    cur, out = history[-1], []
    u, at = cur.underlying_key, cur.as_of

    def val(v: FeatureVector, n: str):
        fv = v.values.get(n)
        return fv.value if fv is not None and fv.status == "OK" else None

    for name in ("trend_regime", "vol_regime"):
        tail = [val(v, name) for v in history[-(cfg.min_dwell + 1) :]]
        if (
            len(tail) == cfg.min_dwell + 1
            and tail[0] is not None
            and len(set(tail[1:])) == 1
            and tail[1] is not None
            and tail[1] != tail[0]
        ):
            out.append(
                Event(EventKind.REGIME_SHIFT, u, at, {"key": name, "from": tail[0], "to": tail[1], "feature_digest": cur.digest})
            )
    for side in ("call", "put"):
        steps = val(cur, f"{side}_wall_migration_steps")
        if steps is not None and abs(steps) >= cfg.wall_steps:
            out.append(Event(EventKind.OI_WALL_MIGRATION, u, at, {"key": f"{side}:{val(cur, f'{side}_wall')}", "side": side, "steps": steps,
                                                                    "feature_digest": cur.digest}))  # fmt: skip
    a, b = val(history[-2], "atm_iv_b76"), val(cur, "atm_iv_b76")
    if a is not None and b is not None and abs(b - a) >= cfg.iv_jump_pts:
        out.append(Event(EventKind.IV_SHOCK, u, at, {"key": "atm_iv", "from": a, "to": b, "feature_digest": cur.digest}))
    return out


@dataclass(frozen=True)
class AdmissionPolicy:
    cooldown: dict[EventKind, timedelta] = field(
        default_factory=lambda: {
            EventKind.REGIME_SHIFT: timedelta(minutes=45),
            EventKind.OI_WALL_MIGRATION: timedelta(minutes=45),
            EventKind.IV_SHOCK: timedelta(minutes=45),
            EventKind.SIGNAL_CANDIDATE: timedelta(minutes=15),
        }
    )
    daily_trade_meetings: int = 3
    min_quality: float = 80.0


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str | None = None


# MeetingEngine aborts with this reason before any LLM call. The worker then
# finishes the event DONE (not SKIPPED) and stores the reason on skip_reason.
_DATA_QUALITY_ABORT = "DATA_QUALITY"


def counts_toward_daily_trade_cap(status: str, skip_reason: str | None) -> bool:
    """True when this trade event occupies one AdmissionPolicy.daily_trade_meetings slot.

    SKIPPED never ran. A zero-LLM data-quality abort is recorded as DONE with
    skip_reason DATA_QUALITY and must not consume the cap. Queued, running,
    failed, and every other finished meeting still count.
    """
    if status == "SKIPPED":
        return False
    return not (status == "DONE" and skip_reason == _DATA_QUALITY_ABORT)


class EventQueue:
    """Interface: admit (atomic with the de-dupe and cooldown checks), claim, finish."""

    def admit(self, ev: Event, decision: Admission) -> str: ...
    def recent(self, kind: EventKind, underlying_key: str | None, since: datetime) -> int: ...
    def trade_meetings_today(self, day_start: datetime) -> int: ...
    def claim(self, now: datetime) -> dict | None: ...
    def finish(self, event_id: str, status: str, reason: str | None = None) -> None: ...


def admit(ev: Event, q: EventQueue, policy: AdmissionPolicy, *, calendar: SessionCalendar, kill_switch: bool,
          quality: float | None, ai_available: bool) -> Admission:  # fmt: skip
    if kill_switch:
        return Admission(False, "kill switch engaged")
    if not ai_available:
        return Admission(False, "AI unavailable (disabled, budget stop or provider down)")
    trade = ev.kind in TRADE_EVENTS
    if trade and ev.kind is not EventKind.HUMAN_REQUEST and not calendar.is_open(ev.at):
        return Admission(False, "market closed")
    if trade and ev.kind is not EventKind.HUMAN_REQUEST and (quality is None or quality < policy.min_quality):
        return Admission(False, f"data quality {quality} below {policy.min_quality}")
    cd = policy.cooldown.get(ev.kind)
    if cd and q.recent(ev.kind, ev.underlying_key, ev.at - cd):
        return Admission(False, f"cooldown {cd} for {ev.kind}/{ev.underlying_key}")
    if trade:
        day = calendar.at(calendar.session_date(ev.at), datetime.min.time())
        if q.trade_meetings_today(day) >= policy.daily_trade_meetings:
            return Admission(False, f"daily cap {policy.daily_trade_meetings} trade meetings reached")
    return Admission(True)


class MemoryEventQueue(EventQueue):
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def admit(self, ev: Event, decision: Admission) -> str:
        with self._lock:
            for r in self.rows.values():
                if r["fingerprint"] == ev.fingerprint:
                    return r["id"]
            eid = "EVT-" + secrets.token_hex(6)
            self.rows[eid] = {"id": eid, "kind": ev.kind, "underlying_key": ev.underlying_key, "fingerprint": ev.fingerprint,
                              "priority": ev.priority, "payload": ev.payload, "status": "QUEUED" if decision.admitted else "SKIPPED",
                              "skip_reason": decision.reason, "valid_until": ev.at + ev.valid_for, "created_at": ev.at}  # fmt: skip
            return eid

    def recent(self, kind, underlying_key, since) -> int:
        return sum(1 for r in self.rows.values() if r["kind"] == kind and r["underlying_key"] == underlying_key
                   and r["status"] != "SKIPPED" and r["created_at"] >= since)  # fmt: skip

    def trade_meetings_today(self, day_start) -> int:
        return sum(
            1
            for r in self.rows.values()
            if r["kind"] in TRADE_EVENTS
            and r["created_at"] >= day_start
            and counts_toward_daily_trade_cap(r["status"], r["skip_reason"])
        )

    def claim(self, now):
        with self._lock:
            q = sorted((r for r in self.rows.values() if r["status"] == "QUEUED"), key=lambda r: (r["priority"], r["created_at"]))
            for r in q:
                if r["valid_until"] < now:
                    r["status"], r["skip_reason"] = "SKIPPED", "expired before a worker was free"
                    continue
                r["status"] = "RUNNING"
                return dict(r)
        return None

    def finish(self, event_id, status, reason=None) -> None:
        self.rows[event_id]["status"], self.rows[event_id]["skip_reason"] = status, reason


class PgEventQueue(EventQueue):
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def admit(self, ev: Event, decision: Admission) -> str:
        eid = "EVT-" + secrets.token_hex(6)
        with psycopg.connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO ai_events (id, kind, underlying_key, fingerprint, priority, payload, status, skip_reason, valid_until, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (fingerprint) DO NOTHING RETURNING id",
                (eid, ev.kind, ev.underlying_key, ev.fingerprint, ev.priority, Jsonb(ev.payload),
                 "QUEUED" if decision.admitted else "SKIPPED", decision.reason, ev.at + ev.valid_for, ev.at),
            ).fetchone()  # fmt: skip
            if row:
                return row[0]
            return c.execute("SELECT id FROM ai_events WHERE fingerprint = %s", (ev.fingerprint,)).fetchone()[0]

    def recent(self, kind, underlying_key, since) -> int:
        with psycopg.connect(self.dsn) as c:
            return c.execute(
                "SELECT count(*) FROM ai_events WHERE kind = %s AND underlying_key IS NOT DISTINCT FROM %s AND status <> 'SKIPPED' AND created_at >= %s",
                (kind, underlying_key, since),
            ).fetchone()[0]

    def trade_meetings_today(self, day_start) -> int:
        with psycopg.connect(self.dsn) as c:
            # IS NOT DISTINCT FROM so a real DONE meeting (skip_reason NULL) still counts.
            # `<>` would make NOT (status = 'DONE' AND NULL) unknown and drop that row.
            return c.execute(
                "SELECT count(*) FROM ai_events WHERE kind = ANY(%s) AND created_at >= %s"
                " AND status <> 'SKIPPED'"
                " AND NOT (status = 'DONE' AND skip_reason IS NOT DISTINCT FROM %s)",
                ([k.value for k in TRADE_EVENTS], day_start, _DATA_QUALITY_ABORT),
            ).fetchone()[0]

    def claim(self, now):
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute(
                "UPDATE ai_events SET status = 'SKIPPED', skip_reason = 'expired before a worker was free' WHERE status = 'QUEUED' AND valid_until < %s",
                (now,),
            )
            r = c.execute(
                "SELECT id, kind, underlying_key, fingerprint, priority, payload, valid_until, created_at FROM ai_events"
                " WHERE status = 'QUEUED' ORDER BY priority, created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if r is None:
                return None
            c.execute(
                "UPDATE ai_events SET status = 'RUNNING', started_at = %s, attempts = attempts + 1 WHERE id = %s", (now, r[0])
            )
        return dict(
            zip(
                ("id", "kind", "underlying_key", "fingerprint", "priority", "payload", "valid_until", "created_at"),
                r,
                strict=True,
            )
        )

    def finish(self, event_id, status, reason=None) -> None:
        with psycopg.connect(self.dsn) as c:
            c.execute(
                "UPDATE ai_events SET status = %s, skip_reason = %s, finished_at = %s WHERE id = %s",
                (status, reason, datetime.now(UTC), event_id),
            )
