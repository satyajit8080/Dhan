import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
"""Phase 6b: PAPER tracker (TP 6 / SL 11 premium points), fast poller, and the
1-minute / futures VWAP-volume observation facts. Synthetic data; no network;
no orders (the modules contain no order code)."""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from sensex.dhan_client import AuthError, Credentials, DhanClient, RateLimiter, RawResponse, Redactor
from sensex.fastpoll import FastPoller
from sensex.levels import session_vwap
from sensex.observation import futures_flow, one_minute_index
from sensex.paper import PaperConfig, PaperTracker, scan_context
from sensex.scan import in_no_trade_window
from sensex.scanner import JsonLogger

T10 = int(datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc).timestamp() * 1000)   # 10:00 IST
T1135 = int(datetime(2026, 9, 21, 6, 5, tzinfo=timezone.utc).timestamp() * 1000)  # 11:35 IST


class Row:
    def __init__(self, strike, side, trigger, status="OK"):
        self.strike, self.side, self.trigger, self.status = strike, side, trigger, status


class Log:
    def __init__(self):
        self.events = []

    def __call__(self, level, event, **f):
        self.events.append((event, f))

    def names(self):
        return [e for e, _ in self.events]


def tracker(**kw):
    log = Log()
    return PaperTracker(PaperConfig(**kw), log, in_no_trade_window), log


LEGS = {(100.0, "CE"): "11", (100.0, "PE"): "12", (200.0, "CE"): "21"}


class Tracker(unittest.TestCase):
    def test_take_profit_on_bid_after_entry_at_ask(self):
        t, log = tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T10, 998.0, {"11": (49.0, 50.0)})
        t.on_tick(T10 + 5000, 1000.0, {"11": (49.5, 50.5)})          # crossing (>= trigger): entry ask 50.5
        t.on_tick(T10 + 10000, 1003.0, {"11": (56.0, 57.0)})         # bid 56 -> +5.5: still open
        self.assertEqual(len(t.open), 1)
        t.on_tick(T10 + 15000, 1005.0, {"11": (56.6, 57.0)})         # +6.1 -> TP
        r = t.closed[0]
        self.assertEqual((r["outcome"], r["entryAsk"], r["exitBid"], r["pnlPts"]), ("TAKE_PROFIT", 50.5, 56.6, 6.1))
        self.assertEqual(r["heldS"], 10.0)
        self.assertEqual(log.names(), ["paper_entry", "paper_exit"])
        entry = log.events[0][1]
        self.assertEqual((entry["spreadPts"], entry["takeProfitAt"], entry["stopLossAt"]), (1.0, 56.5, 39.5))

    def test_stop_loss_and_time_stop_and_mfe_mae(self):
        t, _ = tracker()
        t.arm([Row(100.0, "PE", 990.0), Row(200.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T10, 995.0, {"12": (59.0, 60.0), "21": (30.0, 31.0)})
        t.on_tick(T10 + 5000, 990.0, {"12": (59.0, 60.0)})            # PE cross: entry 60
        t.on_tick(T10 + 10000, 992.0, {"12": (63.0, 64.0)})           # +3 favourable
        t.on_tick(T10 + 15000, 999.0, {"12": (48.5, 49.5)})           # -11.5 -> SL
        r = t.closed[0]
        self.assertEqual((r["outcome"], r["pnlPts"], r["maxFavourablePts"], r["maxAdversePts"]),
                         ("STOP_LOSS", -11.5, 3.0, -11.5))
        t.on_tick(T10 + 20000, 1001.0, {"21": (31.0, 32.0)})          # CE cross on strike 200: entry 32
        for k in range(1, 130):                                        # drifts, never +6 / -11
            t.on_tick(T10 + 20000 + k * 5000, 1001.0, {"21": (33.0, 33.5)})
        r2 = t.closed[1]
        self.assertEqual((r2["outcome"], r2["pnlPts"]), ("TIME_STOP", 1.0))
        self.assertEqual(r2["heldS"], 600.0)

    def test_crossing_is_required_and_fires_once_per_trigger(self):
        t, log = tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T10, 1002.0, {"11": (49.0, 50.0)})                  # already above when armed: no entry
        t.on_tick(T10 + 5000, 1003.0, {"11": (49.0, 50.0)})
        self.assertEqual(t.open, [])
        t.on_tick(T10 + 10000, 999.0, {"11": (49.0, 50.0)})
        t.on_tick(T10 + 15000, 1001.0, {"11": (49.0, 50.0)})          # real crossing
        self.assertEqual(len(t.open), 1)
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)                  # next scan, same trigger: not re-armed
        t.on_tick(T10 + 20000, 998.0, {"11": (49.0, 50.0)})
        t.on_tick(T10 + 25000, 1001.0, {"11": (49.0, 50.0)})
        self.assertEqual(log.names().count("paper_entry"), 1)

    def test_no_entry_in_no_trade_window_weak_level_or_blocked_scan(self):
        t, _ = tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T1135, 999.0, {"11": (49.0, 50.0)})
        t.on_tick(T1135 + 5000, 1001.0, {"11": (49.0, 50.0)})         # 11:35 IST: midday window
        self.assertEqual(t.open, [])
        t2, _ = tracker()
        t2.arm([Row(100.0, "CE", 1000.0, status="WEAK_LEVEL")], LEGS, True)
        self.assertEqual(t2.arms, {})
        t2.arm([Row(100.0, "CE", 1000.0)], LEGS, False)                # gate-blocked scan disarms
        self.assertEqual(t2.arms, {})

    def test_missing_ask_skips_and_gaps_are_counted(self):
        t, log = tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T10, 999.0, {})
        t.on_tick(T10 + 5000, 1001.0, {"11": (49.0, 0)})
        self.assertEqual((log.names(), t.open), (["paper_skip"], []))
        t3, _ = tracker()
        t3.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t3.on_tick(T10, 999.0, {"11": (49.0, 50.0)})
        t3.on_tick(T10 + 5000, 1001.0, {"11": (49.0, 50.0)})
        t3.on_tick(T10 + 10000, 1001.0, {})
        t3.close_all(T10 + 15000)
        r = t3.closed[0]
        self.assertEqual((r["outcome"], r["quoteGaps"], r["exitBid"]), ("SESSION_END", 1, None))

    def test_summary_and_rupees(self):
        t, _ = tracker(lot_size=10, cost_per_trade_rs=50.0)
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        t.on_tick(T10, 999.0, {"11": (49.0, 50.0)})
        t.on_tick(T10 + 5000, 1001.0, {"11": (49.0, 50.0)})
        t.on_tick(T10 + 10000, 1003.0, {"11": (56.0, 57.0)})
        r = t.closed[0]
        self.assertEqual((r["pnlRsGross"], r["pnlRsNet"]), (60.0, 10.0))
        s = t.summary()
        self.assertEqual((s["trades"], s["winRateTpVsSl"], s["breakEvenWinRate"], s["totalPnlPts"]),
                         (1, 1.0, 0.647, 6.0))


