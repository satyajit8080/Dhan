"""Parity: sensex.normalize + sensex.integrity vs TS normalize.ts / integrity.ts / historical.ts toCandles."""

import unittest

from parity_support import ParityMixin, load, run
from sensex.integrity import check_chain, check_quote, check_snapshot_skew
from sensex.normalize import normalize_chain, normalize_quote, parse_last_trade_time, to_candles
from sensex.pricing import ist_to_epoch_ms

N = load("normalize")
I = load("integrity")
GM_AS_OF = ist_to_epoch_ms("2026-09-18T15:30:00")  # same constant the TS builder uses


def chain(raw):
    return normalize_chain(raw, "SENSEX", 51, "IDX_I", "2026-09-24", "chain", GM_AS_OF, "/optionchain")


def quote(raw):
    return normalize_quote(raw, "FUT", "BSE_FNO", "futures", GM_AS_OF, "/marketfeed/quote")


class NormalizeParity(ParityMixin, unittest.TestCase):
    def test_chains(self):
        ids = [k for k in N if k.startswith("normalizeChain/")]
        self.assertEqual(len(ids), 10)
        for cid in ids:
            self.assertParity(run(lambda: chain(N[cid]["input"])), N[cid]["expected"], cid)

    def test_quotes(self):
        ids = [k for k in N if k.startswith("normalizeQuote/")]
        self.assertEqual(len(ids), 17)
        for cid in ids:
            self.assertParity(run(lambda: quote(N[cid]["input"])), N[cid]["expected"], cid)

    def test_last_trade_time(self):
        c = N["parseLastTradeTime"]
        self.assertParity([parse_last_trade_time(x) for x in c["input"]], c["expected"], "parseLastTradeTime")

    def test_to_candles(self):
        for cid in [k for k in N if k.startswith("toCandles/")]:
            self.assertParity(run(lambda: to_candles(N[cid]["input"])), N[cid]["expected"], cid)


class IntegrityParity(ParityMixin, unittest.TestCase):
    def test_quotes(self):
        for cid in [k for k in I if k.startswith("checkQuote/") and "/boundary-" not in k]:
            q = quote(I[cid]["input"])
            self.assertParity(run(lambda: check_quote(q, GM_AS_OF + 1000, 15000)), I[cid]["expected"], cid)

    def test_quote_freshness_boundary(self):
        ids = [k for k in I if k.startswith("checkQuote/boundary-")]
        self.assertEqual(len(ids), 3)
        for cid in ids:
            i = I[cid]["input"]
            q = quote(i["raw"])
            self.assertParity(run(lambda: check_quote(q, i["nowMs"], 15000)), I[cid]["expected"], cid)

    def test_chains(self):
        for cid in [k for k in I if k.startswith("checkChain/")]:
            ch = chain(I[cid]["input"])
            self.assertParity(run(lambda: check_chain(ch, GM_AS_OF)), I[cid]["expected"], cid)

    def test_skew(self):
        for cid in [k for k in I if k.startswith("checkSnapshotSkew/")]:
            i = I[cid]["input"]
            self.assertParity(run(lambda: check_snapshot_skew(i["a"], i["b"], i["max"])), I[cid]["expected"], cid)


if __name__ == "__main__":
    unittest.main()
