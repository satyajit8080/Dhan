import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
"""Phase 6 observation record: per-endpoint outcomes, gate details, skip
reasons and session labels. The scan result itself must be unchanged."""

import json
import unittest
from datetime import datetime, timezone

from sensex.dhan_client import RawResponse
from test_scanner import scanner_for


class Observation(unittest.TestCase):
    def test_observation_does_not_change_the_scan(self):
        a = scanner_for("s21Sep")[0].run_once()
        b = scanner_for("s21Sep", cfg_over={"observe": False})[0].run_once()
        self.assertNotIn("observation", b)
        self.assertEqual(a["table"], b["table"])
        self.assertEqual(json.dumps(a["scanInputs"], sort_keys=True), json.dumps(b["scanInputs"], sort_keys=True))

    def test_candle_failure_is_recorded_per_endpoint_with_retries(self):
        script = {"/charts/intraday": [RawResponse(502, b"", {}, 1)] * 4}
        rec = scanner_for("s21Sep", script=script)[0].run_once()
        eps = {e["endpoint"]: e for e in rec["observation"]["endpoints"]}
        self.assertTrue(eps["/optionchain"]["ok"])
        self.assertEqual((eps["/charts/intraday"]["ok"], eps["/charts/intraday"]["attempts"],
                          eps["/charts/intraday"]["error"]), (False, 4, "TransientError"))
        self.assertIn("refresh table blank: no candles / candle timestamp (RULES.md §5)", rec["observation"]["skipped"])

    def test_gate_block_records_reasons_and_numbers(self):
        o = scanner_for("gmFuturesDiverge")[0].run_once()["observation"]
        self.assertTrue(o["gate"]["blocked"])
        self.assertAlmostEqual(o["gate"]["details"]["divergenceVsFuture"], 583.4255, places=3)
        self.assertEqual(o["conditions"][0]["outcome"], "BLOCKED")
        self.assertEqual(o["legs"], [])

    def test_leg_rows_and_band(self):
        o = scanner_for("s21Sep")[0].run_once()["observation"]
        self.assertEqual(len(o["legs"]), 8)
        leg = o["legs"][0]
        for k in ("bid", "ask", "spreadPct", "oi", "oiChange", "ivPct", "delta", "gamma", "thetaPerDay", "vegaPerIvPt"):
            self.assertIn(k, leg)
        o1 = scanner_for("s21Sep", cfg_over={"observe_leg_band": 0})[0].run_once()["observation"]
        self.assertEqual({l["strike"] for l in o1["legs"]}, {o1["prices"]["atmStrike"]})

    def test_session_labels(self):
        sc = scanner_for("s21Sep")[0]
        lab = lambda h, m: sc._session_label(datetime(2026, 9, 21, h, m, tzinfo=timezone.utc).astimezone(
            __import__("sensex.session", fromlist=["IST"]).IST))
        self.assertTrue(lab(3, 50).startswith("OPENING"))     # 09:20 IST
        self.assertEqual(lab(4, 30), "MORNING")              # 10:00
        self.assertTrue(lab(6, 30).startswith("MIDDAY"))      # 12:00
        self.assertEqual(lab(8, 0), "AFTERNOON")             # 13:30
        self.assertTrue(lab(9, 30).startswith("CLOSING"))     # 15:00

    def test_no_credentials_in_observation(self):
        sc, _, _, out = scanner_for("s21Sep")
        sc.run_once()
        self.assertNotIn("mock-token-not-a-real-credential", out.getvalue())
        self.assertNotIn("MOCKCLIENT", out.getvalue())


if __name__ == "__main__":
    unittest.main()