def quote_body(index=None, legs=None):
    d = {}
    if index is not None:
        d["IDX_I"] = {"51": {"last_price": index}}
    if legs:
        d["BSE_FNO"] = {sid: {"last_price": (b + a) / 2, "depth": {"buy": [{"price": b, "quantity": 20, "orders": 1}],
                                                                  "sell": [{"price": a, "quantity": 20, "orders": 1}]}}
                        for sid, (b, a) in legs.items()}
    return json.dumps({"status": "success", "data": d}).encode()


class Seq:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def send(self, method, url, headers, body, timeout):
        self.calls.append(json.loads(body))
        status, b, ms = self.items.pop(0)
        return RawResponse(status, b, {}, ms)


def poller(items, strikes=(100.0,)):
    t = Seq(items)
    sim = [0.0]
    lim = RateLimiter(clock=lambda: sim[0], sleep=lambda s: sim.__setitem__(0, sim[0] + s))
    red = Redactor()
    out = io.StringIO()
    log = JsonLogger(red, stream=out)
    client = DhanClient(Credentials("CID12345", "tok-not-real-000000"), transport=t, limiter=lim, redactor=red,
                        sleep=lambda s: None)
    tr = PaperTracker(PaperConfig(), log, in_no_trade_window)
    return FastPoller(client, tr, log, strikes), tr, t, out


class Poller(unittest.TestCase):
    CHAIN = {"strikes": [{"strike": 100.0, "ce": {"securityId": "11"}, "pe": {"securityId": "12"}},
                         {"strike": 300.0, "ce": {"securityId": "31"}, "pe": None}]}

    def test_poll_request_and_parsing_drive_the_tracker(self):
        p, tr, t, out = poller([(200, quote_body(999.0, {"11": (49.0, 50.0), "12": (60.0, 61.0)}), T10),
                                (200, quote_body(1001.0, {"11": (49.5, 50.5), "12": (59.0, 60.0)}), T10 + 5000),
                                (200, quote_body(1004.0, {"11": (57.0, 57.5), "12": (55.0, 56.0)}), T10 + 10000)])
        p.after_scan({"status": "OK"}, {"chain": self.CHAIN}, [Row(100.0, "CE", 1000.0), Row(300.0, "CE", 1000.0)])
        self.assertEqual(tr.legs, {(100.0, "CE"): "11", (100.0, "PE"): "12"})   # only configured strikes
        for _ in range(3):
            self.assertTrue(p.tick())
        self.assertEqual(t.calls[0], {"IDX_I": [51], "BSE_FNO": [11, 12]})
        self.assertEqual([r["outcome"] for r in tr.closed], ["TAKE_PROFIT"])
        self.assertEqual(tr.closed[0]["entryAsk"], 50.5)
        self.assertNotIn("tok-not-real", out.getvalue())

    def test_missing_index_logged_once_and_auth_stops(self):
        p, tr, t, out = poller([(200, quote_body(None, {"11": (1, 2)}), T10), (200, quote_body(None, {}), T10 + 5000),
                                (401, b'{"errorCode":"DH-901"}', T10 + 10000)])
        p.after_scan({"status": "OK"}, {"chain": self.CHAIN}, [Row(100.0, "CE", 1000.0)])
        self.assertTrue(p.tick())
        self.assertTrue(p.tick())
        self.assertEqual(out.getvalue().count("paper_index_missing"), 1)
        self.assertFalse(p.tick())


def bars(start_ms, closes, vols, step=60_000):
    return [{"timestampMs": start_ms + i * step, "open": c - 1, "high": c + 2, "low": c - 2, "close": c,
             "volume": v, "openInterest": None} for i, (c, v) in enumerate(zip(closes, vols))]


class MinuteFacts(unittest.TestCase):
    def test_futures_flow_vwap_and_relative_volume_use_closed_bars_only(self):
        b = bars(T10, [100 + i for i in range(13)], [10] * 11 + [30, 999])
        now = T10 + 12 * 60_000 + 5_000          # bar 12 (vol 999) is still forming
        fut = {"ltp": 120.0, "volume": 5000, "averagePrice": 110.0}
        f = futures_flow(fut, b, now)
        self.assertEqual(f["bars"], 12)
        self.assertEqual(f["lastClosedBarVolume"], 30)
        self.assertEqual(f["avgVolumePrev10"], 10.0)
        self.assertEqual(f["relVolumeVsPrev10"], 3.0)
        self.assertEqual(f["sessionVwap1m"], round(session_vwap(b[:12]), 2))
        self.assertEqual((f["dayVolume"], f["futLtpMinusDayAvg"]), (5000, 10.0))

    def test_index_volume_absent_gives_no_relative_volume(self):
        f = futures_flow(None, bars(T10, [100] * 12, [0] * 12), T10 + 13 * 60_000)
        self.assertIsNone(f["sessionVwap1m"])

    def test_one_minute_close_facts_against_triggers(self):
        b = bars(T10, [990, 1001, 1003, 1010], [0] * 4)
        o = one_minute_index(b, {"breakoutAbove": 1000.0, "breakdownBelow": 980.0}, T10 + 3 * 60_000 + 1)
        self.assertEqual(o["lastBars"][-1][0], "10:02")
        self.assertEqual((o["closeMinusBreakout"], o["closedAboveBreakout"], o["previousAlsoAbove"]), (3.0, True, True))
        self.assertEqual((o["closedBelowBreakdown"], o["previousAlsoBelow"]), (False, False))


