"""NIFTY single-file Dhan Cloud engine: self-test, parity with the VPS engine, execution realism,
roll adjustment, incomplete-candle guard, persistent (replayed) paper ledger and the full audit pipeline."""
import datetime as dt
import importlib.util
import io
import math
import random
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import main as M  # noqa: E402
import test_indicators as TI  # noqa: E402

spec = importlib.util.spec_from_file_location("cloud_single", ROOT / "dhan_cloud_single" / "main.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def test_selftest_passes():
    assert S.selftest()


def test_nifty_only_scope():
    assert list(S.INDICES) == ["NIFTY"] and S.INDICES["NIFTY"]["sid"] == 13


def test_indicator_and_signal_parity_with_vps_engine():
    for b in (TI.rnd_bars(700, 4), TI.load_fixture()):
        a, c = M.compute_indicators(b), S.compute(b)
        for k in a:
            TI.same(a[k], c[k], 1e-7)
        lv = M.ind_pivots(24641.6, 24330, 24580.2)
        for i in range(1, len(b)):
            s1, s2 = M.tech_signal_at(a, i, lv, M.TechParams()), S.signal_at(c, i, lv)
            assert (s1["signal"], s1["setup"]) == (s2["signal"], s2["setup"])


def test_backtest_parity_legacy_mode():
    b = TI.rnd_bars(3000, 11)
    dl = [{"ts": TI.T0 + d * 86400 - 86400, "high": 24300, "low": 23700, "close": 24000} for d in range(60)]
    r1 = M.backtest_tech(b, dl, 65, "NSE", levels="index_daily")
    r2 = S.backtest(b, 65, "NSE", S.PARAMS, None, "legacy", "index_daily", dl, 2026)["trades"]
    assert r1["trades"] == len(r2) > 0
    for t1, t2 in zip(r1["trades_list"], r2):
        assert t1["entry_ts"] == t2["entry_ts"] and t1["why"] == t2["why"] and abs(t1["net"] - t2["net"]) < 1e-6


def test_series_levels_parity():
    b = TI.rnd_bars(3000, 12)
    r1 = M.backtest_tech(b, [], 65, "NSE")
    r2 = S.backtest(b, 65, "NSE", S.PARAMS, None, "legacy", "series", None, 2026)["trades"]
    assert r1["trades"] == len(r2)
    assert abs(r1["net_rupees"] - sum(t["net"] for t in r2)) < 1e-6


def _bars(rows, t0):
    return [{"ts": t0 + k * 300, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0} for k, (o, h, l, c) in enumerate(rows)]


def test_gap_through_stop_fills_at_open():
    base = S.synthetic_bars(800, seed=8)
    tr = S.backtest(base, 65, "NSE", S.PARAMS, None, "realistic")["trades"]
    tr = [x for x in tr if x["exit_ts"] > x["entry_ts"] + 300]
    assert tr
    t = tr[0]
    i = next(k for k, b in enumerate(base) if b["ts"] == t["entry_ts"])
    stop = t["entry"] - t["dir"] * t["sd"]
    g = [dict(b) for b in base]
    j = i + 1
    gap_px = stop - t["dir"] * 20
    g[j] = dict(g[j], open=gap_px, high=max(gap_px, g[j]["high"]) if t["dir"] < 0 else gap_px + 1,
                low=min(gap_px, g[j]["low"]) if t["dir"] > 0 else gap_px - 1, close=gap_px)
    tr2 = S.backtest(g, 65, "NSE", S.PARAMS, None, "realistic")["trades"]
    t2 = next(x for x in tr2 if x["entry_ts"] == t["entry_ts"])
    assert t2["why"] == "SL_GAP" and abs(t2["exit"] - (gap_px - t["dir"] * 1.0)) < 1e-6
    tl = S.backtest(g, 65, "NSE", S.PARAMS, None, "legacy")["trades"]
    tl2 = next(x for x in tl if x["entry_ts"] == t["entry_ts"])
    assert tl2["why"] == "SL" and abs(tl2["exit"] - (tl2["entry"] - t["dir"] * tl2["sd"])) < 1e-6   # legacy filled at the stop


def test_both_touched_never_favourable_in_realistic():
    for seed in (1, 2, 3):
        b = S.synthetic_bars(2500, seed=seed, vol=0.003)
        r = S.backtest(b, 65, "NSE", S.PARAMS, None, "realistic")["trades"]
        o = S.backtest(b, 65, "NSE", S.PARAMS, None, "optimistic")["trades"]
        assert sum(t["pts"] for t in o) >= sum(t["pts"] for t in r)


def test_charges_2026_vs_2025():
    c26 = S.charges("fut", "NSE", 25000 * 65, 25000 * 65, 2026)
    c25 = S.charges("fut", "NSE", 25000 * 65, 25000 * 65, 2025)
    assert abs((c26 - c25) - 0.0003 * 25000 * 65) < 1e-6
    assert S.charges("fut", "NSE", 1, 1, None, dt.date(2026, 3, 31)) == S.charges("fut", "NSE", 1, 1, 2025)
    assert abs(S.charges("fut", "NSE", 25000 * 65, 25000 * 65, 2026) - M.trade_charges("fut", "NSE", 25000 * 65, 25000 * 65)["total"]) < 1e-6


def test_complete_candle_guard_drops_gappy_bucket():
    d = dt.datetime(2026, 11, 9, 9, 15, tzinfo=S.IST).timestamp()
    m1 = [{"ts": d + k * 60, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for k in range(10) if k != 9]
    assert len(S.complete_5m(m1, d + 3600)) == 1


def _mock_market(seed=2, days=9):
    rnd = random.Random(seed)
    t0 = dt.datetime(2026, 11, 2, 9, 15, tzinfo=S.IST).timestamp()
    m1, px = [], 25000.0
    for k_day in range(days):
        day = t0 + k_day * 86400
        if S.ist(day).weekday() >= 5:
            continue
        for k in range(375):
            o = px
            px *= math.exp(rnd.gauss(0, 0.0006))
            m1.append({"ts": day + k * 60, "open": o, "high": max(o, px) + 2, "low": min(o, px) - 2, "close": px, "volume": 1000.0})
    return m1


def _patch_live(monkeypatch, m1, now_dt):
    m1 = [b for b in m1 if b["ts"] < now_dt.timestamp() - 30]
    monkeypatch.setattr(S, "nifty_futures", lambda: [{"sid": 1, "expiry": "2026-11-24", "lot": 65.0, "segment": "NSE_FNO",
                                                      "display": "NIFTY NOV FUT"}])
    monkeypatch.setattr(S, "intraday", lambda sid, seg, ins, frm, to, interval=5, oi=False, strict=False:
                        [b for b in m1 if frm <= S.ist(b["ts"]).date() <= to])
    monkeypatch.setattr(S, "KNOWN_FUTURES", [])
    monkeypatch.setattr(S, "option_chain", lambda sid: (None, {}))
    monkeypatch.setattr(S, "vix", lambda: 12.0)
    sent = []
    monkeypatch.setattr(S, "telegram", lambda t: sent.append(t) or True)
    monkeypatch.setattr(S, "now_ist", lambda: now_dt)
    monkeypatch.setattr(S.time, "time", lambda: now_dt.timestamp())
    monkeypatch.setattr(S, "LEDGER_START", "2026-11-03")
    return sent


def test_paper_ledger_survives_restart_without_duplicates(monkeypatch):
    m1 = _mock_market()
    now_dt = dt.datetime(2026, 11, 10, 12, 7, 30, tzinfo=S.IST)
    sent = _patch_live(monkeypatch, m1, now_dt)
    st = {"sent": set(), "last_bar": None, "start_ts": now_dt.timestamp() - 7 * 86400}
    with redirect_stdout(io.StringIO()):
        S.cycle(st)
        S.cycle(st)
    ids = [t["id"] for t in st["ledger"]]
    assert ids and len(ids) == len(set(ids))
    paper_msgs = [m for m in sent if m.startswith("[PAPER")]
    assert len(paper_msgs) == len(set(paper_msgs))
    st2 = {"sent": set(), "last_bar": None, "start_ts": now_dt.timestamp()}
    before = len(sent)
    with redirect_stdout(io.StringIO()):
        S.cycle(st2)
    assert [t["id"] for t in st2["ledger"]] == ids
    assert st2["open"] == st["open"]
    assert not [m for m in sent[before:] if m.startswith("[PAPER")]
    for t in st["ledger"]:
        assert abs(t["net"] - (t["pts"] * 65 - t["charges"])) < 1e-6
    assert "since 2026-11-03" in S.ledger_summary(st["ledger"], "2026-11-10")


def test_card_applies_f3_f4():
    b = S.synthetic_bars(1500, seed=21)
    x = S.compute(S.adjusted(b))
    lvls = S.prior_session_levels(b)
    n_checked = 0
    for i in range(80, len(b)):
        t = S.ist(b[i]["ts"])
        sig = S.signal_at(x, i, lvls.get(t.date(), {}))
        if sig["signal"] not in ("LONG", "SHORT"):
            continue
        card, _ = S.build_card(b[:i + 1], {}, None, None, 12.0, 65, S.LIVE_FILTER)
        if t.time() >= dt.time(13, 0) and S.ENTRY_START <= t.time():
            assert card["signal"] == "NO_TRADE"
        elif sig["setup"] == "SQUEEZE_BREAKOUT" and S.ENTRY_START <= t.time():
            band = x["bb_up"][i] if sig["signal"] == "LONG" else x["bb_lo"][i]
            brk = abs(x["close"][i] - band) / x["close"][i] * 1e4
            assert (card["signal"] == "NO_TRADE") == (brk < 2.0)
        n_checked += 1
    assert n_checked > 0


def test_full_audit_pipeline_runs_on_mock_data(monkeypatch):
    end = dt.datetime(2026, 11, 6, 15, 25, tzinfo=S.IST)
    idx = [b for b in S.synthetic_bars(125 * 75, seed=4, t0=dt.datetime(2026, 5, 8, 9, 15, tzinfo=S.IST).timestamp())
           if b["ts"] <= end.timestamp()]
    for b in idx:
        b["volume"] = 0.0
    split = dt.datetime(2026, 8, 26, 9, 15, tzinfo=S.IST).timestamp()
    c1 = [dict(b, open=b["open"] + 60, high=b["high"] + 60, low=b["low"] + 60, close=b["close"] + 60, volume=5.0) for b in idx if b["ts"] >= split]
    c2 = [dict(b, open=b["open"] + 140, high=b["high"] + 140, low=b["low"] + 140, close=b["close"] + 140, volume=3.0) for b in idx if b["ts"] >= split]
    monkeypatch.setattr(S, "nifty_futures", lambda: [{"sid": 1, "expiry": "2026-09-29", "lot": 65.0, "segment": "NSE_FNO", "display": "SEP"},
                                                     {"sid": 2, "expiry": "2026-12-29", "lot": 65.0, "segment": "NSE_FNO", "display": "DEC"}])
    monkeypatch.setattr(S, "intraday", lambda sid, seg, ins, frm, to, interval=5, oi=False:
                        [b for b in {13: idx, 1: c1, 2: c2}[sid] if frm <= S.ist(b["ts"]).date() <= to])
    days = sorted({S.ist(b["ts"]).date() for b in idx})
    dl = []
    for d in days:
        bs = [b for b in idx if S.ist(b["ts"]).date() == d]
        dl.append({"ts": dt.datetime.combine(d, dt.time(0, 0), S.IST).timestamp(), "open": bs[0]["open"],
                   "high": max(b["high"] for b in bs), "low": min(b["low"] for b in bs), "close": bs[-1]["close"], "volume": 0})
    monkeypatch.setattr(S, "daily", lambda sid, n: dl)
    monkeypatch.setattr(S, "now_ist", lambda: end)
    monkeypatch.setattr(S, "PERMUTATIONS", 30)
    buf = io.StringIO()
    with redirect_stdout(buf):
        S.run_audit(6)
    out = buf.getvalue()
    for key in ("PHASE 1", "PHASE 3a", "PHASE 3b", "PERMUTATION", "FILTER ATTRIBUTION", "slippage 5.0", "REGIME",
                "ML INDEX", "Hurst", "VERDICT"):
        assert key in out, key


def test_dhan_cloud_restrictions():
    src = (ROOT / "dhan_cloud_single" / "main.py").read_text()
    for bad in ("import os", "os.environ", "os.getenv", "os.path", "subprocess", "open(", "socket", "sqlite", ".write(", "eval(", "exec("):
        assert bad not in src, bad
    assert "{{DHAN_ACCESS_TOKEN}}" in src
    assert "place_order" not in src and "/orders" in src     # /orders only appears in the blocked-endpoint self-test


def test_expired_token_raises_auth_error_without_retry_storm(monkeypatch):
    calls = []

    class R:
        status_code = 401
        text = '{"errorType":"Invalid_Authentication","errorCode":"DH-901"}'

    monkeypatch.setattr(S.requests, "post", lambda *a, **k: calls.append(1) or R())
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    for fn in (lambda: S.post("/charts/intraday", {}),
               lambda: S.intraday(13, "NSE_FNO", "FUTIDX", dt.date(2026, 11, 1), dt.date(2026, 11, 9), 1)):
        try:
            fn()
            raise AssertionError("no AuthError")
        except S.AuthError:
            pass
    assert len(calls) == 2  # one request each, no retries on a bad token
    assert "web.dhan.co" in S.TOKEN_HELP


# ------------------------------------------------------------------ audit fixes (10-Nov-2026)
def _day(d, h=9, m=15, n=3, px=25000.0, mon=11):
    t0 = dt.datetime(2026, mon, d, h, m, tzinfo=S.IST).timestamp()
    return [{"ts": t0 + 300 * k, "open": px, "high": px + 1, "low": px - 1, "close": px, "volume": 1.0} for k in range(n)]


def test_regular_session_drops_muhurat_and_levels_use_last_full_day():
    fri, sun, mon = _day(6, px=25000), _day(8, 18, 15, px=25500), _day(9, px=25100)
    clean = S.regular_session(fri + sun + mon)
    assert len(clean) == 6 and all(S.ist(b["ts"]).day != 8 for b in clean)
    lv = S.prior_session_levels(clean)[dt.date(2026, 11, 9)]
    assert lv == S.pivots(25001, 24999, 25000)          # Friday, not the one-hour Sunday session
    late = [{"ts": dt.datetime(2026, 11, 6, 15, 30, tzinfo=S.IST).timestamp(), "open": 1, "high": 1, "low": 1, "close": 1}]
    assert S.regular_session(late) == []


def test_front_month_flag_and_no_silent_substitution():
    assert S.last_tuesday(2026, 10) == dt.date(2026, 10, 27)
    assert S.is_front_month(dt.date(2026, 11, 9), "2026-11-23") and not S.is_front_month(dt.date(2026, 9, 1), "2026-11-23")
    assert not S.is_front_month(dt.date(2026, 11, 20), "2026-12-29") and S.is_front_month(dt.date(2026, 11, 24), "2026-12-29")
    dec = _day(20, px=25100) + _day(23, px=25100) + _day(24, px=25100)
    b, _ = S.stitch_front_month({2: dec}, {2: "2026-12-29"})      # NOV expired and gone from the master
    assert [x["front"] for x in b[::3]] == [False, False, True]


def test_ledger_skips_far_month_days_and_locks_closed_trades():
    bars = S.synthetic_bars(1500, seed=21)
    for x in bars:
        x["front"] = True
    locked = {}
    a, _, _ = S.ledger_build(bars, 65.0, "2026-05-01", None, locked)
    assert a and all(t["id"] in locked for t in a)
    snap = {k: (v["entry"], v["exit"], v["net"]) for k, v in locked.items()}
    far = [dict(x, front=False, close=x["close"] + 100, open=x["open"] + 100, high=x["high"] + 100, low=x["low"] + 100)
           for x in bars]                                          # same days rebuilt from another contract
    with redirect_stdout(io.StringIO()) as out:
        b, _, _ = S.ledger_build(far, 65.0, "2026-05-01", None, locked)
    assert {k: (v["entry"], v["exit"], v["net"]) for k, v in locked.items()} == snap
    assert [t["id"] for t in b] == [t["id"] for t in a] and "locked record is kept" in out.getvalue()
    c, _, _ = S.ledger_build(far, 65.0, "2026-05-01")            # fresh process: far-month days are not counted
    assert c == []


def test_live_cycle_skips_when_listed_contract_fetch_fails(monkeypatch):
    now_dt = dt.datetime(2026, 11, 10, 12, 7, 30, tzinfo=S.IST)
    sent = _patch_live(monkeypatch, _mock_market(), now_dt)

    def boom(*a, **k):
        raise RuntimeError("/charts/intraday: HTTP 429")
    monkeypatch.setattr(S, "intraday", boom)
    st = {"sent": set(), "last_bar": None, "start_ts": now_dt.timestamp()}
    with redirect_stdout(io.StringIO()) as out:
        S.cycle(st)
    assert "cycle skipped" in out.getvalue() and "ledger" not in st and not sent


def test_parity_forward_recovers_the_option_markets_forward():
    F, T, r = 24972.0, 0.4 / 365, S.RISK_FREE
    legs = {}
    for k in range(24700, 25300, 50):
        for side in ("CE", "PE"):
            px = S.black76(F, k, T, r, 0.20, side == "CE")
            legs[(float(k), side)] = {"top_bid_price": px * 0.999, "top_ask_price": px * 1.001, "last_price": px}
    assert abs(S.parity_forward(legs, T, 24990.0) - F) < 0.5
    assert S.parity_forward({}, T, 24990.0) is None
    bars = S.synthetic_bars(400, seed=3)
    now = dt.datetime.fromtimestamp(bars[-1]["ts"] + 300, S.IST)
    exp = now.date().isoformat()
    card, _ = S.build_card(bars, legs, exp, (now.date() + dt.timedelta(days=14)).isoformat(), 15.0, 65.0, None, S.PARAMS, now)
    assert card["forward"]["source"] == "parity" and abs(card["analytics"]["parity_residual"]) < 1.0
