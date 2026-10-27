"""Paper iron condor (engine/sensex/condor.py): plan days, strike choice, settlement, never raises, read-only."""
import unittest
from datetime import date, datetime

from sensex.condor import CondorPaper, chain_ids, plan, prev_trading_day
from sensex.pricing import b76_greeks, b76_iv, b76_price, ist_to_epoch_ms, year_fraction_to_expiry
from sensex.session import IST

ENTRY, EXPIRY = "2026-10-28", "2026-10-29"
STRIKES = [70000 + 100 * i for i in range(80)]          # 70,000 .. 77,900
SPOT, SIGMA = 73600.0, 0.14


class _Resp:
    def __init__(self, payload):
        self.payload = payload


def _bars(day, start, end, value_at):
    a = ist_to_epoch_ms("%sT%s" % (day, start)) // 1000
    b = ist_to_epoch_ms("%sT%s" % (day, end)) // 1000
    ts = list(range(a, b + 1, 60))
    return {"timestamp": ts, "close": [value_at(t) for t in ts]}


class FakeClient:
    """Serves Black-76 option prices on the entry day and a chosen index path on expiry day."""

    def __init__(self, settle_spot=74000.0, fail=False):
        self.calls, self.settle_spot, self.fail = [], settle_spot, fail

    def option_chain(self, expiry):
        self.calls.append(("optionchain", expiry))
        if self.fail:
            raise RuntimeError("feed down")
        return _Resp({"last_price": SPOT, "oc": {"%.6f" % k: {"ce": {"security_id": "C%d" % k},
                                                              "pe": {"security_id": "P%d" % k}} for k in STRIKES}})

    def intraday_candles(self, sid, seg, instrument, interval, frm, to):
        self.calls.append(("intraday", sid, seg, instrument, frm[:10]))
        day = frm[:10]
        if sid == "51":
            if day == ENTRY:
                return _Resp(_bars(day, "09:15", "09:45", lambda t: SPOT))
            return _Resp(_bars(day, "09:15", "15:29", lambda t: self.settle_spot))
        typ, k = ("CE" if sid[0] == "C" else "PE"), float(sid[1:])
        T = year_fraction_to_expiry(ist_to_epoch_ms(ENTRY + "T09:31"), EXPIRY)
        px = round(b76_price(SPOT, k, T, SIGMA, 0.0, typ), 2)
        return _Resp(_bars(day, "09:15", "09:45", lambda t: px))


def _tool(client, events):
    return CondorPaper(client, lambda *a, **k: events.append((a[1], k)), lots=13, lot_size=20)


class TestCondorPaper(unittest.TestCase):
    def test_plan_days(self):
        exp = [EXPIRY, "2026-11-05"]
        self.assertEqual(plan(date(2026, 10, 28), exp)["action"], "ENTRY")
        self.assertEqual(plan(date(2026, 10, 29), exp)["action"], "SETTLE")
        self.assertEqual(plan(date(2026, 10, 26), exp)["action"], "WAIT")
        self.assertEqual(plan(date(2026, 10, 30), exp)["expiry"], "2026-11-05")
        self.assertEqual(prev_trading_day(date(2026, 10, 29), frozenset({date(2026, 10, 28)})), date(2026, 10, 27))
        self.assertEqual(prev_trading_day(date(2026, 11, 2)), date(2026, 10, 30))      # Monday expiry -> Friday

    def test_chain_ids(self):
        ids = chain_ids({"oc": {"73500.000000": {"ce": {"security_id": 1}, "pe": {}}, "x": {}}})
        self.assertEqual(ids, {73500.0: {"CE": "1"}})

    def test_entry_picks_delta_010_strikes_and_wings(self):
        events = []
        c = FakeClient()
        e = _tool(c, events).rebuild_entry(EXPIRY, ENTRY, c.option_chain(EXPIRY).payload)
        sc, sp, bc, bp = e["legs"]
        self.assertEqual([(l["action"], l["type"]) for l in e["legs"]],
                         [("SELL", "CE"), ("SELL", "PE"), ("BUY", "CE"), ("BUY", "PE")])
        self.assertGreater(sc["strike"], SPOT)
        self.assertLess(sp["strike"], SPOT)
        self.assertEqual((bc["strike"] - sc["strike"], sp["strike"] - bp["strike"]), (1000, 1000))
        T = year_fraction_to_expiry(ist_to_epoch_ms(ENTRY + "T09:31"), EXPIRY)
        for leg in (sc, sp):                                   # first strike out from ATM with |delta| <= 0.10
            self.assertLessEqual(leg["delta"], 0.10)
            step = -100 if leg["type"] == "CE" else 100
            px = b76_price(SPOT, leg["strike"] + step, T, SIGMA, 0.0, leg["type"])
            inner = abs(b76_greeks(SPOT, leg["strike"] + step, T, b76_iv(px, SPOT, leg["strike"] + step, T, 0.0,
                                                                          leg["type"]), 0.0, leg["type"])["delta"])
            self.assertGreater(inner, 0.10)
        credit = sc["price"] + sp["price"] - bc["price"] - bp["price"]
        self.assertAlmostEqual(e["creditPts"], credit, places=2)
        self.assertEqual(e["quantityPerLeg"], 260)
        self.assertEqual(e["maxLossRs"], round((1000 - e["creditPts"]) * 260))
        self.assertTrue(all(k[0] in ("optionchain", "intraday") for k in c.calls))

    def test_settle_inside_and_beyond_wing(self):
        events = []
        for spot, want in ((74000.0, "keep"), (76500.0, "maxloss")):
            c = FakeClient(settle_spot=spot)
            t = _tool(c, events)
            e = t.rebuild_entry(EXPIRY, ENTRY, c.option_chain(EXPIRY).payload)
            r = t.settle(e, EXPIRY, final=True)
            if want == "keep":
                self.assertEqual(r["pnlPts"], e["creditPts"])
            else:
                self.assertAlmostEqual(r["pnlPts"], e["creditPts"] - 1000, places=2)
                self.assertAlmostEqual(r["grossRs"], -e["maxLossRs"], delta=1)
            self.assertEqual((r["status"], r["spotBars"]), ("FINAL", 30))

    def test_run_logs_entry_and_provisional_result_without_raising(self):
        events = []
        now = datetime(2026, 10, 29, 12, 0, tzinfo=IST)
        p = _tool(FakeClient(), events).run(date(2026, 10, 29), now, [EXPIRY])
        names = [n for n, _ in events]
        self.assertEqual(names, ["condor_plan", "condor_entry", "condor_settle", "condor_note"])
        self.assertEqual(p["result"]["status"], "PROVISIONAL")  # market still open at 12:00
        events.clear()
        _tool(FakeClient(fail=True), events).run(date(2026, 10, 29), now, [EXPIRY])
        self.assertEqual([n for n, _ in events], ["condor_plan", "condor_skip"])

    def test_entry_waits_for_the_0930_candle(self):
        events = []
        c = FakeClient()
        _tool(c, events).run(date(2026, 10, 28), datetime(2026, 10, 28, 9, 20, tzinfo=IST), [EXPIRY])
        self.assertEqual([n for n, _ in events], ["condor_plan", "condor_skip"])
        self.assertEqual(c.calls, [])


if __name__ == "__main__":
    unittest.main()
