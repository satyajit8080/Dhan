"""Pure analytics: technical series, Black-76, option-chain calculations."""

import importlib.util
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from doi.analytics import black76
from doi.analytics import options as opt
from doi.analytics import technical as ta
from doi.core.models import Bar, ChainRow, OptionLeg

T0 = datetime(2026, 11, 2, 3, 45, tzinfo=UTC)


def bars(closes, rng=1.0, vol=None):
    return [Bar(T0 + timedelta(minutes=5 * i), c, c + rng, c - rng, c, vol) for i, c in enumerate(closes)]


# ---------------- technical ----------------


def test_ema_seed_constant_and_known_value():
    assert ta.ema([5.0] * 10, 3)[2:] == [5.0] * 8
    out = ta.ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None] and out[2] == 2.0 and out[3] == pytest.approx(3.0) and out[4] == pytest.approx(4.0)
    with pytest.raises(ValueError):
        ta.ema([1, 2], 0)


def test_rsi_extremes_and_known_series():
    assert ta.rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert ta.rsi(list(range(30, 1, -1)), 14)[-1] == pytest.approx(0.0)
    assert ta.rsi([5.0] * 20, 14)[-1] == 50.0
    # Wilder's classic 14-period example: gains 3.34, losses 1.40 over 14 changes
    # (often printed as 70.53 from rounded averages; exact value is 70.4641)
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert ta.rsi(closes, 14)[14] == pytest.approx(100 - 100 / (1 + 3.34 / 1.40), abs=1e-9)


def test_macd_alignment():
    line, sig, hist = ta.macd([float(i) for i in range(60)], 12, 26, 9)
    assert line[24] is None and line[25] is not None and sig[33] is not None and sig[32] is None
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])


def test_atr_adx_and_di():
    b = bars([100 + i for i in range(60)])  # steady uptrend, constant 2-pt range
    assert ta.atr(b, 14)[-1] == pytest.approx(2.0, rel=0.01)
    a, p, m = ta.adx(b, 14)
    assert a[26] is None and a[27] is not None and a[-1] > 50 and p[-1] > m[-1]
    flat = bars([100.0] * 60)
    assert ta.adx(flat, 14)[0][-1] == pytest.approx(0.0)


def test_bollinger_realized_vol_roc():
    assert ta.bollinger_width([10.0] * 25, 20)[-1] == 0.0
    rv = ta.realized_vol([100 * math.exp(0.01 * (i % 2)) for i in range(30)], 20, 252)[-1]
    assert rv == pytest.approx(0.01 * math.sqrt(252) * 100 * math.sqrt(20 / 19) / 2 * 2, rel=0.01)
    assert ta.roc([100, 110], 1)[-1] == pytest.approx(10.0)


def test_volume_features_need_volume():
    assert not ta.has_volume(bars([1, 2, 3]))
    vb = bars([100, 101, 102], vol=10)
    assert ta.vwap(vb) == pytest.approx(101.0)
    assert ta.vwap(bars([1, 2])) is None
    assert ta.relative_volume(vb + [Bar(T0, 1, 2, 0, 1, 40)], 3) == pytest.approx(4.0)
    assert ta.volume_poc(vb, 50) == 100.0


def test_swings_are_confirmed_only():
    closes = [10, 12, 15, 12, 10, 13, 17, 13, 11, 14, 19, 14, 12]
    b = bars(closes)
    highs, lows = ta.swing_points(b, 2)
    assert [i for i, _ in highs] == [2, 6, 10] and all(i <= len(b) - 3 for i, _ in highs + lows)
    assert ta.market_structure(b, 2) == "HH_HL"
    assert ta.market_structure(b[:7], 2) is None


def test_candle_and_percentile():
    c = ta.candle_context(Bar(T0, 10, 12, 8, 11.5), 2.0)
    assert c == {"range_expansion": 2.0, "body_ratio": 0.375, "close_location": 0.875}
    assert ta.percentile_rank([1, 2, 3, 4], 3) == 75.0


# ---------------- Black-76 ----------------