class LoopIntegration(unittest.TestCase):
    def test_polls_run_between_scans_and_session_closes_paper(self):
        from test_scanner import FakeTime, scanner_for
        from sensex.mock_dhan import load_scenario
        _, ref = load_scenario("s21Sep")
        ft = FakeTime(datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, timezone.utc))
        sc, t, _, out = scanner_for("s21Sep", clock=ft.clock, sleep=ft.sleep,
                                    cfg_over={"max_scans": 2, "ignore_session": False, "extra_series": True})
        log = Log()
        sc.poller = FastPoller(sc.client, PaperTracker(PaperConfig(), log, in_no_trade_window), log, sc.cfg.strikes)
        self.assertEqual(sc.run_loop(), "MAX_SCANS")
        paths = [c["path"] for c in t.calls]
        self.assertEqual(paths.count("/optionchain"), 2)
        self.assertEqual(paths.count("/charts/intraday"), 6)        # 5-min index + 1-min futures + 1-min index, x2
        polls = [c for c in t.calls if c["path"] == "/marketfeed/quote" and "IDX_I" in (c["body"] or {})]
        self.assertEqual(len(polls), 11)                             # every 5 s inside the 60 s wait
        self.assertEqual(polls[0]["body"]["BSE_FNO"], sorted(polls[0]["body"]["BSE_FNO"]))
        self.assertIn("paper_index_missing", log.names())            # mock quote has no IDX_I block
        self.assertIn('"event": "paper_summary"', out.getvalue())


class ConfirmedClose(unittest.TestCase):
    def tracker(self):
        log = Log()
        return PaperTracker(PaperConfig(variant="CONFIRMED_CLOSE"), log, in_no_trade_window), log

    def test_enters_only_after_a_one_minute_close_beyond_the_trigger(self):
        t, log = self.tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        m0 = T10 - T10 % 60000                                  # start of a minute
        t.on_tick(m0 + 5000, 999.0, {"11": (49.0, 50.0)})
        t.on_tick(m0 + 10000, 1001.0, {"11": (49.5, 50.5)})     # crossing: pending, no entry yet
        self.assertEqual((t.open, list(t.pending)), ([], [("CE", 1000.0)]))
        t.on_tick(m0 + 55000, 1003.0, {"11": (51.0, 52.0)})     # last poll of the minute: close ~1003
        t.on_tick(m0 + 60000, 1004.0, {"11": (52.0, 53.0)})     # minute rolled: confirmed -> entry at 53
        self.assertEqual(len(t.open), 1)
        e = [f for n, f in log.events if n == "paper_entry"][0]
        self.assertEqual((e["variant"], e["entryAsk"], e["confirmClose"], e["side"]), ("CONFIRMED_CLOSE", 53.0, 1003.0, "CE"))
        t.on_tick(m0 + 65000, 1008.0, {"11": (59.5, 60.0)})
        self.assertEqual((t.closed[0]["outcome"], t.closed[0]["paperId"]), ("TAKE_PROFIT", "C001"))

    def test_touch_that_closes_back_inside_is_skipped(self):
        t, log = self.tracker()
        t.arm([Row(100.0, "CE", 1000.0)], LEGS, True)
        m0 = T10 - T10 % 60000
        t.on_tick(m0 + 5000, 999.0, {"11": (49.0, 50.0)})
        t.on_tick(m0 + 10000, 1001.0, {"11": (49.5, 50.5)})     # touch
        t.on_tick(m0 + 55000, 998.0, {"11": (48.0, 49.0)})      # closes back below
        t.on_tick(m0 + 60000, 998.0, {"11": (48.0, 49.0)})
        t.on_tick(m0 + 120000, 997.0, {"11": (48.0, 49.0)})     # second minute also below -> give up
        self.assertEqual(t.open, [])
        self.assertEqual(t.pending, {})
        self.assertIn("no 1-min close beyond", [f for n, f in log.events if n == "paper_skip"][0]["reason"])

    def test_two_variants_share_polls_in_the_poller(self):
        p, tr, t, out = poller([(200, quote_body(999.0, {"11": (49.0, 50.0)}), T10),
                                (200, quote_body(1001.0, {"11": (49.5, 50.5)}), T10 + 5000)])
        second = PaperTracker(PaperConfig(variant="CONFIRMED_CLOSE"), Log(), in_no_trade_window)
        p.trackers.append(second)
        p.after_scan({"status": "OK", "strikesUsed": [100.0]}, {"chain": Poller.CHAIN}, [Row(100.0, "CE", 1000.0)])
        p.tick(); p.tick()
        self.assertEqual((len(tr.open), len(second.open), list(second.pending)), (1, 0, [("CE", 1000.0)]))


if __name__ == "__main__":
    unittest.main()



# ------------------------------------------------------------------ FILTERED (v7.1 fixes)
T0915 = T10 - 45 * 60_000                                         # 09:15 IST on the same day
CROSS = T10 + 5000                                                # the crossing poll in run_filtered()


def detail(fut_ltp=1010.0, fut_open=990.0, ltt=CROSS - 10_000, fetched=CROSS - 4000, hi=1300.0, lo=900.0, bars=None):
    """A scan's detail_out as Scanner fills it: normalised futures quote and 1-min index candles."""
    if bars is None:
        bars = [{"timestampMs": T0915, "open": 950.0, "high": hi, "low": lo, "close": 990.0},
                {"timestampMs": T0915 + 60_000, "open": 990.0, "high": 995.0, "low": 985.0, "close": 992.0}]
    return {"futures": {"ltp": fut_ltp, "ohlc": {"open": fut_open, "high": 1.0, "low": 1.0, "close": 1.0},
                        "lastTradeTimeMs": ltt, "provenance": {"epochMs": fetched}},
            "idx1m": bars}


SEL = {"CE": {"trigger": 1000, "levelPrice": 995.0, "touches": 2, "sources": ["swing_high"], "weak": False,
              "touchSource": "refresh_table.pick_level"},
       "PE": {"trigger": 990, "levelPrice": 995.0, "touches": 2, "sources": ["swing_low"], "weak": False,
              "touchSource": "refresh_table.pick_level"}}
FLEGS = {(800.0, "CE"): "8", (850.0, "CE"): "85", (900.0, "CE"): "9", (1100.0, "CE"): "11", (1100.0, "PE"): "12"}
FQ = {"8": (200.0, 201.0), "85": (150.0, 151.0), "9": (110.0, 111.0), "11": (20.0, 21.0), "12": (99.0, 100.0)}


