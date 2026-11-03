"""Feature engine: declared set, look-ahead safety, determinism, provenance,
options/time/regime features and immutable persistence."""

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from doi.analytics import black76
from doi.core.market import IST, SessionCalendar
from doi.core.models import Bar, ChainRow, OptionChainSnapshot, OptionLeg
from doi.intelligence.feature_engine.config import FeatureConfig
from doi.intelligence.feature_engine.engine import ENGINE_VERSION, FeatureEngine, FeatureInput, Status
from doi.intelligence.feature_engine.spec import feature_specs

OPEN = datetime(2026, 11, 2, 3, 45, tzinfo=UTC)  # Monday 09:15 IST
EXPIRY = date(2026, 11, 5)  # Thursday


def bars(n, tf=5, start=None, drift=1.0, vol=None, wiggle=0.0):
    start = start or OPEN
    out = []
    for i in range(n):
        c = 24000 + drift * i + wiggle * math.sin(i)
        out.append(Bar(start + timedelta(minutes=tf * i), c - 0.5, c + 2, c - 2, c, vol))
    return tuple(out)


def leg(bid, ask, oi, vol=1000, iv=None):
    return OptionLeg(None, (bid + ask) / 2, oi, oi - 100, vol, iv, None, None, None, None, bid, 500, ask, 500)


def snapshot(ts, f=24037.0, sigma=0.14, ltp=23990.0, call_wall=24200, put_wall=23800, expiry=EXPIRY, vendor_iv=15.0, vol_mult=1):
    t = (datetime.combine(expiry, datetime.min.time().replace(hour=15, minute=30), IST) - ts).total_seconds() / (365 * 86400)
    df = math.exp(-0.065 * t)
    rows = []
    for i in range(-12, 13):
        k = 24000 + 50 * i
        c, p = black76.price(f, k, t, sigma, df, True), black76.price(f, k, t, sigma, df, False)
        ce_oi = 50000 if k == call_wall else 10000
        pe_oi = 60000 if k == put_wall else 9000
        rows.append(
            ChainRow(
                float(k),
                leg(c - 0.05, c + 0.05, ce_oi, 1000 * vol_mult, vendor_iv),
                leg(max(p - 0.05, 0.05), p + 0.05, pe_oi, 1200, vendor_iv),
            )
        )
    return OptionChainSnapshot("NIFTY", expiry, "k", ts, ts, ltp, tuple(rows), "mv")


def inp(**kw):
    base = dict(underlying_key="NIFTY", timeframe_min=5, bars=bars(70))
    return FeatureInput(**{**base, **kw})


AS_OF = OPEN + timedelta(minutes=5 * 70)  # 15:05 IST, after the 70th bar closes


def test_vector_has_exactly_declared_features_with_provenance():
    eng = FeatureEngine()
    v = eng.calculate(inp(snapshots=(snapshot(AS_OF - timedelta(seconds=2)),)), AS_OF)
    assert list(v.values) == [s.name for s in feature_specs(eng.cfg)]
    for name, fv in v.values.items():
        assert fv.provenance.calc and fv.provenance.sources, name
        if fv.status is not Status.OK:
            assert fv.reason, name
    assert v.engine_version == ENGINE_VERSION and v.version_key.startswith("fe-1:fc-1:")


def test_no_look_ahead_future_data_is_ignored():
    eng = FeatureEngine()
    past = inp(snapshots=(snapshot(AS_OF - timedelta(seconds=2)),))
    future = replace(
        past,
        bars=past.bars + bars(5, start=AS_OF, drift=50),  # bars not yet closed / in the future
        snapshots=past.snapshots + (snapshot(AS_OF + timedelta(seconds=1), f=25000),),
    )
    assert eng.calculate(past, AS_OF).digest == eng.calculate(future, AS_OF).digest
    forming = replace(past, bars=past.bars + (Bar(AS_OF - timedelta(minutes=2), 1, 99999, 0, 99999),))
    assert eng.calculate(forming, AS_OF).value("ema_9") == eng.calculate(past, AS_OF).value("ema_9")


def test_no_look_ahead_via_future_snapshot_metadata():
    """A future chain with a different strike step must not change volume features (POC bin size)."""
    eng = FeatureEngine()
    base = inp(bars=bars(70, vol=1000), snapshots=(snapshot(AS_OF - timedelta(seconds=2)),))
    fut = snapshot(AS_OF + timedelta(minutes=1))
    fut = replace(fut, rows=tuple(replace(r, strike=r.strike * 2) for r in fut.rows))
    assert eng.calculate(base, AS_OF).digest == eng.calculate(replace(base, snapshots=base.snapshots + (fut,)), AS_OF).digest


