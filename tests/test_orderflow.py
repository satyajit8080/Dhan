"""Order-flow layer (Dhan Cloud single file): build-up, all-months OI, ESTIMATED delta/CVD, breadth, book,
causality of orderflow_state and its integration in backtest / paper ledger."""
import datetime as dt
import importlib.util
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_single_of", ROOT / "dhan_cloud_single" / "main.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def fut_bars(days=6, seed=1):
    rnd = random.Random(seed)
    out, px, oi = [], 24000.0, 1.0e7
    d0 = dt.date(2026, 11, 2)
    k = 0
    while len({S.ist(b["ts"]).date() for b in out}) < days:
        d = d0 + dt.timedelta(days=k)
        k += 1
        if d.weekday() >= 5:
            continue
        t = dt.datetime(d.year, d.month, d.day, 9, 15, tzinfo=S.IST).timestamp()
        for j in range(75):
            o = px
            px += rnd.gauss(0, 8)
            oi += rnd.gauss(0, 2e4)
            out.append({"ts": t + j * 300, "open": o, "high": max(o, px) + 3, "low": min(o, px) - 3, "close": px,
                        "volume": 1000 + rnd.random() * 2000, "oi": oi})
    return out


def test_buildup_quadrants_and_sides():
    for dp, doi, lab in ((5, 1, "LONG_BUILD"), (5, -1, "SHORT_COVER"), (-5, 1, "SHORT_BUILD"), (-5, -1, "LONG_UNWIND")):
        assert S.buildup(dp, doi) == lab
    assert S.buildup(float("nan"), 1) == "NEUTRAL"
    assert S.BUILDUP_SIDE["SHORT_COVER"] == 1 and S.BUILDUP_SIDE["LONG_UNWIND"] == -1


def test_combine_oi_sums_last_known_other_months():
    t = 1.79e9
    near = [{"ts": t + 300 * k, "oi": 10.0} for k in range(3)]
    nxt = [{"ts": t + 300, "oi": 4.0}, {"ts": t + 600, "oi": 6.0}]
    assert S.combine_oi(near, [nxt]) == [10.0, 14.0, 16.0]


def test_bvc_delta_bounded_and_causal():
    b = fut_bars(3)
    d, sh = S.bvc_delta(b)
    assert all(abs(x) <= q["volume"] + 1e-9 for x, q in zip(d, b))
    assert all(0 <= s <= 1 for s in sh)
    assert S.bvc_delta(b[:100])[0] == d[:100]
    cvd = S.session_cvd(b, d)
    first = [i for i in range(1, len(b)) if S.ist(b[i]["ts"]).date() != S.ist(b[i - 1]["ts"]).date()]
    assert all(abs(cvd[i] - d[i]) < 1e-9 for i in first)  # resets each session


def test_orderflow_state_uses_only_past_bars():
    b = fut_bars(6)
    full = S.of_prepare(b)
    for i in range(200, len(b), 37):
        part = S.of_prepare(b[:i + 1])
        a, c = S.orderflow_state(b, full, i), S.orderflow_state(b[:i + 1], part, i)
        assert a["signal"] == c["signal"] and a["side"] == c["side"]
        if a["checks"]:
            assert [x[1] for x in a["checks"]] == [x[1] for x in c["checks"]]
            assert "ESTIMATED" in a["delta_label"]


def test_breadth_and_book_are_labelled_unavailable_historically():
    b = fut_bars(5)
    ctx = S.of_prepare(b)
    seen = False
    for i in range(100, len(b)):
        st = S.orderflow_state(b, ctx, i)
        if st["checks"]:
            names = {n: p for n, p, _ in st["checks"]}
            assert names["NIFTY 50 breadth agrees"] is None and names["resting order book leans the same way"] is None
            seen = True
    assert seen


def test_live_breadth_and_book_enter_score():
    b = fut_bars(5)
    ctx = S.of_prepare(b)
    br = S.breadth_from_quotes({s: {"ltp": 101, "prev_close": 100} for s in list(S.NIFTY50)[:40]})
    bk = S.book_from_quote({"buy_quantity": 900, "sell_quantity": 100, "depth": {"buy": [], "sell": []}})
    for i in range(100, len(b)):
        st = S.orderflow_state(b, ctx, i, br, bk)
        if st["side"] == "bullish":
            names = {n: p for n, p, _ in st["checks"]}
            assert names["NIFTY 50 breadth agrees"] is True and names["resting order book leans the same way"] is True
            assert "ORDER FLOW" in S.of_text(st)
            return
    raise AssertionError("no bullish bar")


def test_backtest_records_orderflow_and_confirm_filter_subsets():
    b = fut_bars(20, seed=7)
    ctx = S.of_prepare(S.adjusted(b))
    base = S.backtest(b, 65, "NSE", S.PARAMS, None, "realistic", of_ctx=ctx)["trades"]
    conf = S.backtest(b, 65, "NSE", S.PARAMS, {"of_confirm": True}, "realistic", of_ctx=ctx)["trades"]
    assert len(conf) <= len(base)
    for t in base:
        assert "of_agree" in t
    assert all(t["of_agree"] for t in conf)