def run_filtered(ctx, index=1001.0, rows=None, variant="FILTERED", **cfg):
    t, log = tracker(variant=variant, **cfg)
    t.arm(rows or [Row(900.0, "CE", 1000.0), Row(1100.0, "CE", 1000.0)], FLEGS, True, ctx)
    t.on_tick(T10, 995.0, FQ)
    t.on_tick(CROSS, index, FQ)
    checks = [f for e, f in log.events if e == "filter_check"]
    return t, log, checks


def ctx_of(sel=SEL, **kw):
    return scan_context(detail(**kw), sel)


class Filtered(unittest.TestCase):
    def test_all_pass_enters_and_logs_one_complete_check(self):
        t, log, checks = run_filtered(ctx_of())
        self.assertEqual(len(checks), 1)
        c = checks[0]
        self.assertEqual((c["decision"], c["enterStrikes"], c["reasons"]), ("ENTER", [900.0, 1100.0], []))
        self.assertEqual({k: v["result"] for k, v in c["filters"].items()},
                         {"dayTrend": "PASS", "levelTouches": "PASS", "sessionRange": "PASS"})
        self.assertEqual((c["eventId"], c["variant"], c["side"], c["trigger"], c["crossingMs"]),
                         ("F-E001", "FILTERED", "CE", 1000.0, CROSS))
        self.assertEqual(c["selectedLevel"]["levelPrice"], 995.0)
        self.assertEqual(c["filters"]["levelTouches"]["touchSource"], "refresh_table.pick_level")
        self.assertEqual(c["filters"]["dayTrend"]["ageS"], 10.0)
        self.assertEqual(c["filters"]["sessionRange"]["rangePts"], 400.0)
        self.assertEqual([x["itmPts"] for x in c["strikes"]], [101.0, -99.0])
        self.assertEqual(c["diagnostics"]["overshootPts"], 1.0)
        entries = [f for e, f in log.events if e == "paper_entry"]
        self.assertEqual([(f["strike"], f["eventId"]) for f in entries], [(900.0, "F-E001"), (1100.0, "F-E001")])
        self.assertTrue(all(o.tid.startswith("F") for o in t.open))
        t.on_tick(CROSS + 5000, 990.0, FQ)                 # back below and across again: fires once per trigger
        t.on_tick(CROSS + 10000, 1002.0, FQ)
        self.assertEqual(sum(1 for e, _ in log.events if e == "filter_check"), 1)

    def test_every_filter_is_evaluated_even_when_another_fails(self):
        _, log, (c,) = run_filtered(ctx_of(fut_ltp=980.0, hi=1100.0, lo=900.0,
                                           sel={"CE": dict(SEL["CE"], touches=3)}))
        self.assertEqual({k: v["result"] for k, v in c["filters"].items()},
                         {"dayTrend": "FAIL", "levelTouches": "FAIL", "sessionRange": "FAIL"})
        self.assertEqual(len(c["reasons"]), 3)
        self.assertEqual(c["decision"], "REJECT")
        self.assertNotIn("paper_entry", log.names())

    # ---- day trend: futures vs day open, last-trade age
    def trend(self, **kw):
        return run_filtered(ctx_of(**kw))[2][0]["filters"]["dayTrend"]

    def test_trend_fresh_quote_and_price_near_or_crossing_the_open(self):
        self.assertEqual(self.trend(fut_ltp=990.05)["result"], "PASS")
        at = self.trend(fut_ltp=990.0)
        self.assertEqual((at["result"], at["diffInTradeDirection"]), ("FAIL", 0.0))
        self.assertIn("at day open", at["reason"])
        self.assertEqual(self.trend(fut_ltp=989.95)["result"], "FAIL")

    def test_trend_age_boundaries_use_last_trade_time_not_fetch_time(self):
        self.assertEqual(self.trend(ltt=CROSS - 119_900)["result"], "PASS")
        self.assertEqual(self.trend(ltt=CROSS - 120_000)["result"], "PASS")
        stale = self.trend(ltt=CROSS - 120_100, fetched=CROSS - 1000)     # fetched 1 s ago, traded 120.1 s ago
        self.assertEqual((stale["result"], stale["ageS"], stale["maxAgeS"], stale["fetchedMs"]),
                         ("STALE", 120.1, 120.0, CROSS - 1000))
        self.assertEqual(stale["lastTradeMs"], CROSS - 120_100)
        self.assertEqual(self.trend(ltt=CROSS - 120_040)["result"], "STALE")     # 120.04 s: no rounding to 120.0
        self.assertEqual(self.trend(ltt=CROSS + 5000)["result"], "PASS")         # exactly at the clock tolerance
        self.assertEqual(self.trend(ltt=CROSS + 5040)["result"], "INVALID")

    def test_trend_missing_and_invalid_timestamps(self):
        self.assertEqual(self.trend(ltt=None)["result"], "MISSING")
        self.assertEqual(self.trend(ltt="10:00:00")["result"], "INVALID")
        self.assertEqual(self.trend(ltt=float("nan"))["result"], "INVALID")
        self.assertEqual(self.trend(ltt=CROSS + 6000)["result"], "INVALID")       # 6 s in the future (> 5 s tolerance)
        self.assertEqual(self.trend(ltt=CROSS + 4000)["result"], "PASS")          # within clock tolerance
        self.assertEqual(self.trend(fut_ltp=None)["result"], "MISSING")
        self.assertEqual(self.trend(fut_open=0)["result"], "INVALID")
        self.assertEqual(self.trend(fut_open=float("inf"))["result"], "INVALID")

    def test_pe_trend_direction(self):
        t, log = tracker(variant="FILTERED")
        t.arm([Row(1100.0, "PE", 990.0)], FLEGS, True, ctx_of(fut_ltp=980.0, fut_open=995.0))
        t.on_tick(T10, 995.0, FQ)
        t.on_tick(CROSS, 989.0, FQ)
        c = [f for e, f in log.events if e == "filter_check"][0]
        self.assertEqual((c["filters"]["dayTrend"]["result"], c["strikes"][0]["itmPts"], c["decision"]),
                         ("PASS", 111.0, "ENTER"))

    # ---- level touches and session range boundaries
    def test_touch_boundaries(self):
        for n, want in ((2, "PASS"), (3, "FAIL"), (4, "FAIL")):
            c = run_filtered(ctx_of(sel={"CE": dict(SEL["CE"], touches=n)}))[2][0]
            self.assertEqual(c["filters"]["levelTouches"]["result"], want, n)

    def test_missing_or_invalid_touch_data_fails_closed(self):
        for sel, want in (({}, "MISSING"), ({"CE": dict(SEL["CE"], touches=None)}, "MISSING"),
                          ({"CE": dict(SEL["CE"], touches="2")}, "INVALID"), ({"CE": dict(SEL["CE"], touches=True)}, "INVALID"),
                          ({"CE": dict(SEL["CE"], trigger=1005)}, "MISSING")):   # another trigger's level: never used
            t, log, (c,) = run_filtered(ctx_of(sel=sel))
            self.assertEqual((c["filters"]["levelTouches"]["result"], c["decision"]), (want, "REJECT"), sel)
            self.assertEqual(t.open, [])

    def test_range_boundaries_and_bad_bars(self):
        for hi, want in ((1199.99, "FAIL"), (1200.0, "PASS"), (1200.01, "PASS")):
            c = run_filtered(ctx_of(hi=hi, lo=900.0))[2][0]
            self.assertEqual(c["filters"]["sessionRange"]["result"], want, hi)
        self.assertEqual(run_filtered(ctx_of(bars=[]))[2][0]["filters"]["sessionRange"]["result"], "MISSING")
        bad = [{"timestampMs": T0915, "open": 1.0, "high": 900.0, "low": 1300.0, "close": 1.0}]
        self.assertEqual(run_filtered(ctx_of(bars=bad))[2][0]["filters"]["sessionRange"]["result"], "INVALID")
        nan = [{"timestampMs": T0915, "open": 1.0, "high": float("nan"), "low": 900.0, "close": 1.0}]
        self.assertEqual(run_filtered(ctx_of(bars=nan))[2][0]["filters"]["sessionRange"]["result"], "INVALID")

    def test_itm_boundaries_from_index_ltp_at_the_crossing(self):
        legs = {**FLEGS, (850.01, "CE"): "a", (850.0, "CE"): "b", (849.99, "CE"): "c"}
        q = dict(FQ, a=(150.0, 151.0), b=(150.0, 151.0), c=(150.0, 151.0))
        t, log = tracker(variant="FILTERED")
        t.arm([Row(k, "CE", 1000.0) for k in (850.01, 850.0, 849.99)], legs, True, ctx_of())
        t.on_tick(T10, 995.0, q)
        t.on_tick(CROSS, 1000.0, q)                        # index exactly at the trigger: ITM 149.99 / 150 / 150.01
        c = [f for e, f in log.events if e == "filter_check"][0]
        self.assertEqual([(x["itmPts"], x["result"]) for x in c["strikes"]],
                         [(149.99, "PASS"), (150.0, "PASS"), (150.01, "FAIL")])
        self.assertEqual(sorted(f["strike"] for e, f in log.events if e == "paper_entry"), [850.0, 850.01])
        self.assertEqual(c["strikeReference"], "SENSEX index LTP at the crossing poll (IDX_I:51)")

    def test_all_strikes_too_deep_rejects_with_reason(self):
        _, log, (c,) = run_filtered(ctx_of(), rows=[Row(800.0, "CE", 1000.0)])
        self.assertEqual((c["decision"], c["enterStrikes"]), ("REJECT", []))
        self.assertIn("in the money", c["reasons"][0])

    def test_spot_vs_0915_open_is_diagnostic_only(self):
        _, _, (c,) = run_filtered(ctx_of())                  # 09:15 bar open 950 < index 1001
        self.assertEqual(c["diagnostics"]["spotVsOpen0915"]["result"], "PASS")
        self.assertEqual(c["diagnostics"]["spotVsOpen0915"]["openBarIst"], "09:15")
        dhan = [{"timestampMs": T0915 + 60_000, "open": 950.0, "high": 1300.0, "low": 900.0, "close": 1.0}]
        _, _, (c,) = run_filtered(ctx_of(bars=dhan))         # Dhan's real first bar is stamped 09:16
        self.assertEqual((c["diagnostics"]["spotVsOpen0915"]["result"],
                          c["diagnostics"]["spotVsOpen0915"]["openBarIst"]), ("PASS", "09:16"))
        late = [{"timestampMs": T0915 + 120_000, "open": 1050.0, "high": 1300.0, "low": 900.0, "close": 1.0}]
        _, _, (c,) = run_filtered(ctx_of(bars=late))         # series starts 09:17: the open is unknown
        self.assertEqual((c["diagnostics"]["spotVsOpen0915"]["result"],
                          c["diagnostics"]["spotVsOpen0915"]["openBarIst"]), ("MISSING", None))
        above = [{"timestampMs": T0915, "open": 1050.0, "high": 1300.0, "low": 900.0, "close": 1.0}]
        _, _, (c,) = run_filtered(ctx_of(bars=above))        # spot below its open, futures above theirs
        self.assertEqual((c["diagnostics"]["spotVsOpen0915"]["result"], c["decision"]), ("FAIL", "ENTER"))

    def test_filtered_fills_are_the_first_touch_fills(self):
        ft, ft_log = tracker()
        fl, fl_log = tracker(variant="FILTERED")
        rows = [Row(900.0, "CE", 1000.0), Row(1100.0, "CE", 1000.0)]
        ft.arm(rows, FLEGS, True)
        fl.arm(rows, FLEGS, True, ctx_of())
        for tr in (ft, fl):
            tr.on_tick(T10, 995.0, FQ)
            tr.on_tick(CROSS, 1001.0, FQ)
        strip = lambda log: [(f["strike"], f["entryAsk"], f["entryBid"]) for e, f in log.events if e == "paper_entry"]
        self.assertEqual(strip(ft_log), strip(fl_log))
        self.assertNotIn("filter_check", ft_log.names())

    def test_scan_context_fields(self):
        c = scan_context(detail(), SEL)
        self.assertEqual((c["futuresLtp"], c["futuresOpen"], c["futuresLastTradeMs"], c["futuresFetchedMs"]),
                         (1010.0, 990.0, CROSS - 10_000, CROSS - 4000))
        self.assertEqual((c["sessionHigh"], c["sessionLow"], c["indexOpen0915"]), (1300.0, 900.0, 950.0))
        self.assertEqual(scan_context({})["sessionHigh"], None)


