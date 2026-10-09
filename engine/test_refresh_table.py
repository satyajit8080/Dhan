"""Regression tests for refresh_table.py. Stdlib only: python3 -m unittest -v"""
import unittest
from datetime import datetime, timezone

from refresh_table import in_skip_window, build_refresh_table, Leg, Level


class SkipWindow(unittest.TestCase):
    def test_documented_windows(self):
        for hh, mm in [(9, 15), (9, 24), (11, 30), (12, 59), (14, 50)]:
            self.assertTrue(in_skip_window(datetime(2026, 9, 21, hh, mm)), (hh, mm))
        for hh, mm in [(9, 25), (10, 33), (11, 29), (13, 0), (14, 49)]:
            self.assertFalse(in_skip_window(datetime(2026, 9, 21, hh, mm)), (hh, mm))

    def test_after_close_and_before_open_are_blocked(self):
        # Pre-fix, 15:45 and 08:00 were treated as tradeable.
        self.assertTrue(in_skip_window(datetime(2026, 9, 21, 15, 45)))
        self.assertTrue(in_skip_window(datetime(2026, 9, 21, 23, 59, 59)))
        self.assertTrue(in_skip_window(datetime(2026, 9, 21, 8, 0)))

    def test_tz_aware_utc_is_converted_to_ist(self):
        # 05:03 UTC == 10:33 IST (tradeable); 09:30 UTC == 15:00 IST (blocked).
        self.assertFalse(in_skip_window(datetime(2026, 9, 21, 5, 3, tzinfo=timezone.utc)))
        self.assertTrue(in_skip_window(datetime(2026, 9, 21, 9, 30, tzinfo=timezone.utc)))


class RefreshTable(unittest.TestCase):
    def _rows(self, now):
        return build_refresh_table(
            forward=74658.8947, T=0.0087825, df=0.9994293, candle_ref_price=74667.55,
            resistances=[Level(74723.5, 12, ["swing_low"])],
            supports=[Level(74595.425, 12, ["swing_low"])],
            atr=36.91, bar_minutes=5,
            legs=[Leg(74700, True, 12.7279, 335, 335.05, 335), Leg(74700, False, 12.7519, 376.3, 376.9, 376.75)],
            snapshot_ms=1789967033611, candles_ms=1789966980000, now=now)

    def test_ok_inside_session(self):
        rows = self._rows(datetime(2026, 9, 21, 10, 33))
        self.assertEqual([r.status for r in rows], ["OK", "OK"])
        ce, pe = rows
        self.assertEqual(ce.trigger, 74730)   # 74723.5 + 5, rounded to 5
        self.assertEqual(pe.trigger, 74590)   # 74595.4 - 5, rounded to 5
        self.assertGreater(ce.target, ce.entry_ask)
        self.assertGreater(pe.target, pe.entry_ask)

    def test_after_close_is_no_trade(self):
        rows = self._rows(datetime(2026, 9, 21, 15, 45))
        self.assertTrue(all(r.status == "NO_TRADE_WINDOW" for r in rows))


if __name__ == "__main__":
    unittest.main()
