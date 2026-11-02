from dataclasses import replace
from datetime import UTC, date, timedelta

import pytest

from doi.core.config import DEFAULT_STRATEGY_DIR
from doi.core.errors import ConfigError
from doi.core.models import OptionBar
from doi.data.historical_ingestion.planner import date_windows, offset_label, plan_rolling_requests
from doi.data.instruments.master import InstrumentMaster
from doi.data.quality.audit import AuditPolicy, CoverageStatus, audit_series, expected_session_timestamps
from doi.intelligence.strategy_configs.loader import UNIVERSE, load_index_config, load_index_configs
from tests.fixtures import master_csv

NIFTY = InstrumentMaster.parse(master_csv()).resolve_underlying("NIFTY", "NSE", "NIFTY")


def test_offset_labels():
    assert [offset_label(o) for o in (0, 1, -1, 10)] == ["ATM", "ATM+1", "ATM-1", "ATM+10"]


def test_windows_cap_30_days_contiguous():
    w = date_windows(date(2026, 1, 1), date(2026, 3, 15))
    assert all((b - a).days <= 30 for a, b in w)
    assert w[0][0] == date(2026, 1, 1) and w[-1][1] == date(2026, 3, 15)
    assert all(w[i][1] == w[i + 1][0] for i in range(len(w) - 1))
    with pytest.raises(ConfigError):
        date_windows(date(2026, 1, 1), date(2026, 2, 1), max_days=31)


def test_one_offset_and_one_side_per_request():
    reqs = plan_rolling_requests(
        NIFTY,
        expiry_flag="WEEK",
        expiry_code=1,
        offsets=[0, 1, -1],
        start=date(2026, 1, 1),
        end=date(2026, 2, 15),
        max_abs_offset=3,
    )
    assert len(reqs) == 3 * 2 * 2
    for r in reqs:
        b = r.body()
        assert "," not in b["strike"] and b["drvOptionType"] in ("CALL", "PUT")
        assert b["securityId"] == 9013 and b["exchangeSegment"] == "NSE_FNO" and b["instrument"] == "OPTIDX"
        assert (r.to_date - r.from_date).days <= 30


def test_offset_limit_enforced():
    with pytest.raises(ConfigError, match="exceed"):
        plan_rolling_requests(
            NIFTY, expiry_flag="WEEK", expiry_code=1, offsets=[4], start=date(2026, 1, 1), end=date(2026, 1, 5), max_abs_offset=3
        )


DAYS = [date(2026, 10, 26), date(2026, 10, 27)]


def bars_for(ts_list):
    return [OptionBar(t, 100, 101, 99, 100.5, 10, 1000, 12.0, 24000, 24010) for t in ts_list]


def test_expected_timestamps_session_grid():
    ts = expected_session_timestamps(DAYS[:1])
    assert len(ts) == 375 and ts[0].tzinfo == UTC and (ts[0].hour, ts[0].minute) == (3, 45)  # 09:15 IST
    assert len(expected_session_timestamps(DAYS[:1], 5)) == 75


def test_complete_series_approved():
    exp = expected_session_timestamps(DAYS)
    assert audit_series(bars_for(exp), exp).status is CoverageStatus.APPROVED


def test_gaps_quarantined_unless_policy_allows():
    exp = expected_session_timestamps(DAYS)
    bars = bars_for(exp[:-3])
    assert audit_series(bars, exp).status is CoverageStatus.QUARANTINED
    r = audit_series(bars, exp, AuditPolicy(allow_gaps=True, max_missing_pct=1.0))
    assert r.status is CoverageStatus.APPROVED_WITH_GAPS and r.missing_count == 3


def test_duplicates_identical_reconciled_conflicting_quarantined():
    exp = expected_session_timestamps(DAYS[:1])
    bars = bars_for(exp)
    ok = audit_series(bars + [bars[5]], exp)
    assert ok.status is CoverageStatus.APPROVED and ok.duplicate_count == 1
    bad = audit_series(bars + [replace(bars[5], close=100.7)], exp)
    assert bad.status is CoverageStatus.QUARANTINED and bad.conflicting_duplicates == 1


def test_invalid_ohlc_and_missing_spot_quarantined():
    exp = expected_session_timestamps(DAYS[:1])
    bars = bars_for(exp)
    assert audit_series([replace(bars[0], high=99.0)] + bars[1:], exp).invalid_ohlc == 1
    r = audit_series([replace(bars[0], spot=None)] + bars[1:], exp)
    assert r.status is CoverageStatus.QUARANTINED and r.missing_spot == 1


def test_off_grid_bar_quarantined():
    exp = expected_session_timestamps(DAYS[:1])
    r = audit_series(bars_for(exp + [exp[-1] + timedelta(minutes=1)]), exp)
    assert r.status is CoverageStatus.QUARANTINED and r.unexpected_ts == 1


def test_unsupported_status():
    exp = expected_session_timestamps(DAYS[:1])
    assert audit_series(bars_for(exp), exp, supported=False).status is CoverageStatus.UNSUPPORTED


def test_five_configs_load_and_are_independent():
    cfgs = load_index_configs(DEFAULT_STRATEGY_DIR)
    assert set(cfgs) == set(UNIVERSE)
    assert {k for k, c in cfgs.items() if c.exchange_family == "BSE"} == {"SENSEX", "BANKEX"}
    assert cfgs["SENSEX"].historical.status == cfgs["BANKEX"].historical.status == "blocked"
    for c in cfgs.values():
        assert sum(c.weights.values()) == 100 and c.thresholds.bull_min is None and not c.live_enabled


def test_config_rejects_bad_weights_and_unknown_keys(tmp_path):
    src = (DEFAULT_STRATEGY_DIR / "nifty.yaml").read_text()
    (tmp_path / "nifty.yaml").write_text(src.replace("trend: 20", "trend: 25"))
    with pytest.raises(ConfigError, match="total 100"):
        load_index_config(tmp_path / "nifty.yaml")
    (tmp_path / "nifty.yaml").write_text(src + "inherit_from: banknifty\n")
    with pytest.raises(ConfigError):
        load_index_config(tmp_path / "nifty.yaml")
    (tmp_path / "sensex.yaml").write_text(src)
    with pytest.raises(ConfigError, match="file name"):
        load_index_config(tmp_path / "sensex.yaml")