class FreshLevel(unittest.TestCase):
    def test_touches_alone_decide_and_every_strike_enters(self):
        for n, want in ((0, "ENTER"), (2, "ENTER"), (3, "REJECT"), (4, "REJECT")):
            # trend against, narrow range, deep ITM strike: none of these matter for FRESH_LEVEL
            t, log, (c,) = run_filtered(ctx_of(fut_ltp=980.0, hi=1000.0, lo=990.0,
                                               sel={"CE": dict(SEL["CE"], touches=n)}),
                                        rows=[Row(800.0, "CE", 1000.0), Row(1100.0, "CE", 1000.0)], variant="FRESH_LEVEL")
            self.assertEqual((list(c["filters"]), c["filters"]["levelTouches"]["result"], c["decision"]),
                             (["levelTouches"], "PASS" if want == "ENTER" else "FAIL", want), n)
            self.assertEqual([x["result"] for x in c["strikes"]], ["NOT_APPLIED"] * 2)
            entries = [f for e, f in log.events if e == "paper_entry"]
            self.assertEqual([(f["strike"], f["eventId"], f["variant"]) for f in entries],
                             [(800.0, "L-E001", "FRESH_LEVEL"), (1100.0, "L-E001", "FRESH_LEVEL")] if want == "ENTER" else [])
            self.assertTrue(all(o.tid.startswith("L") for o in t.open))

    def test_missing_or_other_triggers_level_never_enters(self):
        for sel in ({}, {"CE": dict(SEL["CE"], touches=None)}, {"CE": dict(SEL["CE"], trigger=1005)}):
            t, _, (c,) = run_filtered(ctx_of(sel=sel), variant="FRESH_LEVEL")
            self.assertEqual((c["filters"]["levelTouches"]["result"], c["decision"], t.open), ("MISSING", "REJECT", []))

    def test_fills_are_the_first_touch_fills(self):
        ft, ft_log = tracker()
        fl, fl_log = tracker(variant="FRESH_LEVEL")
        rows = [Row(900.0, "CE", 1000.0), Row(1100.0, "CE", 1000.0)]
        ft.arm(rows, FLEGS, True)
        fl.arm(rows, FLEGS, True, ctx_of())
        for tr in (ft, fl):
            tr.on_tick(T10, 995.0, FQ)
            tr.on_tick(CROSS, 1001.0, FQ)
        strip = lambda log: [(f["strike"], f["entryAsk"], f["entryBid"]) for e, f in log.events if e == "paper_entry"]
        self.assertEqual(strip(ft_log), strip(fl_log))


