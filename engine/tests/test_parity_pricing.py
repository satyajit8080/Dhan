"""Parity: sensex.pricing + sensex.gate vs TS pricing/*.ts (parity/fixtures/pricing.json)."""

import unittest

from parity_support import ParityMixin, load, run
from sensex.gate import check_gate
from sensex.pricing import (b76_greeks, b76_iv, b76_price, days_to_expiry, discount_factor, expiry_stamp_ms,
                            ist_to_epoch_ms, median, norm_cdf, norm_pdf, parity_forward, per_strike_forward,
                            year_fraction_to_expiry)

CASES = load("pricing")


class PricingParity(ParityMixin, unittest.TestCase):
    def test_norm(self):
        xs = CASES["normCdf/grid"]["input"]
        self.assertParity([norm_cdf(x) for x in xs], CASES["normCdf/grid"]["expected"], "normCdf")
        self.assertParity([norm_pdf(x) for x in xs], CASES["normPdf/grid"]["expected"], "normPdf")

    def test_time(self):
        for cid, c in CASES.items():
            if cid.startswith("expiryStampMs/"):
                self.assertParity(run(lambda: expiry_stamp_ms(c["input"])), c["expected"], cid)
            elif cid.startswith("time/"):
                i = c["input"]
                self.assertParity(run(lambda: {"T": year_fraction_to_expiry(i["nowMs"], i["expiry"]),
                                               "days": days_to_expiry(i["nowMs"], i["expiry"])}), c["expected"], cid)
            elif cid.startswith("istToEpochMs/"):
                self.assertParity(run(lambda: ist_to_epoch_ms(c["input"])), c["expected"], cid)

    def test_black76(self):
        for e in CASES["b76Price/grid"]["expected"]:
            self.assertParity(b76_price(*e["in"]), e["out"], "b76Price%s" % e["in"])
        for e in CASES["b76IV/grid"]["expected"]:
            self.assertParity(b76_iv(*e["in"]), e["out"], "b76IV%s" % e["in"])
        for e in CASES["b76Greeks/grid"]["expected"]:
            self.assertParity(b76_greeks(*e["in"]), e["out"], "b76Greeks%s" % e["in"])
        c = CASES["discountFactor"]
        self.assertParity([discount_factor(r, T) for r, T in c["input"]], c["expected"], "discountFactor")

    def test_forward(self):
        self.assertParity([median(v) for v in CASES["median"]["input"]], CASES["median"]["expected"], "median")
        self.assertParity(run(lambda: median([])), CASES["median/empty"]["expected"], "median/empty")
        c = CASES["perStrikeForward"]
        self.assertParity(per_strike_forward(*c["input"]), c["expected"], "perStrikeForward")
        for cid, c in CASES.items():
            if cid.startswith("parityForward/"):
                i = c["input"]
                bp = i["opts"].get("bandPct", 0.015)
                self.assertParity(run(lambda: parity_forward(i["legs"], i["atmHint"], i["r"], i["T"], band_pct=bp)),
                                  c["expected"], cid)

    def test_gate(self):
        keys = {"parityForward": "parity_forward", "listedFuture": "listed_future",
                "perStrikeSpread": "per_strike_spread", "indexLtp": "index_ltp",
                "slopeWithinTolerance": "slope_within_tolerance", "slopeRelativeError": "slope_relative_error",
                "futureExpiryGapYears": "future_expiry_gap_years", "thresholds": "thresholds"}
        n = 0
        for cid, c in CASES.items():
            if cid.startswith("checkGate/"):
                kw = {keys[k]: v for k, v in c["input"].items()}
                self.assertParity(run(lambda: check_gate(**kw)), c["expected"], cid)
                n += 1
        self.assertEqual(n, 15)

    def test_golden_master_values_independent_of_ts(self):
        """Manually validated 18-Sep values (server/test/fixtures.ts GM.expected)."""
        T = year_fraction_to_expiry(ist_to_epoch_ms("2026-09-18T15:30:00"), "2026-09-24")
        self.assertEqual(T, 0.01643835616438356)
        legs = [{"strike": 74100, "callPrice": 713.6, "putPrice": 208.3},
                {"strike": 74200, "callPrice": 655.0, "putPrice": 236.7},
                {"strike": 74300, "callPrice": 588.0, "putPrice": 270.05},
                {"strike": 74400, "callPrice": 517.1, "putPrice": 303.7},
                {"strike": 74500, "callPrice": 461.55, "putPrice": 345.1}]
        f = parity_forward(legs, 74300, 0.065, T)
        self.assertAlmostEqual(f["forward"], 74616.5745, places=3)
        self.assertAlmostEqual(f["spread"], 12.907, places=3)
        iv = b76_iv(461.55, f["forward"], 74500, T, 0.065, "CE")
        self.assertAlmostEqual(iv * 100, 10.5167, places=3)
        g = b76_greeks(f["forward"], 74500, T, iv, 0.065, "CE")
        self.assertAlmostEqual(g["delta"], 0.54824, places=4)
        self.assertAlmostEqual(g["theta"], -33.0798, places=3)
        self.assertAlmostEqual(g["vega"], 37.838, places=2)


if __name__ == "__main__":
    unittest.main()
