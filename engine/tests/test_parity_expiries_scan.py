"""Parity: sensex.expiries + sensex.scan vs TS expiries.ts and the TS-composed refresh scan."""

import unittest
from datetime import datetime, timedelta, timezone

from parity_support import ParityMixin, load, run
from sensex.expiries import clean_expiries, is_iso_date, nearest, next_after
from sensex.normalize import to_candles
from sensex.scan import BLANK_TABLE, build_scan, run_refresh

E = load("expiries")
S = load("scan")
LABEL = "IDX_I:51"
IST = timezone(timedelta(hours=5, minutes=30))


class ExpiryParity(ParityMixin, unittest.TestCase):
    def test_is_iso_date(self):
        c = E["isIsoDate"]
        self.assertParity([is_iso_date(x) for x in c["input"]], c["expected"], "isIsoDate")

    def test_selection(self):
        ids = [k for k in E if k.startswith("expiries/")]
        self.assertEqual(len(ids), 7)
        for cid in ids:
            data, today = E[cid]["input"]["data"], E[cid]["input"]["today"]
            got = {
                "list": run(lambda: clean_expiries(data, LABEL)),
                "nearest": run(lambda: nearest(clean_expiries(data, LABEL), today, LABEL)),
                "nextAfter": run(lambda: next_after(clean_expiries(data, LABEL), today, LABEL)),
            }
            self.assertParity(got, E[cid]["expected"], cid)


def py_scan(i):
    return build_scan(raw_chain=i["chain"], raw_futures=i["fut"], candles=to_candles(i["candles"]),
                      expiry=i["expiry"], strikes=i["strikes"], chain_received_ms=i["chainMs"],
                      futures_received_ms=i["futMs"], futures_expiry=i.get("futuresExpiry"))


class ScanParity(ParityMixin, unittest.TestCase):
    def test_scan_inputs_match_typescript(self):
        self.assertEqual(len(S), 9)
        for cid, c in S.items():
            self.assertParity(run(lambda: py_scan(c["input"])), c["expected"], cid)

    def test_refresh_table_identical_from_ts_and_py_inputs(self):
        """The existing refresh_table must produce IDENTICAL rows whether fed
        the TS-computed scan or the Python-computed one, across the session
        boundaries: in-session, opening skip, lunch skip, after 14:50, after close."""
        for cid, c in S.items():
            snap_day = datetime.fromtimestamp(c["input"]["chainMs"] / 1000, IST).date()
            for hh, mm in [(10, 33), (9, 20), (12, 0), (14, 55), (15, 45)]:
                now = datetime(snap_day.year, snap_day.month, snap_day.day, hh, mm)
                ts_rows, ts_table = run_refresh(c["expected"], now)
                py_rows, py_table = run_refresh(py_scan(c["input"]), now)
                self.assertEqual(py_table, ts_table, "%s @ %02d:%02d" % (cid, hh, mm))
                self.assertEqual(py_rows, ts_rows, "%s @ %02d:%02d" % (cid, hh, mm))

    def test_blocked_scans_give_the_blank_table(self):
        for cid in ("scan/futuresMissing", "scan/futuresDiverge", "scan/emptyChain", "scan/expired"):
            self.assertEqual(run_refresh(py_scan(S[cid]["input"]), datetime(2026, 9, 18, 10, 33))[1], BLANK_TABLE)

    def test_session_and_staleness_outcomes(self):
        s21 = py_scan(S["scan/s21Sep"]["input"])
        statuses = lambda h, m: {r.status for r in run_refresh(s21, datetime(2026, 9, 21, h, m))[0]}  # noqa: E731
        self.assertNotIn("NO_TRADE_WINDOW", statuses(10, 33))
        for h, m in [(9, 20), (12, 0), (14, 55), (15, 45), (8, 0)]:
            self.assertEqual(statuses(h, m), {"NO_TRADE_WINDOW"})
        stale = run_refresh(py_scan(S["scan/staleCandles"]["input"]), datetime(2026, 9, 21, 10, 33))[0]
        self.assertEqual({r.status for r in stale}, {"STALE"})


if __name__ == "__main__":
    unittest.main()
