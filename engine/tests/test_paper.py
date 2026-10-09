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


if __name__ == "__main__":
    unittest.main()