def test_black76_parity_iv_roundtrip_and_greeks():
    f, k, t, s, df, r = 24000.0, 24100.0, 7 / 365, 0.14, math.exp(-0.065 * 7 / 365), 0.065
    c, p = black76.price(f, k, t, s, df, True), black76.price(f, k, t, s, df, False)
    assert c - p == pytest.approx(df * (f - k), abs=1e-8)
    assert black76.implied_vol(c, f, k, t, df, True) == pytest.approx(s, abs=1e-6)
    assert black76.implied_vol(p, f, k, t, df, False) == pytest.approx(s, abs=1e-6)
    g = black76.greeks(f, k, t, s, df, True, r)
    h = 0.5
    num_delta = (black76.price(f + h, k, t, s, df, True) - black76.price(f - h, k, t, s, df, True)) / (2 * h)
    num_gamma = (black76.price(f + h, k, t, s, df, True) - 2 * c + black76.price(f - h, k, t, s, df, True)) / h**2
    num_vega = (black76.price(f, k, t, s + 0.0001, df, True) - black76.price(f, k, t, s - 0.0001, df, True)) / 0.0002 * 0.01
    assert g.delta == pytest.approx(num_delta, rel=1e-4) and g.gamma == pytest.approx(num_gamma, rel=1e-3)
    assert g.vega == pytest.approx(num_vega, rel=1e-4) and g.theta < 0
    assert black76.implied_vol(0.0001, f, 20000, t, df, False) is not None
    assert black76.implied_vol(df * f, f, k, t, df, True) is None  # above no-arbitrage bound
    assert black76.implied_vol(1.0, f, 23000, t, df, True) is None  # below intrinsic


def test_black76_matches_independent_legacy_implementation():
    path = Path(__file__).resolve().parents[4] / "engine" / "refresh_table.py"
    spec = importlib.util.spec_from_file_location("legacy_refresh", path)
    legacy = importlib.util.module_from_spec(spec)
    sys.modules["legacy_refresh"] = legacy  # dataclasses resolve their module by name
    spec.loader.exec_module(legacy)
    for k in (23500, 24000, 24600):
        for call in (True, False):
            a = black76.price(24010, k, 0.02, 0.15, 0.9985, call)
            b = legacy.black76(24010, k, 0.02, 0.15, 0.9985, call)
            assert a == pytest.approx(b, rel=1e-12)


# ---------------- options ----------------


def leg(bid, ask, oi, vol=100, iv=None):
    return OptionLeg(None, (bid + ask) / 2, oi, oi, vol, iv, None, None, None, None, bid, 100, ask, 100)


def chain(f=24000.0, t=7 / 365, sigma=0.14, rate=0.065, step=50, n=10):
    df = math.exp(-rate * t)
    rows = []
    for i in range(-n, n + 1):
        k = f + i * step
        c, p = black76.price(f, k, t, sigma, df, True), black76.price(f, k, t, sigma, df, False)
        rows.append(
            ChainRow(
                k, leg(c - 0.25, c + 0.25, 1000 + max(0, i) * 500), leg(max(p - 0.25, 0.05), p + 0.25, 1000 + max(0, -i) * 800)
            )
        )
    return rows, df


def test_forward_recovered_from_parity():
    rows, df = chain(f=24037.0)
    fwd, n = opt.implied_forward(rows, 23950.0, df, 1.5)  # index LTP far below the forward
    assert fwd == pytest.approx(24037.0, abs=0.01) and n > 5
    assert opt.atm_strike(rows, fwd) == pytest.approx(24037.0)


def test_pcr_walls_max_pain():
    rows = [
        ChainRow(100, leg(5, 6, 100, vol=10), leg(1, 2, 300, vol=30)),
        ChainRow(110, leg(2, 3, 500, vol=50), leg(4, 5, 200, vol=20)),
        ChainRow(120, leg(1, 2, 200, vol=20), leg(9, 10, 100, vol=10)),
    ]
    assert opt.pcr(rows) == pytest.approx(600 / 800) and opt.pcr(rows, "volume") == pytest.approx(60 / 80)
    w = opt.wall(rows, "ce")
    assert (w.strike, w.oi, w.share) == (110, 500, 500 / 800)
    # pain(100)=CE 0 + PE (200*10+100*20)=4000 ; pain(110)=CE 100*10 + PE 100*10=2000 ; pain(120)=CE 100*20+500*10 + PE 0=7000
    assert opt.max_pain(rows) == 110
    assert opt.strike_step(rows) == 10
    assert opt.pcr([ChainRow(1, None, leg(1, 2, 5))]) is None


def test_gamma_proxy_sign_and_liquidity():
    rows, df = chain()
    g = opt.gamma_exposure_proxy(rows, 24000.0, 7 / 365, df, 0.065, 1.0)
    assert g is not None and g < 0  # put OI dominates below spot in this chain
    lq = opt.liquidity(rows[9:12])
    assert lq.two_sided_share == 1.0 and lq.min_top_qty == 100 and lq.legs == 6