def test_deterministic_and_versioned():
    i = inp(snapshots=(snapshot(AS_OF - timedelta(seconds=2)),))
    a, b = FeatureEngine().calculate(i, AS_OF), FeatureEngine().calculate(i, AS_OF)
    assert a.digest == b.digest and a.inputs_digest == b.inputs_digest
    c = FeatureEngine(FeatureConfig(rsi_period=10)).calculate(i, AS_OF)
    assert c.config_hash != a.config_hash and c.digest != a.digest and c.version_key != a.version_key
    shuffled = replace(i, bars=tuple(reversed(i.bars)) + i.bars[:3])  # order and duplicates don't matter
    assert FeatureEngine().calculate(shuffled, AS_OF).digest == a.digest


def test_warmup_and_no_volume_statuses():
    v = FeatureEngine().calculate(inp(bars=bars(10)), OPEN + timedelta(minutes=50))
    assert v.values["ema_200"].status is Status.WARMUP and v.values["adx"].status is Status.WARMUP
    assert v.values["vwap"].status is Status.NULL and "no volume" in v.values["vwap"].reason
    assert v.values["forward"].status is Status.NO_DATA and v.coverage_status == "NO_CHAIN" and v.data_quality_score is None
    vv = FeatureEngine().calculate(inp(bars=bars(70, vol=1000)), AS_OF)
    assert vv.values["vwap"].status is Status.OK and vv.value("relative_volume") == pytest.approx(1.0)
    empty = FeatureEngine().calculate(inp(bars=()), AS_OF)
    assert empty.values["ema_9"].status is Status.NO_DATA


def test_trend_momentum_and_regime():
    v = FeatureEngine().calculate(inp(), AS_OF)
    assert v.value("trend_regime") == "TREND_UP" and v.value("rsi") > 70 and v.value("plus_di") > v.value("minus_di")
    assert v.value("ema_9") > v.value("ema_20") > v.value("ema_50") and v.values["ema_200"].status is Status.WARMUP
    assert v.value("vol_regime") in ("HIGH", "NORMAL", "LOW") and 0 <= v.value("atr_pct_percentile") <= 100
    down = FeatureEngine().calculate(inp(bars=bars(70, drift=-1.0)), AS_OF)
    assert down.value("trend_regime") == "TREND_DOWN"
    flat = FeatureEngine().calculate(inp(bars=bars(70, drift=0.0, wiggle=3)), AS_OF)
    assert flat.value("trend_regime") == "RANGE"


def test_opening_range():
    v = FeatureEngine().calculate(inp(), AS_OF)
    first3 = bars(70)[:3]
    assert v.value("opening_range_high") == max(b.high for b in first3)
    assert v.value("opening_range_state") == "ABOVE"
    early = FeatureEngine().calculate(inp(bars=bars(2)), OPEN + timedelta(minutes=10))
    assert early.values["opening_range_high"].status is Status.WARMUP


def test_options_features_from_chain():
    s0 = snapshot(AS_OF - timedelta(minutes=15), call_wall=24100, put_wall=23800, vol_mult=1)
    s1 = snapshot(AS_OF - timedelta(seconds=3), call_wall=24200, put_wall=23800, vol_mult=2)
    v = FeatureEngine().calculate(inp(snapshots=(s0, s1)), AS_OF)
    assert v.value("forward") == pytest.approx(24037.0, abs=0.05) and v.value("forward_basis") == pytest.approx(47.0, abs=0.05)
    assert v.value("atm_strike") == 24050.0 and v.value("strike_step") == 50.0
    assert v.value("atm_iv_b76") == pytest.approx(14.0, abs=0.05)
    assert abs(v.value("iv_skew")) < 0.1  # flat-vol chain
    assert v.value("atm_iv_dhan") == 15.0 and v.value("atm_iv_diff") == pytest.approx(1.0, abs=0.05)
    assert v.value("call_wall") == 24200.0 and v.value("put_wall") == 23800.0
    assert v.value("call_wall_migration_steps") == 2 and v.value("put_wall_migration_steps") == 0
    assert v.value("pcr_oi_chg_15m") == pytest.approx(0.0, abs=1e-9) and v.value("pcr_vol_chg_15m") < 0
    assert v.values["pcr_oi_chg_5m"].status is Status.WARMUP  # no snapshot near t-5m
    assert 0.45 < v.value("atm_delta") < 0.5 and v.value("atm_gamma") > 0 and v.value("atm_theta") < 0
    assert v.value("max_pain") is not None and v.value("snapshot_age_s") == 3.0
    assert v.value("atm_two_sided_share") == 1.0 and v.coverage_status == "LIVE_OK" and v.data_quality_score == 100.0
    assert v.value("gamma_exposure_proxy") is not None
    assert any(src.startswith("chain:NIFTY:2026-11-05") for src in v.values["call_wall_migration_steps"].provenance.sources)
    assert len(v.values["call_wall_migration_steps"].provenance.sources) == 2  # current + earlier snapshot


def test_stale_snapshot_flagged_not_hidden():
    v = FeatureEngine().calculate(inp(snapshots=(snapshot(AS_OF - timedelta(seconds=60)),)), AS_OF)
    assert v.values["pcr_oi"].status is Status.STALE and v.value("pcr_oi") is not None
    assert v.coverage_status == "STALE" and v.data_quality_score == 0.0


