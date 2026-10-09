"""Parity: sensex.pricing_bridge + sensex.liquidity vs TS pricingBridge.ts / liquidity.ts."""

import unittest

from parity_support import ParityMixin, load, run
from sensex.liquidity import assess_depth, screen_chain, walk_book
from sensex.normalize import normalize_chain, normalize_quote
from sensex.pricing import ist_to_epoch_ms
from sensex.pricing_bridge import attach_pricing, fair_price

B = load("bridge")
L = load("liquidity")
GM_AS_OF = ist_to_epoch_ms("2026-09-18T15:30:00")
OPT = {"atmHint": "atm_hint", "bandPct": "band_pct", "nowMs": "now_ms", "futuresExpiry": "futures_expiry",
       "thresholds": "thresholds"}


class BridgeParity(ParityMixin, unittest.TestCase):
    def test_attach_pricing(self):
        ids = [k for k in B if k.startswith("attachPricing/")]
        self.assertEqual(len(ids), 20)
        for cid in ids:
            b = B[cid]["input"]
            ch = normalize_chain(b["chain"], "SENSEX", 51, "IDX_I", b["expiry"], b.get("chainFetchId", "chain"),
                                 b["chainMs"], "/optionchain")
            fq = None if b["fut"] is None else normalize_quote(
                b["fut"], "FUT", "BSE_FNO", b.get("futFetchId", "futures"), b["futMs"], "/marketfeed/quote")
            kw = {OPT[k]: v for k, v in b["opts"].items()}
            self.assertParity(run(lambda: attach_pricing(ch, fq, 0.065, 3000, **kw)), B[cid]["expected"], cid)

    def test_fair_price(self):
        for cid in [k for k in B if k.startswith("fairPrice/")]:
            self.assertParity(fair_price(B[cid]["input"]), B[cid]["expected"], cid)


class LiquidityParity(ParityMixin, unittest.TestCase):
    def test_assess_depth(self):
        ids = [k for k in L if k.startswith("assessDepth/")]
        self.assertEqual(len(ids), 24)
        for cid in ids:
            i = L[cid]["input"]
            q = normalize_quote(i["raw"], "900001", "BSE_FNO", "d", GM_AS_OF, "/marketfeed/quote")
            self.assertParity(assess_depth(q, i["lots"], i["lotSize"]), L[cid]["expected"], cid)

    def test_walk_book(self):
        lv = lambda p, q: {"price": p, "quantity": q, "orders": 1}  # noqa: E731
        got = [walk_book([lv(10, 5), lv(0, 5), lv(11, 10)], 12), walk_book([], 5), walk_book([lv(10, 5)], 0)]
        self.assertParity(got, L["walkBook/basic"]["expected"], "walkBook/basic")

    def test_screen_chain(self):
        keys = {"maxQuotedSpreadPct": "max_quoted_spread_pct", "minOi": "min_oi", "minTopQuantity": "min_top_quantity",
                "bandPct": "band_pct", "reference": "reference", "maxCandidates": "max_candidates"}
        ids = [k for k in L if k.startswith("screenChain/")]
        self.assertEqual(len(ids), 7)
        for cid in ids:
            i = L[cid]["input"]
            ch = normalize_chain(i["raw"], "SENSEX", 51, "IDX_I", "2026-09-24", "chain", GM_AS_OF, "/optionchain")
            kw = {keys[k]: v for k, v in i["opts"].items()}
            self.assertParity(screen_chain(ch["strikes"], **kw), L[cid]["expected"], cid)


if __name__ == "__main__":
    unittest.main()
