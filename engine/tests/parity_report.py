"""Run every parity test and print the comparison statistics used in
docs/PHASE4_PARITY_RESULTS.md:   cd engine && python3 tests/parity_report.py"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parity_support  # noqa: E402

suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_parity_*.py",
                                            top_level_dir=str(Path(__file__).parent))
result = unittest.TextTestRunner(verbosity=0).run(suite)
cases = {p.stem: len(json.loads(p.read_text())["cases"]) for p in sorted(parity_support.FIXTURES.glob("*.json"))}
s = parity_support.STATS
print("fixture cases:", cases, "total", sum(cases.values()))
print("numeric values compared: %d (bit-identical %d, within tolerance %d)"
      % (s["numbers"], s["exact_numbers"], s["numbers"] - s["exact_numbers"]))
print("non-numeric values compared (exact): %d" % s["non_numeric"])
print("max |py - ts| = %.3g at %s; max relative = %.3g" % (s["max_abs"], s["max_where"] or "-", s["max_rel"]))
print("tolerance: abs %.0e + rel %.0e x |ts|" % (parity_support.ABS_TOL, parity_support.REL_TOL))
sys.exit(0 if result.wasSuccessful() else 1)
