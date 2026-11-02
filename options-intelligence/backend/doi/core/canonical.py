"""Canonical JSON used for hashing (ledger, feature vectors, evidence packs).
Decimals become strings, datetimes ISO-8601, keys sorted, no whitespace."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o)}
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"not serialisable: {type(o).__name__}")


def canonical(obj: Any) -> str:
    return json.dumps(obj, default=_default, sort_keys=True, separators=(",", ":"))
