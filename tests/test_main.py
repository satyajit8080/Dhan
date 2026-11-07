"""pytest suite for main.py (offline). Run: python -m pytest -q tests/"""
import asyncio
import dataclasses
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as M  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    c = M.Config.from_env()
    c.data_dir = tmp_path
    c.db_path = tmp_path / "t.db"
    c.client_id, c.access_token, c.token_file = "", "", ""
    c.telegram_token = c.telegram_chat_id = ""
    c.health_port = 0
    return c


def test_selftest_all_pass(cfg):
    rep = M.run_selftest(cfg)
    assert rep.failed == 0, [r for r in rep.rows if r[1] == "FAIL"]


def test_black76_put_call_parity_grid():
    for F in (90.0, 100.0, 125.0):
        for K in (80.0, 100.0, 140.0):
            for T in (0.01, 0.3, 2.0):
                c = M.black76_price(F, K, T, 0.07, 0.25, True)
                p = M.black76_price(F, K, T, 0.07, 0.25, False)
                assert abs(M.put_call_parity_residual(c, p, F, K, T, 0.07)) < 1e-10


def test_iv_matches_vendor_scale():
    F, K, T, r = 24000.0, 24000.0, 3 / 365, 0.065
    px = M.black76_price(F, K, T, r, 0.12, True)
    assert abs(M.implied_vol_black76(px, F, K, T, r, True) - 0.12) < 1e-6


def test_jdump_nan_safe():
    s = M.jdump({"a": float("nan"), "b": [1.0, float("inf")], "c": np.float64(2.5)})
    assert json.loads(s) == {"a": None, "b": [1.0, None], "c": 2.5}


def test_read_only_guard():
    with pytest.raises(M.OrderEndpointBlocked):
        M.assert_read_only("/orders")
    with pytest.raises(M.OrderEndpointBlocked):
        M.assert_read_only("/super/orders")


def test_rest_requires_credentials(cfg):
    rest = M.DhanRest(cfg, M.RateLimiter())
    with pytest.raises(M.DhanApiError):
        asyncio.run(rest.expiry_list(13, "IDX_I"))


def test_backtest_costs_deducted(cfg):
    """Deterministic panel: pattern fires before +10bp moves; OOS net must equal gross - cost."""
    cfg = dataclasses.replace(cfg, min_samples_pattern=5, snapshot_interval_s=60, cost_bps_roundtrip=6.0)
    rows = []
    t0 = 1.75e9
    for i in range(2000):
        on = i % 20 == 0
        rows.append({"ts": t0 + i * 60, "index": "NIFTY", "spot": 100.0,
                     "f": {"_patterns": {"BULL_SCORE_HIGH": 1} if on else {}, "atm_ce": 50.0, "atm_gamma": 0.001,
                           "atm_ce_delta": 0.5, "atm_theta": -1.0, "median_spread_pct": 1.0},
                     "fwd": {15: 0.001 if on else 0.0}, "path": {}})
    bt = M.walk_forward_backtest(rows, cfg, 15)
    assert bt["lookahead_violations"] == 0
    assert bt["oos_trades"] > 10
    assert abs(bt["oos_mean_net"] - (0.001 - 0.0006)) < 1e-12


def test_engine_snapshot_pipeline(cfg):
    """Run Engine._snapshot on a synthetic chain: features, scores, regime, DB rows."""
    eng = M.Engine(cfg)
    eng.db.start()
    st = eng.states["NIFTY"]
    st.expiry = (M.now_ist().date() + M.dt.timedelta(days=4)).isoformat()
    st.lot, st.step = 65.0, 50.0
    st.idx = M.Contract(13, "IDX_I", "NIFTY", "INDEX", "", None, None, 1, 0.05, "Nifty 50")
    T = 4 / 365
    for i in range(40):
        F = 24000 * (1 + 0.0002 * i)
        _, st.chain = M.normalize_chain(M._synthetic_chain(F, 50, T, 0.065, 0.13), time.time())
        st.chain_ts = time.time()
        st.chain_last_price = F
        st.bars.update(F, time.time())
        asyncio.run(eng._snapshot("NIFTY", st))
    assert st.last_scores and 0 <= st.last_scores["bullish_breakout"]["score"] <= 100
    assert st.regime["regime"] in M.REGIMES
    eng.db.stop()
    n = eng.db.query("SELECT COUNT(*) n FROM index_snapshots")[0]["n"]
    m = eng.db.query("SELECT COUNT(*) n FROM chain_snapshots")[0]["n"]
    assert n == 40 and m > 40 * 40
    txt = M.render_dashboard_text(eng)
    assert "BULLISH" in txt and "NIFTY" in txt


def test_resample_anchor():
    base = M.dt.datetime(2026, 11, 6, 9, 15, tzinfo=M.IST).timestamp()
    c1 = [{"ts": base + 60 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for i in range(31)]
    r30 = M.resample_candles(c1, 30)
    assert len(r30) == 2 and r30[0]["ts"] == base and r30[1]["ts"] == base + 1800


def test_vanna_charm_and_hurst_vps():
    F, K, T, r, s = 24000.0, 24200.0, 10 / 365, 0.065, 0.14
    for call in (True, False):
        va, ch = M.black76_vanna_charm(F, K, T, r, s, call)
        d = lambda sig, TT: M.black76_greeks(F, K, TT, r, sig, call).delta
        fd_va = (d(s + 1e-4, T) - d(s - 1e-4, T)) / 2e-4 * 0.01
        fd_ch = -(d(s, T + 1e-5) - d(s, T - 1e-5)) / 2e-5 / 365
        assert abs(va - fd_va) < 1e-6 and abs(ch - fd_ch) < 1e-6
    rng = np.random.default_rng(3)
    assert 0.4 < M.hurst_exponent(24000 * np.exp(np.cumsum(rng.normal(0, 0.001, 5000)))) < 0.6
    rank, pct = M.iv_rank_percentile(list(np.linspace(0.10, 0.20, 50)), 0.15)
    assert abs(rank - 50) < 1e-9 and 48 <= pct <= 52