class FreshExit(unittest.TestCase):
    def run_day(self, variant, back_close):
        t, log, (c,) = run_filtered(ctx_of(), variant=variant)       # enters at CROSS (10:00:05), index 1001
        q = dict(FQ, **{"9": (112.0, 113.0), "11": (19.0, 20.0)})
        t.on_tick(T10 + 50_000, back_close, q)                      # last poll of the 10:00 minute = its close
        t.on_tick(T10 + 60_000, 1001.0, q)                          # first poll of 10:01: the 10:00 minute closed
        return t, log, c

    def test_close_back_through_the_trigger_exits_at_this_polls_bid(self):
        t, log, c = self.run_day("FRESH_EXIT", 999.0)
        self.assertEqual((c["eventId"], list(c["filters"]), c["decision"]), ("X-E001", ["levelTouches"], "ENTER"))
        exits = [f for e, f in log.events if e == "paper_exit"]
        self.assertEqual([(f["paperId"], f["outcome"], f["exitBid"], f["pnlPts"], f["indexClose"]) for f in exits],
                         [("X001", "INDEX_BACK", 112.0, 1.0, 999.0), ("X002", "INDEX_BACK", 19.0, -2.0, 999.0)])
        self.assertEqual(t.open, [])

    def test_close_at_or_beyond_the_trigger_keeps_the_trade(self):
        for close in (1000.0, 1003.0):
            t, log, _ = self.run_day("FRESH_EXIT", close)
            self.assertNotIn("paper_exit", log.names(), close)
            self.assertEqual(len(t.open), 2)

    def test_pe_direction(self):
        sel = {"PE": dict(SEL["PE"], trigger=1000)}
        t, log = tracker(variant="FRESH_EXIT")
        t.arm([Row(1100.0, "PE", 1000.0)], FLEGS, True, ctx_of(sel=sel))
        t.on_tick(T10, 1005.0, FQ)
        t.on_tick(CROSS, 999.0, FQ)
        t.on_tick(T10 + 50_000, 1001.0, FQ)                         # PE: a close above the trigger is back through
        t.on_tick(T10 + 60_000, 999.0, FQ)
        self.assertEqual([f["outcome"] for e, f in log.events if e == "paper_exit"], ["INDEX_BACK"])

    def test_fresh_level_never_exits_on_close_back(self):
        t, log, _ = self.run_day("FRESH_LEVEL", 999.0)
        self.assertNotIn("paper_exit", log.names())
        self.assertEqual(len(t.open), 2)


