"""Phase 7.0B: recording, exact replay, leakage bounds, isolation and historical ingestion."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from doi.core.market import IST, Quote, SessionCalendar
from doi.core.models import Bar, OptionBar
from doi.data.historical_ingestion.planner import plan_rolling_requests
from doi.data.historical_ingestion.runner import (
    DatasetNotApproved,
    HistoricalIngestor,
    coverage_report,
    load_approved_dataset,
    replay_features,
)
from doi.data.instruments.master import InstrumentMaster
from doi.data.quality.audit import CoverageStatus, expected_session_timestamps
from doi.data.recorder import MarketRecorder, ReplayStore, SafeRecorder, snapshot_sha
from doi.intelligence.feature_engine.engine import FeatureEngine, FeatureInput
from tests.fixtures import master_csv
from tests.unit.test_feature_engine import AS_OF, OPEN, bars, snapshot


def test_snapshot_round_trip_is_exact_and_idempotent(pg_dsn):
    rec, store = MarketRecorder(pg_dsn), ReplayStore(pg_dsn)
    s = snapshot(AS_OF - timedelta(seconds=2))
    assert rec.record_snapshot(s) is True and rec.record_snapshot(s) is False
    back = store.snapshots("NIFTY", AS_OF)
    assert back == [s] and snapshot_sha(back[0]) == snapshot_sha(s)


def test_replay_never_returns_future_data(pg_dsn):
    rec, store = MarketRecorder(pg_dsn), ReplayStore(pg_dsn)
    rec.record_snapshot(snapshot(AS_OF - timedelta(seconds=5)))
    rec.record_snapshot(snapshot(AS_OF + timedelta(seconds=5), f=25000))
    rec.record_bars("13", 5, bars(72), "test")
    assert [s.received_at for s in store.snapshots("NIFTY", AS_OF)] == [AS_OF - timedelta(seconds=5)]
    got = store.bars("13", 5, AS_OF)
    assert len(got) == 70 and all(b.ts + timedelta(minutes=5) <= AS_OF for b in got)  # bars 71/72 not closed yet


def test_live_and_replayed_features_identical(pg_dsn):
    rec, store, eng = MarketRecorder(pg_dsn), ReplayStore(pg_dsn), FeatureEngine()
    snaps = (snapshot(AS_OF - timedelta(minutes=15)), snapshot(AS_OF - timedelta(seconds=3)))
    live_bars = bars(70)
    for s in snaps:
        rec.record_snapshot(s)
    rec.record_bars("13", 5, live_bars, "test")
    live = eng.calculate(FeatureInput("NIFTY", 5, live_bars, snaps), AS_OF)
    replayed = replay_features(store, eng, underlying_key="NIFTY", index_security_id="13", timeframe_min=5, as_of_points=[AS_OF])[
        0
    ]
    assert replayed.digest == live.digest


def test_quotes_recorded_once_and_replayable(pg_dsn):
    rec, store = MarketRecorder(pg_dsn), ReplayStore(pg_dsn)
    q = Quote("50001", AS_OF, 99.5, 750, 100.0, 750, 99.75, None, 1000, 10)
    assert rec.record_quotes([q, q]) == 1
    back = store.quotes(["50001"], AS_OF - timedelta(seconds=1), AS_OF)
    assert back[0].bid == 99.5 and back[0].ask_qty == 750


def test_safe_recorder_isolates_database_failures():
    bad = SafeRecorder(MarketRecorder("host=/nonexistent port=1 user=x dbname=x connect_timeout=1"))
    bad.snapshot(snapshot(AS_OF))
    bad.bars("13", 5, bars(3), "t")
    bad.quotes([Quote("1", AS_OF, 1, 1, 2, 1)])
    assert bad.health.failures == 3 and bad.health.snapshots == 0 and bad.health.last_error


def test_polling_continues_when_recorder_down():
    from fastapi.testclient import TestClient

    from doi.app.main import create_app
    from doi.paper.driver import pump_once
    from tests.unit.test_paper_integration import ORDER, runtime

    rt = runtime()
    rt.recorder = SafeRecorder(MarketRecorder("host=/nonexistent port=1 user=x dbname=x connect_timeout=1"))
    assert TestClient(create_app(runtime=rt)).post("/api/v1/paper/orders", json=ORDER).status_code == 201
    import time as _t

    _t.sleep(0.3)
    assert asyncio.run(pump_once(rt)) == 1 and rt.paper.orders["ORD-000001"].filled_qty == 150
    assert rt.recorder.health.failures >= 1


# ---------------- historical ingestion ----------------

NIFTY = InstrumentMaster.parse(master_csv()).resolve_underlying("NIFTY", "NSE", "NIFTY")
DAYS = (date(2026, 10, 26), date(2026, 10, 28))  # Mon..Tue (end exclusive)


class FakeHistory:
    def __init__(self, drop=0):
        self.drop, self.calls = drop, []

    async def get_expired_options(self, req):
        self.calls.append(req)
        days = [
            d for d in (req.from_date + timedelta(days=i) for i in range((req.to_date - req.from_date).days)) if d.weekday() < 5
        ]
        ts = expected_session_timestamps(days, int(req.interval))
        ts = ts[self.drop :]
        return tuple(OptionBar(t, 100, 101, 99, 100.5, 10, 1000, 12.0, 24000.0, 24010.0) for t in ts)

    async def get_candles(self, sid, seg, instr, interval, start, end):
        self.calls.append((start, end))
        days = [d for d in (start + timedelta(days=i) for i in range((end - start).days)) if d.weekday() < 5]
        return tuple(Bar(t, 1, 2, 0.5, 1.5) for t in expected_session_timestamps(days, int(interval)))


def test_rolling_ingestion_approves_complete_data_and_loads(pg_dsn):
    reqs = plan_rolling_requests(
        NIFTY, expiry_flag="WEEK", expiry_code=1, offsets=[0], start=DAYS[0], end=DAYS[1], max_abs_offset=3
    )
    ing = HistoricalIngestor(FakeHistory(), MarketRecorder(pg_dsn), pg_dsn, SessionCalendar())
    res = asyncio.run(ing.ingest_rolling(reqs, supported=True))
    assert {r.status for r in res} == {CoverageStatus.APPROVED} and all(r.bars == 750 for r in res)
    meta, loaded = load_approved_dataset(pg_dsn, res[0].dataset_id)
    assert len(loaded) == 750 and loaded == sorted(loaded, key=lambda b: b.ts) and meta["status"] == "APPROVED"


def test_gappy_or_unsupported_data_never_loads(pg_dsn):
    reqs = plan_rolling_requests(
        NIFTY, expiry_flag="WEEK", expiry_code=1, offsets=[1], start=DAYS[0], end=DAYS[1], max_abs_offset=3
    )
    gappy = asyncio.run(
        HistoricalIngestor(FakeHistory(drop=5), MarketRecorder(pg_dsn), pg_dsn, SessionCalendar()).ingest_rolling(
            reqs, supported=True
        )
    )
    assert gappy[0].status is CoverageStatus.QUARANTINED and gappy[0].bars == 0
    with pytest.raises(DatasetNotApproved):
        load_approved_dataset(pg_dsn, gappy[0].dataset_id)
    sensex = replace(NIFTY, key="SENSEX", derivative_segment="BSE_FNO")
    sreqs = plan_rolling_requests(
        sensex, expiry_flag="WEEK", expiry_code=1, offsets=[0], start=DAYS[0], end=DAYS[1], max_abs_offset=3
    )
    uns = asyncio.run(
        HistoricalIngestor(FakeHistory(), MarketRecorder(pg_dsn), pg_dsn, SessionCalendar()).ingest_rolling(
            sreqs[:1], supported=False
        )
    )
    assert uns[0].status is CoverageStatus.UNSUPPORTED
    rows = coverage_report(pg_dsn)
    assert {r["status"] for r in rows} >= {"QUARANTINED", "UNSUPPORTED"} and all(r["source_verified"] is False for r in rows)


def test_index_bar_ingestion_windows(pg_dsn):
    fake = FakeHistory()
    res = asyncio.run(
        HistoricalIngestor(fake, MarketRecorder(pg_dsn), pg_dsn, SessionCalendar()).ingest_index_bars(
            NIFTY, 5, date(2026, 10, 5), date(2026, 10, 24), window_days=7
        )
    )
    assert len(fake.calls) == 3 and res.status is CoverageStatus.APPROVED and res.bars == 15 * 75
    assert datetime.combine(date(2026, 10, 5), datetime.min.time(), IST).astimezone(UTC) <= OPEN