def test_no_order_endpoints_and_license_notice():
    src = (ROOT / "dhan_cloud_single" / "main.py").read_text()
    assert all("order" not in e.lower() for e in S.ALLOWED)
    try:
        S.post("/orders", {})
        raise AssertionError("order endpoint not blocked")
    except ValueError:
        pass
    assert "Mr. Chartist" in src and "no code copied" in src.lower()


# ---------------------------------------------------------------- VPS engine (WebSocket feed)
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))
import main as M  # noqa: E402


def test_vps_uses_the_shared_library_and_constituents():
    assert M.of_lib() is not None and len(M.NIFTY50_SECTORS) == 50


def test_tickflow_quote_rule_tick_rule_and_session_reset():
    f = M.TickFlow()
    t = dt.datetime(2026, 11, 9, 9, 20, tzinfo=M.IST).timestamp()
    f.update(100.0, 1000, 5e6, 99.9, 100.1, t)            # first packet: baseline only
    f.update(100.1, 1100, 5e6, 100.0, 100.2, t + 1)       # at previous ask -> buy 100
    f.update(100.0, 1150, 5e6, 99.95, 100.05, t + 2)      # at previous bid -> sell 50
    f.update(100.02, 1170, 5e6, 99.95, 100.05, t + 3)     # inside spread above mid -> buy 20
    assert f.cvd == 100 - 50 + 20 and f.buy_vol == 120 and f.sell_vol == 50 and f.n_quote == 3
    NAN = float("nan")
    f.update(100.0, 1170, None, NAN, NAN, t + 5)          # no new volume -> nothing
    assert f.cvd == 70
    g = M.TickFlow()                                       # no book at all -> tick rule
    g.update(100.0, 10, None, NAN, NAN, t)
    g.update(100.5, 30, None, NAN, NAN, t + 1)
    g.update(100.5, 35, None, NAN, NAN, t + 2)             # unchanged price keeps the last side
    assert g.cvd == 25 and g.n_tick == 2 and g.n_quote == 0
    f.update(100.0, 10, None, NAN, NAN, t + 86400)        # next session resets
    assert f.cvd == 0 and f.last_vol == 10


def test_orderflow_view_on_feed_bars_and_book():
    b = fut_bars(4)
    b1 = []
    for x in b:                                            # explode 5m into 1m bars
        for k in range(5):
            b1.append({"ts": x["ts"] + 60 * k, "open": x["open"], "high": x["high"], "low": x["low"],
                       "close": x["close"], "volume": x["volume"] / 5, "oi": x["oi"]})
    tk = M.Tick(1, "NSE_FNO", buy_qty=800, sell_qty=200, bids=[(100.0, 50, 3)], asks=[(100.1, 20, 2)])
    out = M.orderflow_view(b1, b1[-1]["ts"] + 61, None, None,
                           {"HDFCBANK": {"ltp": 101, "prev_close": 100}}, M.tick_book_quote(tk))
    assert out["ok"] and out["research_only"]
    assert abs(out["book"]["total_imb"] - 0.6) < 1e-12 and out["breadth"]["advances"] == 1
    assert "ESTIMATED" in out["bvc"]["label"] and out["limitations"]
    assert out["bar_ts"] == b[-1]["ts"]                    # completed bars only
    part = M.orderflow_view(b1[:-2], b1[-3]["ts"] + 61)    # forming last bucket is excluded
    assert part["bar_ts"] == b[-2]["ts"]


def test_vps_orderflow_endpoint_and_snapshot_table(tmp_path):
    c = M.Config.from_env()
    c.data_dir, c.db_path = tmp_path, tmp_path / "t.db"
    c.client_id = c.access_token = c.token_file = ""
    c.telegram_token = c.telegram_chat_id = ""
    eng = M.Engine(c)
    assert "orderflow_snapshots" in M.Database.TABLE_COLS
    st = eng.states["NIFTY"]
    for x in fut_bars(3):
        for k in range(5):
            st.fut_bars.bars.append({"ts": x["ts"] + 60 * k, "open": x["close"], "high": x["high"], "low": x["low"],
                                     "close": x["close"], "volume": x["volume"] / 5, "oi": x["oi"]})
    import asyncio
    eng.db.start()
    out = asyncio.run(eng.orderflow_refresh("NIFTY", store=True))
    eng.db.stop()
    assert out["ok"] and out["index"] == "NIFTY"
    assert eng.db.query("SELECT COUNT(*) n FROM orderflow_snapshots")[0]["n"] == 1
    assert (ROOT / "orderflow.html").exists() and "/api/orderflow" in (ROOT / "main.py").read_text()