class FreshSl8AndAfterExit(unittest.TestCase):
    def test_fresh_sl8_stops_at_minus_8_and_reports_its_break_even(self):
        t, log, (c,) = run_filtered(ctx_of(), variant="FRESH_SL8")
        self.assertEqual((c["eventId"], list(c["filters"])), ("S-E001", ["levelTouches"]))
        entries = [f for e, f in log.events if e == "paper_entry"]
        self.assertEqual([f["stopLossAt"] for f in entries], [103.0, 13.0])          # asks 111 / 21, stop -8
        t.on_tick(CROSS + 5000, 1001.0, dict(FQ, **{"9": (103.5, 104.0), "11": (13.0, 14.0)}))
        exits = [f for e, f in log.events if e == "paper_exit"]
        self.assertEqual([(f["paperId"], f["outcome"]) for f in exits], [("S002", "STOP_LOSS")])   # -7.5 holds, -8 stops
        self.assertEqual(t.summary()["breakEvenWinRate"], round(8 / 14, 3))
        self.assertEqual(PaperConfig().stop_loss_pts, 11.0)                          # default unchanged

    def test_after_exit_recording_never_changes_the_trade(self):
        t, log = tracker(post_exit_track_s=20)
        t.arm([Row(900.0, "CE", 1000.0)], FLEGS, True)
        t.on_tick(T10, 995.0, FQ)
        t.on_tick(CROSS, 1001.0, FQ)                                   # entry at ask 111
        t.on_tick(CROSS + 5000, 1001.0, {"9": (117.5, 118.0)})         # take profit
        self.assertEqual([a["sid"] for a in t.after], ["9"])          # still watched after the exit
        for i, bid in enumerate((120.0, 125.0, 109.0, 130.0), start=2):
            t.on_tick(CROSS + 5000 * i, 1001.0, {"9": (bid, bid + 1)})
        ex = [f for e, f in log.events if e == "paper_exit"]
        after = [f for e, f in log.events if e == "paper_after_exit"]
        self.assertEqual((len(ex), ex[0]["pnlPts"], ex[0]["outcome"]), (1, 6.5, "TAKE_PROFIT"))
        self.assertEqual(len(after), 1)
        a = after[0]
        self.assertEqual((a["paperId"], a["endReason"], a["bestPnlAfterPts"], a["worstPnlAfterPts"]),
                         ("P001", "TRACK_TIME_DONE", 19.0, -2.0))              # 130 - 111 / 109 - 111
        self.assertEqual([b for _, b in a["path"]], [120.0, 125.0, 109.0, 130.0])
        self.assertEqual(t.after, [])

    def test_after_exit_off_by_default_and_flushed_at_session_end(self):
        t, log = tracker()
        t.arm([Row(900.0, "CE", 1000.0)], FLEGS, True)
        t.on_tick(T10, 995.0, FQ); t.on_tick(CROSS, 1001.0, FQ); t.on_tick(CROSS + 5000, 1001.0, {"9": (118.0, 119.0)})
        self.assertNotIn("paper_after_exit", log.names())
        t2, log2 = tracker(post_exit_track_s=600)
        t2.arm([Row(900.0, "CE", 1000.0)], FLEGS, True)
        t2.on_tick(T10, 995.0, FQ); t2.on_tick(CROSS, 1001.0, FQ); t2.on_tick(CROSS + 5000, 1001.0, {"9": (118.0, 119.0)})
        t2.close_all(CROSS + 10000)
        (a,) = [f for e, f in log2.events if e == "paper_after_exit"]
        self.assertEqual((a["endReason"], a["path"]), ("SESSION_END", []))


class FreshExtreme(unittest.TestCase):
    def test_trigger_must_be_a_new_session_extreme(self):
        for hi, want in ((999.0, "ENTER"), (1000.0, "ENTER"), (1000.5, "REJECT")):   # CE trigger 1000
            t, log, (c,) = run_filtered(ctx_of(hi=hi, lo=900.0), variant="FRESH_EXTREME")
            self.assertEqual((c["eventId"], list(c["filters"]), c["decision"]),
                             ("E-E001", ["levelTouches", "newExtreme"], want), hi)
            self.assertEqual(bool([e for e, _ in log.events if e == "paper_entry"]), want == "ENTER")

    def test_old_level_rejected_even_at_a_new_extreme(self):
        _, _, (c,) = run_filtered(ctx_of(hi=990.0, sel={"CE": dict(SEL["CE"], touches=3)}), variant="FRESH_EXTREME")
        self.assertEqual((c["filters"]["newExtreme"]["result"], c["decision"]), ("PASS", "REJECT"))

    def test_pe_and_missing_or_bad_bars(self):
        sel = {"PE": dict(SEL["PE"], trigger=1000)}
        for lo, want in ((1001.0, "PASS"), (999.0, "FAIL")):
            t, log = tracker(variant="FRESH_EXTREME")
            bars = [{"timestampMs": T0915, "open": 1100.0, "high": 1300.0, "low": lo, "close": 1100.0}]
            t.arm([Row(1100.0, "PE", 1000.0)], FLEGS, True, ctx_of(sel=sel, bars=bars))
            t.on_tick(T10, 1005.0, FQ); t.on_tick(CROSS, 999.0, FQ)
            (c,) = [f for e, f in log.events if e == "filter_check"]
            self.assertEqual(c["filters"]["newExtreme"]["result"], want, lo)
        self.assertEqual(run_filtered(ctx_of(bars=[]), variant="FRESH_EXTREME")[2][0]["filters"]["newExtreme"]["result"],
                         "MISSING")
        bad = [{"timestampMs": T0915, "open": 1.0, "high": 900.0, "low": 1300.0, "close": 1.0}]
        self.assertEqual(run_filtered(ctx_of(bars=bad), variant="FRESH_EXTREME")[2][0]["decision"], "REJECT")


class ConfirmedOvershootOption(unittest.TestCase):
    def confirm(self, index, **cfg):
        t, log = tracker(variant="CONFIRMED_CLOSE", **cfg)
        t.arm([Row(1100.0, "CE", 1000.0)], FLEGS, True)
        t.on_tick(T10, 995.0, FQ)
        t.on_tick(T10 + 5000, 1001.0, FQ)                 # crossing in minute 10:00
        t.on_tick(T10 + 55_000, 1003.0, FQ)               # last poll of 10:00: close 1003 > trigger
        t.on_tick(T10 + 60_000, index, FQ)                # first poll of 10:01: confirmation + entry at this index
        return t, log

    def test_default_off_keeps_behaviour(self):
        t, log = self.confirm(1050.0)
        self.assertEqual(len(t.open), 1)
        self.assertEqual([f["indexAtCross"] for e, f in log.events if e == "paper_entry"], [1050.0])

    def test_boundaries_below_at_above_20(self):
        for index, entered in ((1019.99, True), (1020.0, False), (1020.01, False)):
            t, log = self.confirm(index, confirm_max_overshoot_pts=20.0)
            self.assertEqual(bool(t.open), entered, index)
            if not entered:
                skip = [f for e, f in log.events if e == "paper_skip"][0]
                self.assertEqual((skip["maxOvershootPts"], skip["overshootPts"]), (20.0, round(index - 1000.0, 2)))

    def test_pe_direction(self):
        t, log = tracker(variant="CONFIRMED_CLOSE", confirm_max_overshoot_pts=20.0)
        t.arm([Row(1100.0, "PE", 990.0)], FLEGS, True)
        t.on_tick(T10, 995.0, FQ)
        t.on_tick(T10 + 5000, 989.0, FQ)
        t.on_tick(T10 + 55_000, 985.0, FQ)
        t.on_tick(T10 + 60_000, 969.0, FQ)                # 21 pts beyond on the PE side
        self.assertEqual(t.open, [])