def test_one_sided_quotes_never_valued_from_ltp():
    s = snapshot(AS_OF - timedelta(seconds=2))
    rows = tuple(ChainRow(r.strike, replace(r.ce, bid=None), replace(r.pe, ask=None)) for r in s.rows)
    v = FeatureEngine().calculate(inp(snapshots=(replace(s, rows=rows),)), AS_OF)
    assert v.values["forward"].status is Status.NULL and v.values["atm_iv_b76"].status is Status.NULL
    assert v.data_quality_score == 0.0


def test_gamma_proxy_unit_handling():
    s = (snapshot(AS_OF - timedelta(seconds=2)),)
    units = FeatureEngine().calculate(inp(snapshots=s), AS_OF).value("gamma_exposure_proxy")
    cfg = FeatureConfig(oi_unit="contracts")
    no_lot = FeatureEngine(cfg).calculate(inp(snapshots=s), AS_OF)
    assert no_lot.values["gamma_exposure_proxy"].status is Status.NULL
    lot = FeatureEngine(cfg).calculate(inp(snapshots=s, lot_size=75), AS_OF).value("gamma_exposure_proxy")
    assert lot == pytest.approx(units * 75)


def test_time_context_expiry_and_holidays():
    s = (snapshot(AS_OF - timedelta(seconds=2)),)
    v = FeatureEngine().calculate(inp(snapshots=s), AS_OF)
    assert v.value("minutes_since_open") == pytest.approx(350.0) and v.value("minutes_to_close") == pytest.approx(25.0)
    assert v.value("is_last_window") and not v.value("is_first_window") and v.value("session_open")
    assert v.value("dte_calendar") == 3 and v.value("dte_trading") == 3 and not v.value("is_day_before_expiry")
    wed = AS_OF + timedelta(days=2)
    vw = FeatureEngine().calculate(inp(snapshots=s), wed)
    assert vw.value("is_day_before_expiry") and vw.value("dte_trading") == 1
    hol = SessionCalendar(holidays=frozenset({date(2026, 11, 4)}))  # Wednesday holiday
    vt = FeatureEngine().calculate(inp(snapshots=s, calendar=hol), AS_OF + timedelta(days=1))  # Tuesday
    assert vt.value("is_day_before_expiry") and vt.value("dte_trading") == 1
    closed = FeatureEngine().calculate(inp(snapshots=s), AS_OF + timedelta(hours=2))
    assert closed.values["minutes_since_open"].status is Status.NULL and not closed.value("session_open")
    ex = FeatureEngine().calculate(inp(snapshots=s), datetime(2026, 11, 5, 5, 0, tzinfo=UTC))
    assert ex.value("is_expiry_day")


def test_vix_features():
    vix = tuple(Bar(OPEN - timedelta(days=3) + timedelta(minutes=5 * i), 15, 15, 15, 15.0) for i in range(3))
    vix += tuple(Bar(OPEN + timedelta(minutes=5 * i), 16, 16, 16, 16.5) for i in range(5))
    v = FeatureEngine().calculate(inp(vix_bars=vix), AS_OF)
    assert v.value("vix") == 16.5 and v.value("vix_change_pct") == pytest.approx(10.0)


def test_config_validation():
    with pytest.raises(ValueError):
        FeatureConfig(ema_periods=(9, 21))
    with pytest.raises(ValueError):
        FeatureConfig(macd=(26, 12, 9))
    with pytest.raises(ValueError):
        FeatureConfig(unknown=1)
    with pytest.raises(ValueError):
        FeatureEngine().calculate(inp(), AS_OF.replace(tzinfo=None))


def test_feature_store_immutable(pg_dsn):
    from doi.intelligence.feature_engine.store import FeatureConflict, FeatureStore

    store = FeatureStore(pg_dsn)
    v = FeatureEngine().calculate(inp(snapshots=(snapshot(AS_OF - timedelta(seconds=2)),)), AS_OF)
    assert store.save(v) is True and store.save(v) is False
    doc = store.load("NIFTY", 5, AS_OF, v.version_key)
    assert doc["digest"] == v.digest and doc["values"]["forward"]["status"] == "OK"
    other = FeatureEngine().calculate(inp(bars=bars(70, drift=2.0), snapshots=(snapshot(AS_OF - timedelta(seconds=2)),)), AS_OF)
    with pytest.raises(FeatureConflict):
        store.save(other)


def test_quality_scored_near_the_money_not_on_unquoted_far_strikes():
    s = snapshot(AS_OF - timedelta(seconds=2))
    far = tuple(
        ChainRow(25000.0 + 50 * i, replace(s.rows[0].ce, bid=None, ask=None), replace(s.rows[0].pe, bid=None, ask=None))
        for i in range(1, 60)
    )
    v = FeatureEngine().calculate(inp(snapshots=(replace(s, rows=s.rows + far),)), AS_OF)
    assert v.coverage_status == "LIVE_OK" and v.data_quality_score == 100.0  # 59 dead far strikes do not fail the gate
