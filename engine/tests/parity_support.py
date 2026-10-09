"""
Shared helpers for the TypeScript-parity tests.

Fixtures live in parity/fixtures/*.json and are generated ONLY by the
TypeScript implementation (server/test/parity/builder.ts); a vitest test
fails if they drift from a fresh TS run. Nothing here computes an expected
value.

Comparison rules (documented in docs/PHASE4_PARITY_RESULTS.md):
  * str, bool, null, undefined, dict keys, list lengths: EXACT.
  * numbers: equal, or |py - ts| <= ABS_TOL + REL_TOL * |ts|.
    NaN matches NaN, ±Infinity match exactly.
  * errors: same TS error name, same message, same reasons (exact).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from sensex.errors import GateBlockedError, PricingValidationError, SnapshotSkewError
from sensex.expiries import ExpirySelectionError
from sensex.jscompat import UNDEFINED, is_number

ABS_TOL = 1e-9
REL_TOL = 1e-9

FIXTURES = Path(__file__).resolve().parents[2] / "parity" / "fixtures"

# Global statistics, read by tests/parity_report.py.
STATS = {"numbers": 0, "exact_numbers": 0, "max_abs": 0.0, "max_rel": 0.0, "max_where": "",
         "non_numeric": 0}


def _hook(d):
    if set(d) == {"$num"}:
        return float(d["$num"])  # "NaN" / "Infinity" / "-Infinity"
    if set(d) == {"$undefined"}:
        return UNDEFINED
    return d


def load(module: str) -> dict:
    data = json.loads((FIXTURES / ("%s.json" % module)).read_text(), object_hook=_hook)
    return {c["id"]: c for c in data["cases"]}


_TS_NAME = {
    GateBlockedError: "GateBlockedError",
    SnapshotSkewError: "SnapshotSkewError",
    PricingValidationError: "ValidationError",
    ExpirySelectionError: "InstrumentError",
    ValueError: "Error",
}


def run(fn):
    """Call fn; return its value or an {'$error': ...} record shaped like the TS one."""
    try:
        return fn()
    except tuple(_TS_NAME) as e:  # noqa: B030
        name = next(v for k, v in _TS_NAME.items() if isinstance(e, k))
        rec = {"name": name, "message": getattr(e, "message", None) or str(e)}
        if isinstance(e, GateBlockedError):
            rec["reasons"] = e.reasons
        return {"$error": rec}


def compare(py, ts, path="$", out=None):
    """Return a list of mismatch strings (empty = parity)."""
    out = [] if out is None else out
    if isinstance(ts, dict) and "$error" in ts:
        if not (isinstance(py, dict) and "$error" in py):
            out.append("%s: TS raised %s, Python returned a value" % (path, ts["$error"]["name"]))
            return out
        return compare(py["$error"], ts["$error"], path + ".$error", out)
    if is_number(ts) and is_number(py):
        STATS["numbers"] += 1
        if (math.isnan(ts) and math.isnan(py)) or ts == py:
            STATS["exact_numbers"] += 1
            return out
        if math.isinf(ts) or math.isinf(py) or math.isnan(ts) or math.isnan(py):
            out.append("%s: py=%r ts=%r" % (path, py, ts))
            return out
        diff = abs(py - ts)
        rel = diff / abs(ts) if ts != 0 else diff
        if diff > STATS["max_abs"]:
            STATS["max_abs"] = diff
            STATS["max_where"] = path
        STATS["max_rel"] = max(STATS["max_rel"], rel)
        if diff > ABS_TOL + REL_TOL * abs(ts):
            out.append("%s: py=%r ts=%r (diff %.3g)" % (path, py, ts, diff))
        return out
    STATS["non_numeric"] += 1
    if isinstance(ts, dict):
        if not isinstance(py, dict):
            out.append("%s: py=%r is not an object" % (path, type(py).__name__))
            return out
        if set(py) != set(ts):
            out.append("%s: keys differ: only-py=%s only-ts=%s"
                       % (path, sorted(set(py) - set(ts)), sorted(set(ts) - set(py))))
        for k in ts:
            if k in py:
                compare(py[k], ts[k], "%s.%s" % (path, k), out)
        return out
    if isinstance(ts, list):
        if not isinstance(py, (list, tuple)):
            out.append("%s: py=%r is not a list" % (path, type(py).__name__))
            return out
        if len(py) != len(ts):
            out.append("%s: length py=%d ts=%d" % (path, len(py), len(ts)))
        for i, (a, b) in enumerate(zip(py, ts)):
            compare(a, b, "%s[%d]" % (path, i), out)
        return out
    if type(py) is not type(ts) and not (py is None and ts is None):
        if not (isinstance(py, bool) and isinstance(ts, bool)):
            out.append("%s: type py=%s ts=%s (py=%r ts=%r)" % (path, type(py).__name__, type(ts).__name__, py, ts))
            return out
    if py != ts:
        out.append("%s: py=%r ts=%r" % (path, py, ts))
    return out


class ParityMixin:
    """unittest helper: assert parity and print every mismatch on failure."""

    def assertParity(self, py, ts, case_id):  # noqa: N802
        mism = compare(py, ts, case_id)
        if mism:
            self.fail("%d mismatch(es):\n  " % len(mism) + "\n  ".join(mism[:40]))