class LevelIdentity(unittest.TestCase):
    """The FILTERED touch count must belong to the level the refresh table used for the trigger
    (refresh_table.pick_level), not to derive_levels' nearest level."""
    NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)          # 10:00 IST, outside the no-trade windows

    def scan(self, resistances, supports=()):
        from sensex.scan import run_refresh, selected_levels
        lv = lambda p, t, s: {"price": p, "touches": t, "sources": s}
        scan = {"status": "OK", "forward": 1000.0, "T": 0.01, "df": 0.999, "candleRefPrice": 1000.0,
                "resistances": [lv(*r) for r in resistances], "supports": [lv(*r) for r in supports],
                "atr": 10.0, "barMinutes": 5, "snapshotMs": T10, "candlesMs": T10 - 60_000,
                "legs": [{"strike": 1000.0, "isCall": True, "ivPct": 15.0, "bid": 20.0, "ask": 21.0, "ltp": 20.5},
                         {"strike": 1000.0, "isCall": False, "ivPct": 15.0, "bid": 20.0, "ask": 21.0, "ltp": 20.5}]}
        rows, _ = run_refresh(scan, self.NOW)
        return rows, selected_levels(scan)

    def check(self, rows, sel, side="CE"):
        """Arm a FILTERED tracker as FastPoller.after_scan does and cross the CE/PE trigger."""
        t, log = tracker(variant="FILTERED")
        legs = {(1000.0, "CE"): "1", (1000.0, "PE"): "2"}
        q = {"1": (20.0, 21.0), "2": (20.0, 21.0)}
        t.arm(rows, legs, True, scan_context(detail(), sel))
        trig = [r.trigger for r in rows if r.side == side and r.status == "OK"][0]
        t.on_tick(T10, 1000.0, q)
        t.on_tick(CROSS, trig + (1 if side == "CE" else -1), q)
        return [f for e, f in log.events if e == "filter_check"][0], trig

    def test_refresh_parameters_equal_build_refresh_table_defaults(self):
        import inspect
        import refresh_table as rt
        from sensex import scan as sc
        p = inspect.signature(rt.build_refresh_table).parameters
        self.assertEqual((p["buffer"].default, p["round_step"].default, p["min_touches"].default),
                         (sc.REFRESH_BUFFER, sc.REFRESH_ROUND_STEP, sc.REFRESH_MIN_TOUCHES))

    def test_nearest_one_touch_level_is_not_the_selected_level(self):
        rows, sel = self.scan([(1003.0, 1, ["session_high"]), (1012.0, 2, ["swing_high"])])
        c, trig = self.check(rows, sel)
        self.assertEqual(trig, 1015)                                    # from 1012 (+5), not 1003 (1010)
        self.assertEqual((c["selectedLevel"]["levelPrice"], c["filters"]["levelTouches"]["touches"],
                          c["filters"]["levelTouches"]["result"]), (1012.0, 2, "PASS"))

    def test_vwap_only_level_is_excluded(self):
        rows, sel = self.scan([(1003.0, 5, ["vwap"]), (1012.0, 3, ["swing_high"])])
        c, trig = self.check(rows, sel)
        self.assertEqual(trig, 1015)
        self.assertEqual((c["selectedLevel"]["sources"], c["filters"]["levelTouches"]["touches"],
                          c["filters"]["levelTouches"]["result"]), (["swing_high"], 3, "FAIL"))

    def test_multiple_candidates_both_sides(self):
        rows, sel = self.scan([(1030.0, 4, ["swing_high"]), (1003.0, 1, ["swing_high"]), (1012.0, 2, ["consolidation_high"])],
                              [(990.0, 1, ["swing_low"]), (980.0, 4, ["session_low"]), (960.0, 2, ["swing_low"])])
        c, trig = self.check(rows, sel)
        self.assertEqual((trig, c["selectedLevel"]["levelPrice"], c["filters"]["levelTouches"]["touches"]), (1015, 1012.0, 2))
        c, trig = self.check(rows, sel, side="PE")
        self.assertEqual((trig, c["selectedLevel"]["levelPrice"], c["filters"]["levelTouches"]["touches"]), (975, 980.0, 4))
        self.assertEqual(c["filters"]["levelTouches"]["result"], "FAIL")

    def test_rounding_to_the_same_trigger_does_not_borrow_a_touch_count(self):
        # 1011.0 (1 touch, nearer) and 1012.4 (2 touches) both round to trigger 1015; the selected level is 1012.4
        rows, sel = self.scan([(1011.0, 1, ["session_high"]), (1012.4, 2, ["swing_high"])])
        c, trig = self.check(rows, sel)
        self.assertEqual((trig, c["selectedLevel"]["levelPrice"], c["filters"]["levelTouches"]["touches"]),
                         (1015, 1012.4, 2))

    def test_level_from_another_scan_is_never_used(self):
        rows, _ = self.scan([(1003.0, 1, ["session_high"]), (1012.0, 2, ["swing_high"])])
        _, other = self.scan([(1022.0, 2, ["swing_high"])])            # selects trigger 1025, not 1015
        c, _ = self.check(rows, other)
        self.assertEqual((c["filters"]["levelTouches"]["result"], c["selectedLevel"], c["decision"]),
                         ("MISSING", None, "REJECT"))

    def test_no_rows_no_selected_levels(self):
        from sensex.scan import selected_levels
        self.assertEqual(selected_levels({"status": "BLOCKED"}), {})
        self.assertEqual(selected_levels({}), {})

    def test_fast_poller_passes_the_scans_selected_level_to_the_tracker(self):
        p, tr, _, _ = poller([])
        scan = {"status": "OK", "candlesMs": T10, "candleRefPrice": 1000.0,
                "resistances": [{"price": 1003.0, "touches": 1, "sources": ["session_high"]},
                                {"price": 1012.0, "touches": 2, "sources": ["swing_high"]}], "supports": []}
        p.after_scan({"status": "OK", "scanInputs": scan}, detail(), [Row(100.0, "CE", 1015)])
        self.assertEqual((tr.ctx["selected"]["CE"]["levelPrice"], tr.ctx["selected"]["CE"]["trigger"]), (1012.0, 1015))
        self.assertEqual(tr.arms[("CE", 1015.0)].level["touches"], 2)
