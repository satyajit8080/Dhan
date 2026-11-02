"""Market-data recorder and replay store (Phase 7.0B).

Recording:
  - chain snapshots go to `option_chain_snapshots` (exact canonical payload plus metadata) and
    `option_chain_rows` (queryable copy);
  - bars go to `market_bars`; feed quotes go to `market_ticks`.
  Every write is idempotent (unique keys), so redelivered data is ignored.

Isolation (Phase 7 requirement 7): `SafeRecorder` wraps every call and never
raises. A database outage is counted and logged, while the collector, chain polling and
paper engine keep running.

Replay: `ReplayStore` queries only data known at `as_of`. The bound is in SQL,
and the feature engine filters again, so leakage needs two independent failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from doi.core.canonical import canonical
from doi.core.market import Quote
from doi.core.models import Bar, ChainRow, OptionChainSnapshot, OptionLeg

log = logging.getLogger(__name__)


def snapshot_payload(s: OptionChainSnapshot) -> dict:
    return json.loads(canonical({"rows": s.rows, "underlying_ltp": s.underlying_ltp}))


def snapshot_sha(s: OptionChainSnapshot) -> str:
    return hashlib.sha256(canonical(snapshot_payload(s)).encode()).hexdigest()


def _leg_from(d: dict | None) -> OptionLeg | None:
    return None if d is None else OptionLeg(**d)


def snapshot_from_payload(meta: dict, payload: dict) -> OptionChainSnapshot:
    rows = tuple(ChainRow(r["strike"], _leg_from(r["ce"]), _leg_from(r["pe"])) for r in payload["rows"])
    return OptionChainSnapshot(
        underlying_key=meta["underlying_key"],
        expiry=meta["expiry"],
        request_key=meta["request_key"],
        requested_at=meta["requested_at"],
        received_at=meta["received_at"],
        underlying_ltp=payload["underlying_ltp"],
        rows=rows,
        master_version=meta["master_version"].strip(),
        source_ts=meta["source_ts"],
    )


def _quality(s: OptionChainSnapshot, max_age_s: float, now: datetime) -> tuple[float, bool]:
    legs = [leg for r in s.rows for leg in (r.ce, r.pe) if leg is not None]
    stale = (now - (s.source_ts or s.received_at)).total_seconds() > max_age_s
    if stale or not legs:
        return 0.0, True
    return round(100.0 * sum(1 for leg in legs if leg.two_sided) / len(legs), 2), False


class MarketRecorder:
    def __init__(self, dsn: str, max_snapshot_age_s: float = 15.0) -> None:
        self.dsn = dsn
        self.max_age = max_snapshot_age_s

    def record_snapshot(self, s: OptionChainSnapshot, *, recorded_at: datetime | None = None) -> bool:
        sha = snapshot_sha(s)
        score, stale = _quality(s, self.max_age, recorded_at or s.received_at)
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute(
                "INSERT INTO instrument_master_versions (version, source_url, retrieved_at, effective_from, row_count, status)"
                " VALUES (%s, 'recorded', %s, %s, 0, 'active') ON CONFLICT (version) DO NOTHING",
                (s.master_version, s.received_at, s.received_at.date()),
            )
            row = c.execute(
                "INSERT INTO option_chain_snapshots (underlying_key, expiry, request_key, master_version, requested_at, received_at,"
                " source_ts, underlying_ltp, data_quality_score, coverage_status, is_stale, content_sha256, payload, source)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'dhan_rest')"
                " ON CONFLICT (underlying_key, expiry, received_at, content_sha256) DO NOTHING RETURNING id",
                (
                    s.underlying_key, s.expiry, s.request_key, s.master_version, s.requested_at, s.received_at, s.source_ts,
                    s.underlying_ltp, score, "STALE" if stale else "LIVE_OK", stale, sha, Jsonb(snapshot_payload(s)),
                ),
            ).fetchone()  # fmt: skip
            if row is None:
                return False
            sid = row[0]
            vals = []
            for r in s.rows:
                for ot, leg in (("CE", r.ce), ("PE", r.pe)):
                    if leg is None:
                        continue
                    vals.append(
                        (
                            sid, r.strike, ot, leg.security_id or "", leg.ltp, leg.oi, leg.change_oi, leg.volume, leg.iv,
                            leg.delta, leg.gamma, leg.theta, leg.vega, leg.bid, leg.bid_qty, leg.ask, leg.ask_qty,
                        )
                    )  # fmt: skip
            c.cursor().executemany(
                "INSERT INTO option_chain_rows (snapshot_id, strike, option_type, security_id, ltp, oi, change_oi, volume, iv,"
                " delta, gamma, theta, vega, bid, bid_qty, ask, ask_qty) VALUES"
                " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                vals,
            )
        return True

    def record_bars(self, security_id: str, timeframe_min: int, bars: Sequence[Bar], source_version: str) -> int:
        if not bars:
            return 0
        tf = f"{timeframe_min}m"
        with psycopg.connect(self.dsn) as c, c.transaction():
            cur = c.cursor()
            n = 0
            for b in bars:
                cur.execute(
                    "INSERT INTO market_bars (security_id, timeframe, bar_ts, open, high, low, close, volume, oi, source_version)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (security_id, tf, b.ts, b.open, b.high, b.low, b.close,
                     None if b.volume is None else round(b.volume), None if b.oi is None else round(b.oi), source_version),
                )  # fmt: skip
                n += cur.rowcount
        return n

    def record_quotes(self, quotes: Iterable[Quote], received_at: datetime | None = None) -> int:
        rows = [
            (q.ts, received_at or q.ts, q.security_id, q.ltp, q.bid, q.ask, q.bid_qty, q.ask_qty, q.volume, q.oi, q.seq)
            for q in quotes
        ]
        if not rows:
            return 0
        with psycopg.connect(self.dsn) as c, c.transaction():
            cur = c.cursor()
            n = 0
            for r in rows:
                cur.execute(
                    "INSERT INTO market_ticks (event_ts, receive_ts, security_id, ltp, bid, ask, bid_qty, ask_qty, volume, oi, source_seq)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    r,
                )
                n += cur.rowcount
        return n


@dataclass
class RecorderHealth:
    snapshots: int = 0
    bars: int = 0
    quotes: int = 0
    failures: int = 0
    last_error: str | None = None
    last_success_at: datetime | None = None


class SafeRecorder:
    """Never raises: recording problems must not stop data collection or paper trading."""

    def __init__(self, inner: MarketRecorder | None) -> None:
        self.inner = inner
        self.health = RecorderHealth()

    def _guard(self, fn, *args, **kw) -> int:
        if self.inner is None:
            return 0
        try:
            n = fn(*args, **kw)
            self.health.last_success_at = datetime.now(UTC)
            return int(n)
        except Exception as e:  # deliberately broad: isolation boundary
            self.health.failures += 1
            self.health.last_error = type(e).__name__
            log.warning("market recorder failure: %s", type(e).__name__)
            return 0

    def snapshot(self, s: OptionChainSnapshot) -> None:
        self.health.snapshots += self._guard(self.inner.record_snapshot, s) if self.inner else 0

    def bars(self, security_id: str, timeframe_min: int, bars: Sequence[Bar], source_version: str) -> None:
        self.health.bars += (
            self._guard(self.inner.record_bars, security_id, timeframe_min, bars, source_version) if self.inner else 0
        )

    def quotes(self, quotes: Iterable[Quote]) -> None:
        self.health.quotes += self._guard(self.inner.record_quotes, list(quotes)) if self.inner else 0


class ReplayStore:
    """Read recorded data strictly as of a point in time."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def snapshots(
        self, underlying_key: str, as_of: datetime, since: datetime | None = None, expiry: date | None = None
    ) -> list[OptionChainSnapshot]:
        q = (
            "SELECT underlying_key, expiry, request_key, master_version, requested_at, received_at, source_ts, payload"
            " FROM option_chain_snapshots WHERE underlying_key = %s AND COALESCE(source_ts, received_at) <= %s AND payload IS NOT NULL"
        )
        args: list = [underlying_key, as_of]
        if since is not None:
            q += " AND COALESCE(source_ts, received_at) >= %s"
            args.append(since)
        if expiry is not None:
            q += " AND expiry = %s"
            args.append(expiry)
        q += " ORDER BY COALESCE(source_ts, received_at), id"
        cols = ("underlying_key", "expiry", "request_key", "master_version", "requested_at", "received_at", "source_ts")
        with psycopg.connect(self.dsn) as c:
            return [snapshot_from_payload(dict(zip(cols, r[:7], strict=True)), r[7]) for r in c.execute(q, args)]

    def bars(self, security_id: str, timeframe_min: int, as_of: datetime, since: datetime | None = None) -> list[Bar]:
        tf = timedelta(minutes=timeframe_min)
        q = "SELECT bar_ts, open, high, low, close, volume, oi FROM market_bars WHERE security_id = %s AND timeframe = %s AND bar_ts + %s <= %s"
        args: list = [security_id, f"{timeframe_min}m", tf, as_of]
        if since is not None:
            q += " AND bar_ts >= %s"
            args.append(since)
        q += " ORDER BY bar_ts"
        with psycopg.connect(self.dsn) as c:
            return [
                Bar(
                    ts,
                    float(o),
                    float(h),
                    float(lo),
                    float(cl),
                    None if v is None else float(v),
                    None if oi is None else float(oi),
                )
                for ts, o, h, lo, cl, v, oi in c.execute(q, args)
            ]

    def quotes(self, security_ids: Sequence[str], start: datetime, end: datetime) -> list[Quote]:
        with psycopg.connect(self.dsn) as c:
            rows = c.execute(
                "SELECT security_id, event_ts, bid, bid_qty, ask, ask_qty, ltp, oi, volume, source_seq FROM market_ticks"
                " WHERE security_id = ANY(%s) AND event_ts >= %s AND event_ts <= %s ORDER BY event_ts, security_id",
                (list(security_ids), start, end),
            ).fetchall()
        f = lambda x: None if x is None else float(x)
        return [Quote(s, ts, f(b), bq, f(a), aq, f(ltp), None, oi, vol, seq) for s, ts, b, bq, a, aq, ltp, oi, vol, seq in rows]
