"""Indicator exactness, rule behaviour, charges, backtest and main.py <-> dhan_cloud parity."""
import datetime as dt
import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import main as M  # noqa: E402

sys.path.insert(0, str(ROOT / "dhan_cloud"))
import indicators as CI  # noqa: E402
import rules as CR  # noqa: E402

IST = M.IST
T0 = dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()


def bars_from(closes, spread=2.0, vol=100.0, step=300):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"ts": T0 + i * step, "open": o, "high": max(o, c) + spread, "low": min(o, c) - spread,
                    "close": c, "volume": vol})
    return out


def rnd_bars(n=600, seed=1):
    rng = np.random.default_rng(seed)
    c = 24000 * np.exp(np.cumsum(rng.normal(0, 0.0012, n)))
    out = []
    day, k = 0, 0
    for i in range(n):
        if k == 75:
            day, k = day + 1, 0
        ts = T0 + day * 86400 + k * 300
        o = c[i - 1] if i else c[i]
        h = max(o, c[i]) + abs(rng.normal(0, 8))
        l = min(o, c[i]) - abs(rng.normal(0, 8))
        out.append({"ts": ts, "open": float(o), "high": float(h), "low": float(l), "close": float(c[i]),
                    "volume": float(rng.integers(1000, 50000))})
        k += 1
    return out


def same(a, b, tol=1e-9):
    a, b = np.asarray(a, float), np.asarray(b, float)
    assert a.shape == b.shape
    assert np.array_equal(np.isnan(a), np.isnan(b)), (np.where(np.isnan(a) != np.isnan(b))[0][:5])
    m = ~np.isnan(a)
    assert np.allclose(a[m], b[m], rtol=0, atol=tol)


def test_vwap_typical_price_and_session_reset():
    b = [{"ts": T0, "open": 1, "high": 12, "low": 6, "close": 9, "volume": 10},
         {"ts": T0 + 300, "open": 1, "high": 15, "low": 9, "close": 12, "volume": 30},
         {"ts": T0 + 86400, "open": 1, "high": 30, "low": 30, "close": 30, "volume": 5}]
    v = M.ind_vwap(b)
    assert v[0] == 9 and abs(v[1] - (9 * 10 + 12 * 30) / 40) < 1e-12 and v[2] == 30


def test_true_range_and_wilder_atr():
    b = [{"ts": 0, "open": 0, "high": 10, "low": 8, "close": 9},
         {"ts": 1, "open": 0, "high": 12, "low": 11, "close": 11.5},
         {"ts": 2, "open": 0, "high": 11, "low": 7, "close": 8}]
    _, h, l, c, _ = M._ohlcv(b)
    assert list(M.ind_true_range(h, l, c)) == [2, 3, 4.5]
    x = np.array([1, 2, 3, 4, 5, 6.0])
    w = M.wilder(x, 3)
    assert np.isnan(w[1]) and w[2] == 2 and w[3] == (2 * 2 + 4) / 3 and abs(w[5] - ((w[4] * 2 + 6) / 3)) < 1e-12


def test_rsi_adx_extremes():
    up = bars_from([100 + i for i in range(60)])
    assert abs(M.ind_rsi(up)[-1] - 100) < 1e-9
    a, dp, dm = M.ind_adx(up)
    assert dp[-1] > 0 and dm[-1] == 0 and abs(a[-1] - 100) < 1e-6
    flat_rsi = M.ind_rsi(bars_from([100.0] * 30))
    assert flat_rsi[-1] == 50


def test_rsi_textbook_value():
    # Wilder RSI on alternating +2/-1 changes: avg gain -> 2/2=... exact closed form check
    closes = [100.0]
    for i in range(40):
        closes.append(closes[-1] + (2 if i % 2 == 0 else -1))
    r = M.ind_rsi(bars_from(closes))
    g = np.array([max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))])
    l_ = np.array([max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))])
    ag, al = g[:14].mean(), l_[:14].mean()
    for i in range(14, len(g)):
        ag, al = (ag * 13 + g[i]) / 14, (al * 13 + l_[i]) / 14
    assert abs(r[-1] - (100 - 100 / (1 + ag / al))) < 1e-9


