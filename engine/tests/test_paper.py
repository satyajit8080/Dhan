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
from sensex.paper import PaperConfig, PaperTracker
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


class Filtered(unittest.TestCase):
    """FILTERED variant (user, 16 Oct): FIRST_TOUCH entry gated by day trend, level touches, session range,
    strike moneyness and overshoot."""
    LEGS = {(800.0, "CE"): "8", (900.0, "CE"): "9", (1100.0, "CE"): "11", (1100.0, "PE"): "12"}
    Q = {"8": (200.0, 201.0), "9": (110.0, 111.0), "11": (20.0, 21.0), "12": (99.0, 100.0)}

    def ctx(self, fut=1010.0, opn=990.0, hi=1300.0, lo=900.0, ce_touches=2, pe_touches=2):
        return {"futuresLtp": fut, "futuresOpen": opn, "sessionHigh": hi, "sessionLow": lo,
                "touches": {("CE", 1000.0): ce_touches, ("PE", 990.0): pe_touches}}

    def run_cross(self, ctx, index_at_cross=1001.0, rows=None):
        t, log = tracker(variant="FILTERED")
        rows = rows or [Row(800.0, "CE", 1000.0), Row(900.0, "CE", 1000.0), Row(1100.0, "CE", 1000.0)]
        t.arm(rows, self.LEGS, True, ctx)
        t.on_tick(T10, 995.0, self.Q)
        t.on_tick(T10 + 5000, index_at_cross, self.Q)
        return t, log

    def test_all_filters_pass_enters_except_deep_itm_strike(self):
        t, log = self.run_cross(self.ctx())
        entries = [f for e, f in log.events if e == "paper_entry"]
        self.assertEqual(sorted(f["strike"] for f in entries), [900.0, 1100.0])   # 800 is 201 pts ITM
        self.assertTrue(all(f["variant"] == "FILTERED" and f["filterFacts"]["levelTouches"] == 2 for f in entries))
        self.assertTrue(all(o.tid.startswith("F") for o in t.open))
        skip = [f for e, f in log.events if e == "paper_skip"]
        self.assertEqual(len(skip), 1)
        self.assertIn("in the money", skip[0]["reason"])

    def assert_skipped(self, ctx, text, **kw):
        t, log = self.run_cross(ctx, **kw)
        self.assertEqual(t.open, [])
        reasons = " ".join(f["reason"] for e, f in log.events if e == "paper_skip")
        self.assertIn(text, reasons)

    def test_against_day_trend_is_skipped(self):
        self.assert_skipped(self.ctx(fut=980.0, opn=990.0), "against the day trend")

    def test_level_touched_three_times_is_skipped(self):
        self.assert_skipped(self.ctx(ce_touches=3), "level touched 3 time(s)")

    def test_narrow_session_is_skipped(self):
        self.assert_skipped(self.ctx(hi=1100.0, lo=900.0), "session range 200.0 < 300.0")

    def test_late_entry_is_skipped(self):
        self.assert_skipped(self.ctx(), "already 25.0 beyond", index_at_cross=1025.0)

    def test_missing_data_fails_closed(self):
        self.assert_skipped({}, "futures open unavailable")

    def test_pe_uses_its_own_trend_and_touches(self):
        t, log = tracker(variant="FILTERED")
        t.arm([Row(1100.0, "PE", 990.0)], self.LEGS, True, self.ctx(fut=980.0, opn=995.0))
        t.on_tick(T10, 995.0, self.Q)
        t.on_tick(T10 + 5000, 989.0, self.Q)
        self.assertEqual([o.strike for o in t.open], [1100.0])        # PE 1100 is 111 pts ITM: under 150

    def test_scan_context_from_detail(self):
        from sensex.paper import scan_context
        d = {"futures": {"ltp": 1010.0, "ohlc": {"open": 990.0}},
             "idx1m": [{"high": 1005.0, "low": 950.0}, {"high": 1300.0, "low": 990.0}],
             "levels": {"breakoutAbove": 1000.0, "breakdownBelow": 990.0,
                        "resistance": {"touches": 4}, "support": None}}
        c = scan_context(d)
        self.assertEqual((c["futuresLtp"], c["futuresOpen"], c["sessionHigh"], c["sessionLow"]),
                         (1010.0, 990.0, 1300.0, 950.0))
        self.assertEqual(c["touches"], {("CE", 1000.0): 4, ("PE", 990.0): None})
        self.assertEqual(scan_context({})["sessionHigh"], None)
