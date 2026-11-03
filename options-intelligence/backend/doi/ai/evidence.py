"""Frozen, hashed evidence packs and strict claim validation.

Python computes every number. A pack is built deterministically from a FeatureVector, the
deterministic RiskGate dry run, the paper exposure and other Python outputs.
Each agent sees only the sections its role allows (separate evidence contexts).

Validation rules for agent output:
  1. It must parse into the agent's pydantic schema.
  2. Every claim cites at least one evidence ref ("section.key"), and each ref must
     exist in the sections that agent was shown.
  3. Every number in the output must equal a number in the agent's visible evidence
     (within tolerance), or a simple deterministic derivation of two such numbers
     (difference, ratio or % change), or be a small structural integer (0-10),
     or be a minute horizon named in a visible evidence key (15m, 60m).
     Values below 1,000 may be the integer or one-decimal rounding of an evidence
     value (so IV 26.6 may be cited as 27). Values of a million or more may be
     cited in full or in scientific notation / with an explicit scale
     (for example -3.373e11 or -337 billion). A bare truncated prefix such as
     -337 is not a price and is not accepted. Anything else is an untraceable
     financial claim, and the turn is rejected.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import permutations
from typing import Any

from pydantic import BaseModel, ValidationError

from doi.core.canonical import canonical
from doi.intelligence.feature_engine.engine import FeatureVector

SECTION_FEATURES: dict[str, tuple[str, ...]] = {
    "regime": (
        "trend_regime",
        "vol_regime",
        "atr_pct_percentile",
        "adx",
        "plus_di",
        "minus_di",
        "market_structure",
        "vix",
        "vix_change_pct",
    ),
    "technical": (
        "ema_9",
        "ema_20",
        "ema_50",
        "ema_200",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "roc",
        "atr",
        "atr_pct",
        "bb_width_pct",
        "realized_vol_pct",
        "vwap",
        "vwap_distance_pct",
        "opening_range_high",
        "opening_range_low",
        "opening_range_state",
        "range_expansion",
        "body_ratio",
        "close_location",
    ),
    "oi": (
        "pcr_oi",
        "pcr_oi_all",
        "pcr_volume",
        "pcr_oi_chg_5m",
        "pcr_oi_chg_15m",
        "pcr_oi_chg_60m",
        "pcr_oi_chg_session",
        "pcr_vol_chg_5m",
        "pcr_vol_chg_15m",
        "pcr_vol_chg_60m",
        "pcr_vol_chg_session",
        "put_wall",
        "put_wall_share",
        "put_wall_distance_pct",
        "put_wall_migration_steps",
        "call_wall",
        "call_wall_share",
        "call_wall_distance_pct",
        "call_wall_migration_steps",
        "max_pain",
        "max_pain_distance_pct",
        "max_pain_change",
        "forward",
        "atm_strike",
    ),
    "volatility": (
        "atm_iv_b76",
        "atm_iv_dhan",
        "atm_iv_diff",
        "iv_skew",
        "atm_delta",
        "atm_gamma",
        "atm_theta",
        "atm_vega",
        "gamma_exposure_proxy",
        "time_to_expiry_years",
        "forward",
        "forward_basis",
        "atm_strike",
        "strike_step",
    ),
    "liquidity": ("atm_spread_pct", "atm_min_top_qty", "atm_two_sided_share", "snapshot_age_s", "forward_strikes_used"),
    "time": (
        "minutes_since_open",
        "minutes_to_close",
        "is_first_window",
        "is_last_window",
        "session_open",
        "is_expiry_day",
        "is_day_before_expiry",
        "dte_calendar",
        "dte_trading",
    ),
}


@dataclass(frozen=True)
class EvidencePack:
    id: str
    sha256: str
    as_of: datetime
    underlying_key: str | None
    sections: Mapping[str, dict]

    def view(self, allowed: Iterable[str]) -> dict[str, dict]:
        return {k: self.sections[k] for k in allowed if k in self.sections}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sha256": self.sha256,
            "as_of": self.as_of,
            "underlying_key": self.underlying_key,
            "sections": dict(self.sections),
        }


def build_pack(
    *,
    as_of: datetime,
    underlying_key: str | None,
    event: dict,
    features: FeatureVector | None = None,
    extra: Mapping[str, dict] | None = None,
) -> EvidencePack:
    sections: dict[str, dict] = {"event": json.loads(canonical(event))}
    if features is not None:
        for sec, names in SECTION_FEATURES.items():
            body: dict = {}
            unavailable = {}
            for n in names:
                fv = features.values.get(n)
                if fv is None:
                    continue
                if fv.status in ("OK", "STALE") and fv.value is not None:
                    body[n] = fv.value
                else:
                    unavailable[n] = str(fv.status)
            if unavailable:  # scoped to the section: no cross-section leakage
                body["_unavailable"] = unavailable
            sections[sec] = body
        sections["quality"] = {
            "data_quality_score": features.data_quality_score,
            "coverage_status": features.coverage_status,
            "feature_digest": features.digest,
            "feature_version": features.version_key,
        }
    for k, v in (extra or {}).items():
        sections[k] = json.loads(canonical(v))
    body = canonical({"as_of": as_of, "underlying_key": underlying_key, "sections": sections})
    sha = hashlib.sha256(body.encode()).hexdigest()
    return EvidencePack("EVP-" + sha[:16], sha, as_of, underlying_key, json.loads(body)["sections"])


# ------------------------------------------------------------------ validation

# A cited token, optionally scaled. Bare "m" is minutes (15m), not million.
_CITED = re.compile(
    r"(?<![A-Za-z_])([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?:\s*[eE]([+-]?\d+)|\s+(billion|bn|million|mn|crore|cr|lakh)\b)?"
)
_HORIZON_KEY = re.compile(r"(?:^|_)(\d+)m(?:_|$)")
_SCALE = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "crore": 1e7, "cr": 1e7, "lakh": 1e5}


def _leaves(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def evidence_numbers(view: Mapping[str, dict]) -> list[float]:
    nums = []
    for _, v in _leaves(dict(view)):
        if isinstance(v, bool):
            continue
        if isinstance(v, int | float) and math.isfinite(v):
            nums.append(float(v))
        # Numbers embedded in strings (hashes, dates, ids) are NOT evidence: they would
        # otherwise make arbitrary digits look traceable.
    return nums


def cited_numbers(text: str) -> list[float]:
    """Numbers as written, expanding scientific notation and explicit scale words.

    ``-3.373e11`` and ``-337 billion`` become the full magnitude. ``15m`` stays 15
    (a minute horizon), and a bare ``-337`` stays -337.
    """
    out: list[float] = []
    for match in _CITED.finditer(text):
        val = float(match.group(1).replace(",", ""))
        exp, word = match.group(2), match.group(3)
        if exp is not None:
            val *= 10 ** int(exp)
        elif word:
            val *= _SCALE[word.lower()]
        if math.isfinite(val):
            out.append(val)
    return out


def horizon_minutes(view: Mapping[str, dict]) -> frozenset[float]:
    """Integers encoded in visible keys such as pcr_oi_chg_15m. Not mixed into derivations."""
    found: set[float] = set()
    for body in view.values():
        if not isinstance(body, dict):
            continue
        for key in body:
            found.update(float(n) for n in _HORIZON_KEY.findall(str(key)))
    return frozenset(found)


def evidence_refs(view: Mapping[str, dict]) -> set[str]:
    refs = set()
    for sec, body in view.items():
        refs.add(sec)
        if isinstance(body, dict):
            for k in body:
                refs.add(f"{sec}.{k}")
    return refs


@dataclass
class Traceability:
    base: list[float]
    derived: list[float] = field(default_factory=list)
    horizons: frozenset[float] = field(default_factory=frozenset)

    @classmethod
    def of(cls, view: Mapping[str, dict], max_pairs_base: int = 160) -> Traceability:
        base = sorted(set(round(x, 6) for x in evidence_numbers(view)))
        derived = set()
        pool = base[:max_pairs_base]
        for a, b in permutations(pool, 2):
            derived.add(round(a - b, 6))
            if b != 0:
                derived.add(round(a / b, 6))
                derived.add(round((a - b) / b * 100, 6))
        return cls(base, sorted(derived), horizon_minutes(view))

    def _near(self, x: float) -> bool:
        tol = max(0.01, 0.005 * abs(x))
        for arr in (self.base, self.derived):
            i = bisect_left(arr, x - tol)
            if i < len(arr) and arr[i] <= x + tol:
                return True
        return False

    def _rounded_non_price(self, x: float) -> bool:
        """Nearest integer or tenth of a sub-1000 evidence value, within 2%.

        Index levels and open interest are at least 1,000 and stay on the tight
        tolerance, so a neighbouring strike is not accepted as a rounding.
        """
        if abs(x) >= 1000:
            return False
        for e in self.base:
            if not math.isfinite(e) or e == 0 or abs(e) >= 1000:
                continue
            for rounded in (round(e), round(e, 1)):
                if abs(x - rounded) > 1e-6:
                    continue
                err = abs(rounded - e)
                if err / abs(e) <= 0.02 and err <= max(0.5, 0.02 * abs(e)):
                    return True
        return False

    def traceable(self, x: float) -> bool:
        if float(x).is_integer() and 0 <= x <= 10:
            return True  # structural counts ("2 strikes", "3 sessions")
        if float(x).is_integer() and float(x) in self.horizons:
            return True  # "15m" / "60m" windows named in a visible key
        if self._near(x) or self._rounded_non_price(x):
            return True
        return False

    def abbreviation_hint(self, x: float) -> str | None:
        """How to cite a huge evidence value whose leading digits were truncated.

        The bare prefix is not itself traceable: -337 must not pass as a point move
        just because gamma is about -3.37e11.
        """
        ax = abs(x)
        if ax < 100 or ax >= 1000 or x == 0:
            return None
        for e in self.base:
            if abs(e) < 1e6:
                continue
            k = round(math.log10(abs(e) / ax))
            if k < 3:
                continue
            scaled = x * 10**k
            if abs(scaled - e) <= max(0.01, 0.005 * abs(e)):
                return f"if this abbreviates {e:.6g}, quote it as {e:.4e} or the full value, not {x:g}"
        return None


EXEMPT_FIELDS = frozenset({"confidence", "horizon_min", "evidence_refs"})


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: tuple[str, ...]
    output: BaseModel | None


def validate_output(
    raw: dict | None, schema: type[BaseModel], view: Mapping[str, dict], trace: Traceability | None = None
) -> ValidationResult:
    if raw is None:
        return ValidationResult(False, ("no JSON object in response",), None)
    try:
        out = schema.model_validate(raw)
    except ValidationError as e:
        return ValidationResult(False, tuple(f"schema: {x['loc']}: {x['msg']}" for x in e.errors()[:6]), None)
    errors: list[str] = []
    refs = evidence_refs(view)
    data = out.model_dump(mode="json")
    for path, v in _leaves(data):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if leaf == "evidence_refs" or ".evidence_refs[" in path or path.startswith("evidence_refs"):
            if isinstance(v, str) and v not in refs:
                errors.append(f"unknown evidence ref {v!r} at {path}")
    trace = trace or Traceability.of(view)
    for path, v in _leaves(data):
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if leaf in EXEMPT_FIELDS or "evidence_refs" in path:
            continue
        nums: list[float] = []
        if isinstance(v, bool):
            continue
        if isinstance(v, int | float):
            nums = [float(v)]
        elif isinstance(v, str):
            nums = cited_numbers(v)
        for x in nums:
            if not trace.traceable(x):
                hint = trace.abbreviation_hint(x)
                errors.append(f"untraceable number {x:g} at {path}" + (f"; {hint}" if hint else ""))
    return ValidationResult(not errors, tuple(errors[:10]), out if not errors else None)