def test_bollinger_er_z_pivots():
    c = [float(x) for x in range(1, 31)]
    b = bars_from(c)
    bb = M.ind_bollinger(b)
    w = np.array(c[-20:])
    assert abs(bb["mid"][-1] - w.mean()) < 1e-12 and abs(bb["sd"][-1] - w.std()) < 1e-12
    assert abs(bb["bandwidth"][-1] - 4 * w.std() / w.mean()) < 1e-12
    assert M.ind_efficiency_ratio(b)[-1] == 1.0
    zz = bars_from([1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 2])
    assert M.ind_efficiency_ratio(zz)[-1] == 0.0
    zz2 = bars_from([1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 3])
    assert abs(M.ind_efficiency_ratio(zz2)[-1] - 1 / 11) < 1e-12
    assert abs(M.ind_zscore(b)[-1] - (30 - w.mean()) / w.std()) < 1e-12
    p = M.ind_pivots(110, 90, 100)
    assert p == {"P": 100, "R1": 110, "S1": 90, "R2": 120, "S2": 80, "PDH": 110, "PDL": 90, "PDC": 100}


def test_supertrend_flips_on_close_cross():
    c = [100 + i for i in range(40)] + [140 - 3 * i for i in range(40)]
    line, d = M.ind_supertrend(bars_from(c, spread=0.5))
    assert d[38] == 1 and d[-1] == -1
    flips = [i for i in range(1, len(d)) if np.isfinite(d[i - 1]) and d[i] != d[i - 1]]
    assert flips and all(i > 40 for i in flips) and c[flips[0]] < line[flips[0] - 1]


def test_cloud_parity_indicators():
    for seed in (1, 2, 3):
        b = rnd_bars(500, seed)
        a, c = M.compute_indicators(b), CI.compute(b)
        for k in a:
            same(a[k], c[k], 1e-7)


def test_cloud_parity_rules_and_charges():
    p = M.TechParams()
    pc = {k: getattr(p, k) for k in ("adx_trend", "er_trend", "adx_mr", "z_mr", "rsi_lo", "rsi_hi", "squeeze_lookback",
                                     "squeeze_memory", "level_atr", "stop_atr", "risk_rupees", "max_spread_pct", "pcr_low", "pcr_high")}
    n_sig = 0
    for seed in (4, 5, 6, 7):
        b = rnd_bars(700, seed)
        a, c = M.compute_indicators(b), CI.compute(b)
        lv = M.ind_pivots(24300, 23800, 24050)
        for i in range(1, len(b)):
            s1, s2 = M.tech_signal_at(a, i, lv, p), CR.signal_at(c, i, lv, pc)
            assert (s1["signal"], s1["setup"]) == (s2["signal"], s2["setup"]), (seed, i, s1, s2)
            if s1["signal"] in ("LONG", "SHORT"):
                n_sig += 1
                assert abs(s1["stop"] - s2["stop"]) < 1e-6
    assert n_sig > 0
    for kind in ("fut", "opt"):
        for ex in ("NSE", "BSE"):
            assert abs(M.trade_charges(kind, ex, 1e6, 1.01e6)["total"] - CR.charges(kind, ex, 1e6, 1.01e6)) < 1e-9


def test_charges_components():
    ch = M.trade_charges("opt", "NSE", 100000, 110000)
    assert abs(ch["stt"] - 110.0) < 1e-9 and ch["brokerage"] == 40
    assert abs(ch["gst"] - 0.18 * (40 + ch["txn"] + ch["sebi"])) < 1e-9


def test_level_filter_and_mean_reversion():
    p = M.TechParams()
    b = rnd_bars(400, 9)
    ind = M.compute_indicators(b)
    for i in range(60, 400):
        s = M.tech_signal_at(ind, i, {}, p)
        if s["signal"] in ("LONG", "SHORT"):
            L = ind["close"][i] + 0.1 * ind["atr"][i]
            s2 = M.tech_signal_at(ind, i, {"R1": L}, p)
            assert s2["setup"] == "NEAR_LEVEL" or "closing break" in s2.get("reason", "")
            if s["setup"] == "MEAN_REVERSION":
                assert ind["adx"][i] < 20 and abs(ind["z"][i]) > 2 and s["targets"] == [ind["vwap"][i]]
            else:
                assert abs(s["stop_dist"] - 2 * ind["atr"][i]) < 1e-9
            return
    raise AssertionError("no signal generated")


