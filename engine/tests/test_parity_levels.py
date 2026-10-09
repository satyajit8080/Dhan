"""Parity: sensex.levels vs TS levels.ts / indicators.ts / structure.ts (parity/fixtures/levels.json)."""

import unittest

from parity_support import ParityMixin, load, run
from sensex.levels import (aggregate_candles, atr, build_trade_plan, collect_candidates, count_touches,
                           derive_levels, find_swing_points, ist_date_of, ist_time_of, opening_range,
                           project_premium, session_vwap, sessions, true_ranges, vwap, wilder_smooth)

LV = load("levels")
CFG = {"confirmationBuffer": "confirmation_buffer", "tolerance": "tolerance", "minTouches": "min_touches",
       "swingLookback": "swing_lookback", "consolidationWindow": "consolidation_window", "roundTo": "round_to"}


def indicators(c):
    return {
        "trueRanges": true_ranges(c), "wilder14": wilder_smooth(true_ranges(c), 14), "atr14": atr(c, 14),
        "atr3": atr(c, 3), "vwap": vwap(c), "sessionVwap": session_vwap(c), "sessions": sessions(c),
        "openingRange15": opening_range(c, 15), "swings2": find_swing_points(c, 2), "swings1": find_swing_points(c, 1),
        "candidates": collect_candidates(c, 2, 20),
        "istDates": [[ist_date_of(k["timestampMs"]), ist_time_of(k["timestampMs"])] for k in c[:3]],
        "agg5": aggregate_candles(c, 5), "agg15": aggregate_candles(c, 15), "agg1": aggregate_candles(c, 1),
    }


class LevelsParity(ParityMixin, unittest.TestCase):
    def test_indicators(self):
        ids = [k for k in LV if k.startswith("indicators/")]
        self.assertEqual(len(ids), 9)
        for cid in ids:
            self.assertParity(run(lambda: indicators(LV[cid]["input"])), LV[cid]["expected"], cid)

    def test_derive_levels(self):
        ids = [k for k in LV if k.startswith("deriveLevels/")]
        self.assertEqual(len(ids), 26)
        for cid in ids:
            i = LV[cid]["input"]
            kw = {CFG[k]: v for k, v in i["cfg"].items()}
            self.assertParity(run(lambda: derive_levels(i["candles"], i["spot"], **kw)), LV[cid]["expected"], cid)

    def test_count_touches(self):
        real = LV["indicators/real18Sep"]["input"]
        got = [count_touches(real, 74600, 5, "resistance"), count_touches(real, 74500, 5, "support"),
               count_touches([], 1, 1, "support")]
        self.assertParity(got, LV["countTouches"]["expected"], "countTouches")

    def test_trade_plans(self):
        ids = [k for k in LV if k.startswith("buildTradePlan/")]
        self.assertEqual(len(ids), 12)
        for cid in ids:
            i = LV[cid]["input"]
            self.assertParity(run(lambda: build_trade_plan(i["side"], i["levels"], i["entry"], i["delta"], i["gamma"])),
                              LV[cid]["expected"], cid)
        got = [project_premium(100, 0.5, 0.0004, 50), project_premium(10, 0.5, 0, -100),
               project_premium(100, None, 0.0004, 50), project_premium(120, -0.45, 0.0004, -60),
               project_premium(float("nan"), 0.5, None, 1)]
        self.assertParity(got, LV["projectPremium"]["expected"], "projectPremium")


if __name__ == "__main__":
    unittest.main()
