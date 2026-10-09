"""The comparator must actually detect differences (guards against vacuous parity)."""

import unittest

from parity_support import compare
from sensex.jscompat import UNDEFINED


class ComparatorDetects(unittest.TestCase):
    def test_within_tolerance_passes(self):
        self.assertEqual(compare(1.0 + 5e-10, 1.0), [])
        self.assertEqual(compare(float("nan"), float("nan")), [])
        self.assertEqual(compare(74100, 74100.0), [])

    def test_outside_tolerance_fails(self):
        self.assertTrue(compare(74616.5746, 74616.5745))
        self.assertTrue(compare(1e-6, 0.0))
        self.assertTrue(compare(float("nan"), 1.0))
        self.assertTrue(compare(float("inf"), 1e308))

    def test_structure_and_types(self):
        self.assertTrue(compare({"a": 1}, {"a": 1, "b": 2}))
        self.assertTrue(compare([1, 2], [1, 2, 3]))
        self.assertTrue(compare(None, 0))
        self.assertTrue(compare(True, 1))
        self.assertTrue(compare("block", "warn"))
        self.assertTrue(compare(None, UNDEFINED))
        self.assertEqual(compare(UNDEFINED, UNDEFINED), [])

    def test_errors(self):
        ts = {"$error": {"name": "GateBlockedError", "message": "m", "reasons": ["r"]}}
        self.assertTrue(compare({"forward": 1}, ts))
        self.assertTrue(compare({"$error": {"name": "Error", "message": "m", "reasons": ["r"]}}, ts))
        self.assertEqual(compare({"$error": {"name": "GateBlockedError", "message": "m", "reasons": ["r"]}}, ts), [])


if __name__ == "__main__":
    unittest.main()