def test_tech_backtest_costs_and_no_lookahead():
    b = rnd_bars(3000, 11)
    daily = [{"ts": T0 + d * 86400 - 86400, "high": 24300, "low": 23700, "close": 24000} for d in range(60)]
    r = M.backtest_tech(b, daily, 65, "NSE")
    assert r["status"] == "OK" and r["trades"] > 0
    tl = r["trades_list"]
    for t in tl:
        assert t["entry_ts"] > T0 and t["exit_ts"] >= t["entry_ts"]
        assert abs(t["net"] - (t["gross"] - t["charges"])) < 1e-9 and t["charges"] > 40
    assert abs(r["net_rupees"] - (r["gross_rupees"] - r["charges_rupees"])) < 1e-6
    b2 = [dict(x) for x in b]
    for x in b2[2000:]:                     # mutate the future: earlier trades must be unchanged
        x["close"] *= 1.05
        x["high"] *= 1.05
        x["low"] *= 1.05
        x["open"] *= 1.05
    r2 = M.backtest_tech(b2, daily, 65, "NSE")
    early = [t for t in tl if t["exit_ts"] < b[1990]["ts"]]
    early2 = [t for t in r2["trades_list"] if t["exit_ts"] < b[1990]["ts"]]
    assert early == early2


def test_tech_snapshot_card_and_strike():
    b = rnd_bars(500, 5)
    raw = M._synthetic_chain(b[-1]["close"], 50, 4 / 365, 0.065, 0.13)
    _, legs = M.normalize_chain(raw, 0)
    M.compute_leg_analytics(legs, b[-1]["close"], 4 / 365, 0.065, 65, sorted({k for k, _ in legs}))
    out = M.tech_snapshot("NIFTY", b, {"high": 24300, "low": 23800, "close": 24050}, legs, 14.2, b[-1]["close"], 50, 65,
                          4 / 365, 0.065, "NSE")
    assert out["indicators"]["adx"] is not None and "pcr_volume" in out["context"]
    sig = {"signal": "LONG", "setup": "TREND_PULLBACK", "reason": "t", "stop_dist": 60.0, "entry_ref": b[-1]["close"],
           "stop": b[-1]["close"] - 60, "targets": [b[-1]["close"] + 60, b[-1]["close"] + 120]}
    card = M.build_trade_card("NIFTY", sig, None, b[-1]["close"], 65, legs, b[-1]["close"], 50, 4 / 365, 0.065, M.TechParams(max_spread_pct=3.0), "NSE")
    o = card["option"]
    assert o and o["side"] == "CE" and o["sl"] < o["entry"] < o["targets"][0] < o["targets"][1]
    atm = min({k for k, _ in legs}, key=lambda k: abs(k - b[-1]["close"]))
    assert o["strike"] in (atm, atm - 50)
    assert o["lots"] == int(5000 // ((o["entry"] - o["sl"]) * 65))
    tight = M.build_trade_card("NIFTY", sig, None, b[-1]["close"], 65, legs, b[-1]["close"], 50, 4 / 365, 0.065,
                               M.TechParams(max_spread_pct=0.1), "NSE")
    assert tight["option"] is None and tight["strike_notes"]


def test_stitch_front_month():
    d1 = dt.datetime(2026, 11, 23, 10, 0, tzinfo=IST).timestamp()
    d2 = dt.datetime(2026, 11, 24, 10, 0, tzinfo=IST).timestamp()
    per = {"1": [{"ts": d1, "close": 1}], "2": [{"ts": d1, "close": 2}, {"ts": d2, "close": 3}]}
    s = M.stitch_front_month(per, {"1": "2026-11-23", "2": "2026-12-29"})
    assert [x["close"] for x in s] == [1, 3]


def test_cloud_files_respect_dhan_cloud_rules():
    for f in (ROOT / "dhan_cloud").glob("*.py"):
        src = f.read_text()
        assert len(src.splitlines()) <= 200, f
        for bad in ("os.getenv", "os.environ", "os.path", "subprocess", "open(", "socket", "sqlite", "openpyxl", ".write("):
            assert bad not in src, (f.name, bad)
    assert "{{DHAN_ACCESS_TOKEN}}" in (ROOT / "dhan_cloud" / "config.py").read_text()
    req = (ROOT / "dhan_cloud" / "requirements.txt").read_text()
    assert all("==" in ln for ln in req.splitlines() if ln.strip())


def load_fixture():
    import csv as _csv
    out = []
    with open(ROOT / "tests" / "fixtures" / "nifty_fut_nov26_5m.csv") as fh:
        for r in _csv.DictReader(fh):
            t = dt.datetime.strptime(r["dt_ist"], "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp()
            out.append({"ts": t, **{k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}})
    return out


def test_real_nifty_futures_session():
    """Real NIFTY NOV-26 futures 5m bars (04-06 Nov 2026; cross-checked against Dhan sid 61471)."""
    b = load_fixture()
    assert len(b) == 225
    x = M.compute_indicators(b)
    day = [i for i, r in enumerate(b) if dt.datetime.fromtimestamp(r["ts"], IST).date() == dt.date(2026, 11, 6)]
    p = [r for r in b[day[0]:day[-1] + 1]]
    tp = np.array([(r["high"] + r["low"] + r["close"]) / 3 for r in p])
    vv = np.array([r["volume"] for r in p])
    assert abs(x["vwap"][-1] - (tp * vv).sum() / vv.sum()) < 1e-9
    assert x["st_dir"][-1] == 1 and x["close"][-1] > x["vwap"][-1]
    assert 0 <= x["adx"][-1] <= 100 and 0 <= x["rsi"][-1] <= 100 and 0 <= x["er"][-1] <= 1
    c = CI.compute(b)
    for k in x:
        same(x[k], c[k], 1e-7)
    prev = {"high": max(r["high"] for r in b if dt.datetime.fromtimestamp(r["ts"], IST).date() == dt.date(2026, 11, 5)),
            "low": min(r["low"] for r in b if dt.datetime.fromtimestamp(r["ts"], IST).date() == dt.date(2026, 11, 5)),
            "close": [r for r in b if dt.datetime.fromtimestamp(r["ts"], IST).date() == dt.date(2026, 11, 5)][-1]["close"]}
    lv = M.ind_pivots(prev["high"], prev["low"], prev["close"])
    sigs = [M.tech_signal_at(x, i, lv, M.TechParams()) for i in day]
    assert all(s["signal"] in ("LONG", "SHORT", "NO_TRADE", "WAIT") for s in sigs)
    daily = [{"ts": dt.datetime(2026, 11, d, tzinfo=IST).timestamp(), **v} for d, v in
             ((5, prev),)]
    r = M.backtest_tech(b, daily, 65, "NSE")
    assert r["status"] in ("OK", "NO TRADES")


def test_cloud_signal_card_on_real_bars():
    import signal_card as SC
    b = load_fixture()
    raw = M._synthetic_chain(b[-1]["close"], 50, 3 / 365, 0.065, 0.13)
    legs = {}
    for ks, v in raw["oc"].items():
        for s in ("ce", "pe"):
            legs[(float(ks), s.upper())] = v[s]
    card, table = SC.build("NIFTY", b, {"high": 24641.6, "low": 24330, "close": 24580.2}, legs,
                           "2026-11-09", 14.0, 65, b[-1]["ts"] + 300)
    assert card["signal"] in ("LONG", "SHORT", "NO_TRADE", "WAIT") and "PCR" in card["context"]
    assert abs(table["adx"] - M.compute_indicators(b)["adx"][-1]) < 1e-7
    assert SC.text(card).startswith("[NIFTY]")


def test_engine_api_refresh_offline(tmp_path):
    import asyncio
    c = M.Config.from_env()
    c.data_dir, c.db_path, c.client_id, c.access_token, c.token_file, c.health_port = tmp_path, tmp_path / "e.db", "", "", "", 0
    eng = M.Engine(c)
    st = eng.states["NIFTY"]
    st.expiry, st.lot, st.step = "2026-11-09", 65.0, 50.0
    b = load_fixture()
    st.fut_bars.seed(b)
    _, st.chain = M.normalize_chain(M._synthetic_chain(b[-1]["close"], 50, 3 / 365, 0.065, 0.13), M.time.time())
    st.daily = [{"ts": dt.datetime(2026, 11, 5, tzinfo=IST).timestamp(), "open": 0, "high": 24641.6, "low": 24330, "close": 24580.2}]
    out = asyncio.run(eng.refresh_tech("NIFTY"))
    assert out["indicators"]["vwap"] and out["levels"]["R1"] and "DHAN_CLIENT_ID" in " ".join(out["errors"])
    assert json_ok(M.jdump(out))


def json_ok(s):
    import json
    json.loads(s)
    return True
