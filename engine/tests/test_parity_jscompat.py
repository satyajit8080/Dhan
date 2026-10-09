"""Parity: sensex.jscompat vs real JavaScript semantics (parity/fixtures/jscompat.json)."""

import unittest

from parity_support import ParityMixin, load
from sensex.jscompat import date_utc, iso_of, js_max, js_min, js_number, js_round, js_str, object_keys, to_fixed

J = load("jscompat")


class JsCompatParity(ParityMixin, unittest.TestCase):
    def test_string(self):
        c = J["String"]
        self.assertParity([js_str(float(x)) for x in c["input"]], c["expected"], "String")

    def test_to_fixed(self):
        c = J["toFixed"]
        self.assertParity([to_fixed(float(x), d) for x, d in c["input"]], c["expected"], "toFixed")

    def test_round(self):
        c = J["Math.round"]
        self.assertParity([js_round(float(x)) for x in c["input"]], c["expected"], "Math.round")

    def test_number(self):
        c = J["Number"]
        self.assertParity([js_number(x) for x in c["input"]], c["expected"], "Number")

    def test_object_keys(self):
        d = {"b": 1, "10": 2, "2": 3, "a": 4, "01": 5, "4294967295": 6, "4294967294": 7, "-1": 8, "1.5": 9}
        self.assertParity(object_keys(d), J["Object.keys"]["expected"], "Object.keys")

    def test_date_utc_and_iso(self):
        c = J["Date.UTC"]
        self.assertParity([date_utc(*d) for d in c["input"]], c["expected"], "Date.UTC")
        c = J["toISOString"]
        self.assertParity([iso_of(x) for x in c["input"]], c["expected"], "toISOString")

    def test_max_min(self):
        nan = float("nan")
        self.assertParity([js_max(0, nan), js_min(1, nan), js_max(), js_min(), js_max(-0.0, 0)],
                          J["Math.max/min"]["expected"], "Math.max/min")


if __name__ == "__main__":
    unittest.main()
