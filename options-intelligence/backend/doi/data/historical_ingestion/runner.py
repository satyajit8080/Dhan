"""Historical ingestion, approved-dataset loading, coverage reporting and
feature replay (Phase 7.0B).

Dhan's depth of history per endpoint is UNVERIFIED from this build environment.
The code never assumes coverage: every dataset is audited against the exchange
session grid, and only APPROVED (or policy-allowed APPROVED_WITH_GAPS) datasets
can be loaded for research.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import psycopg
from psycopg.types.json import Jsonb

from doi.core.canonical import canonical
from doi.core.errors import DoiError
from doi.core.market import IST, SessionCalendar
from doi.core.models import Bar, OptionBar, UnderlyingRef
from doi.data.historical_ingestion.planner import RollingOptionRequest, offset_label
from doi.data.quality.audit import AuditPolicy, AuditReport, CoverageStatus, audit_series, expected_session_timestamps
from doi.data.recorder import MarketRecorder, ReplayStore
from doi.intelligence.feature_engine.engine import FeatureEngine, FeatureInput, FeatureVector


class DatasetNotApproved(DoiError):
    pass


def rolling_series_key(r: RollingOptionRequest) -> str:
    """Synthetic market_bars.security_id for an ATM-relative rolling series."""
    return f"ROLL:{r.underlying_key}:{r.expiry_flag}{r.expiry_code}:{offset_label(r.offset)}:{r.option_type}:{r.interval}m"


@dataclass(frozen=True)
class IngestResult:
    dataset_id: str
    series_key: str
    status: CoverageStatus
    bars: int
    report: AuditReport


class HistoricalIngestor:
    def __init__(
        self, provider, recorder: MarketRecorder, dsn: str, calendar: SessionCalendar, policy: AuditPolicy | None = None
    ) -> None:
        self.provider, self.recorder, self.dsn, self.cal = provider, recorder, dsn, calendar
        self.policy = policy or AuditPolicy()

    def _days(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d < end:
            if self.cal.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    async def ingest_rolling(
        self, requests: Sequence[RollingOptionRequest], *, supported: bool, source_id: str = "dhan_rest"
    ) -> list[IngestResult]:
        """One dataset per (offset, side, expiry selector). Each planned request
        asks for exactly one offset and one side (planner rule)."""
        groups: dict[str, list[RollingOptionRequest]] = {}
        for r in requests:
            groups.setdefault(rolling_series_key(r), []).append(r)
        results = []
        for key, reqs in sorted(groups.items()):
            bars: list[OptionBar] = []
            for r in sorted(reqs, key=lambda r: r.from_date):
                bars.extend(await self.provider.get_expired_options(r))
            start, end = min(r.from_date for r in reqs), max(r.to_date for r in reqs)
            interval = int(reqs[0].interval)
            expected = [t for t in expected_session_timestamps(self._days(start, end), interval)]
            report = audit_series(bars, expected, self.policy, supported=supported)
            n = 0
            if report.status in (CoverageStatus.APPROVED, CoverageStatus.APPROVED_WITH_GAPS):
                n = self.recorder.record_bars(
                    key,
                    interval,
                    list(bars),  # OptionBars: strike/spot/iv are stored for premium backtests
                    f"{source_id}:rolling",
                )
            ds = self._register(source_id, reqs[0].underlying_key, "ROLLING_OPTION", key, f"{interval}m", start, end - timedelta(days=1), report,
                                {"requests": [r.body() for r in reqs], "bars_received": len(bars)})  # fmt: skip
            results.append(IngestResult(ds, key, report.status, n, report))
        return results

    async def ingest_index_bars(
        self,
        underlying: UnderlyingRef,
        interval_min: int,
        start: date,
        end: date,
        *,
        window_days: int,
        supported: bool = True,
        source_id: str = "dhan_rest",
    ) -> IngestResult:
        """Index candles in bounded windows. Dhan docs (dhanhq.co/docs/v2/historical-data, checked
        2026-11-02): intraday look-back 5 years, at most 90 days per request."""
        if not 1 <= window_days <= 90:
            raise ValueError("window_days must be 1..90 (Dhan intraday per-request limit)")
        bars: list[Bar] = []
        d = start
        while d < end:
            nxt = min(d + timedelta(days=window_days), end)
            bars.extend(
                await self.provider.get_candles(underlying.security_id, underlying.segment, "INDEX", str(interval_min), d, nxt)
            )
            d = nxt
        expected = expected_session_timestamps(self._days(start, end), interval_min)
        as_option_bars = [OptionBar(b.ts, b.open, b.high, b.low, b.close, b.volume, b.oi, None, 1.0, 1.0) for b in bars]
        report = audit_series(as_option_bars, expected, self.policy, supported=supported)
        n = 0
        if report.status in (CoverageStatus.APPROVED, CoverageStatus.APPROVED_WITH_GAPS):
            n = self.recorder.record_bars(underlying.security_id, interval_min, bars, f"{source_id}:candles")
        ds = self._register(source_id, underlying.key, "INDEX_BARS", underlying.security_id, f"{interval_min}m", start, end - timedelta(days=1),
                            report, {"window_days": window_days, "bars_received": len(bars)})  # fmt: skip
        return IngestResult(ds, underlying.security_id, report.status, n, report)

    def _register(self, source_id, underlying_key, klass, series_key, gran, d0, d1, report: AuditReport, manifest: dict) -> str:
        ds_id = "DS-" + hashlib.sha256(canonical([source_id, klass, series_key, gran, d0, d1]).encode()).hexdigest()[:16]
        with psycopg.connect(self.dsn) as c, c.transaction():
            audit_id = c.execute(
                "INSERT INTO data_quality_audits (dataset_id, index_key, date_from, date_to, expected_count, actual_count, missing_pct,"
                " gap_count, duplicate_count, missing_strikes, audit_version, status, reasons)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (ds_id, underlying_key, d0, d1, report.expected_count, report.actual_count, report.missing_pct, report.missing_count,
                 report.duplicate_count, report.missing_strike, report.audit_version, report.status.value, Jsonb(list(report.reasons))),
            ).fetchone()[0]  # fmt: skip
            c.execute(
                "INSERT INTO historical_datasets (id, source_id, underlying_key, instrument_class, series_key, granularity, date_from, date_to,"
                " audit_id, status, manifest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET audit_id = EXCLUDED.audit_id, status = EXCLUDED.status, manifest = EXCLUDED.manifest",
                (
                    ds_id,
                    source_id,
                    underlying_key,
                    klass,
                    series_key,
                    gran,
                    d0,
                    d1,
                    audit_id,
                    report.status.value,
                    Jsonb(manifest),
                ),
            )
        return ds_id


def load_approved_dataset(dsn: str, dataset_id: str, *, allow_gaps: bool = False) -> tuple[dict, list[Bar]]:
    """Chronological normalised bars for an approved dataset (blueprint HistoricalProvider)."""
    with psycopg.connect(dsn) as c:
        row = c.execute(
            "SELECT id, underlying_key, instrument_class, series_key, granularity, date_from, date_to, status FROM historical_datasets WHERE id = %s",
            (dataset_id,),
        ).fetchone()
    if row is None:
        raise DatasetNotApproved(f"unknown dataset {dataset_id}")
    meta = dict(
        zip(
            ("id", "underlying_key", "instrument_class", "series_key", "granularity", "date_from", "date_to", "status"),
            row,
            strict=True,
        )
    )
    ok = {"APPROVED"} | ({"APPROVED_WITH_GAPS"} if allow_gaps else set())
    if meta["status"] not in ok:
        raise DatasetNotApproved(f"dataset {dataset_id} is {meta['status']}; backtests need APPROVED coverage")
    tf = int(meta["granularity"].rstrip("m"))
    end = datetime.combine(meta["date_to"] + timedelta(days=1), time(0, 0), IST)
    bars = [b for b in ReplayStore(dsn).bars(meta["series_key"], tf, end) if b.ts.date() >= meta["date_from"] - timedelta(days=1)]
    return meta, bars


def coverage_report(dsn: str) -> list[dict]:
    with psycopg.connect(dsn) as c:
        rows = c.execute(
            "SELECT d.underlying_key, d.instrument_class, d.series_key, d.granularity, d.date_from, d.date_to, d.status,"
            " a.expected_count, a.actual_count, a.missing_pct, s.verified"
            " FROM historical_datasets d JOIN historical_sources s ON s.id = d.source_id"
            " LEFT JOIN data_quality_audits a ON a.id = d.audit_id ORDER BY d.underlying_key, d.instrument_class, d.date_from"
        ).fetchall()
        rec = c.execute(
            "SELECT underlying_key, min(received_at), max(received_at), count(*) FROM option_chain_snapshots GROUP BY underlying_key ORDER BY 1"
        ).fetchall()
    cols = (
        "underlying",
        "class",
        "series",
        "granularity",
        "from",
        "to",
        "status",
        "expected",
        "actual",
        "missing_pct",
        "source_verified",
    )
    out = [dict(zip(cols, r, strict=True)) for r in rows]
    out += [
        {"underlying": u, "class": "RECORDED_CHAIN_SNAPSHOTS", "series": "live capture", "granularity": "snapshot", "from": a, "to": b,
         "status": "RECORDED", "expected": None, "actual": n, "missing_pct": None, "source_verified": True}
        for u, a, b, n in rec
    ]  # fmt: skip
    return out


def replay_features(
    store: ReplayStore,
    engine: FeatureEngine,
    *,
    underlying_key: str,
    index_security_id: str,
    timeframe_min: int,
    as_of_points: Iterable[datetime],
    lookback: timedelta = timedelta(days=10),
    calendar: SessionCalendar | None = None,
) -> list[FeatureVector]:
    """Feature vectors exactly as they could have been computed live at each point."""
    out = []
    for as_of in as_of_points:
        since = as_of - lookback
        inp = FeatureInput(
            underlying_key=underlying_key,
            timeframe_min=timeframe_min,
            bars=tuple(store.bars(index_security_id, timeframe_min, as_of, since)),
            snapshots=tuple(store.snapshots(underlying_key, as_of, since)),
            calendar=calendar or SessionCalendar(),
        )
        out.append(engine.calculate(inp, as_of))
    return out
