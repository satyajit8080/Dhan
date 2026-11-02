"""Convert Dhan payloads to canonical models. All Dhan field names stay here."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, Literal

from doi.core.errors import SchemaError
from doi.core.models import Bar, ChainRow, OptionBar, OptionChainSnapshot, OptionLeg


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v: Any) -> float | None:
    x = _f(v)
    return x if x is not None and x > 0 else None


def _i(v: Any) -> int | None:
    x = _f(v)
    return None if x is None else int(x)


def _data(payload: Any) -> Any:
    if not isinstance(payload, Mapping) or "data" not in payload:
        raise SchemaError("payload missing 'data'")
    return payload["data"]


def _leg(raw: Any) -> OptionLeg | None:
    if not isinstance(raw, Mapping) or not raw:
        return None
    g = raw.get("greeks") if isinstance(raw.get("greeks"), Mapping) else {}
    sid = raw.get("security_id")
    return OptionLeg(
        security_id=None if sid in (None, "") else str(sid),
        ltp=_f(raw.get("last_price")),
        oi=_i(raw.get("oi")),
        prev_oi=_i(raw.get("previous_oi")),
        volume=_i(raw.get("volume")),
        iv=_pos(raw.get("implied_volatility")),  # vendor 0 means "not available"
        delta=_f(g.get("delta")),
        gamma=_f(g.get("gamma")),
        theta=_f(g.get("theta")),
        vega=_f(g.get("vega")),
        bid=_pos(raw.get("top_bid_price")),  # 0 bid/ask = no quote, never a price
        bid_qty=_i(raw.get("top_bid_quantity")),
        ask=_pos(raw.get("top_ask_price")),
        ask_qty=_i(raw.get("top_ask_quantity")),
    )


def normalize_option_chain(
    payload: Any,
    *,
    underlying_key: str,
    expiry: date,
    request_key: str,
    requested_at: datetime,
    received_at: datetime,
    master_version: str,
) -> OptionChainSnapshot:
    data = _data(payload)
    oc = data.get("oc") if isinstance(data, Mapping) else None
    if not isinstance(oc, Mapping):
        raise SchemaError("option chain missing 'oc'")
    rows = []
    for k, v in oc.items():
        strike = _f(k)
        if strike is None or strike <= 0 or not isinstance(v, Mapping):
            raise SchemaError(f"bad option-chain strike entry: {k!r}")
        rows.append(ChainRow(strike=strike, ce=_leg(v.get("ce")), pe=_leg(v.get("pe"))))
    rows.sort(key=lambda r: r.strike)
    return OptionChainSnapshot(
        underlying_key=underlying_key,
        expiry=expiry,
        request_key=request_key,
        requested_at=requested_at,
        received_at=received_at,
        underlying_ltp=_pos(data.get("last_price")),
        rows=tuple(rows),
        master_version=master_version,
    )


def normalize_expiry_list(payload: Any) -> tuple[date, ...]:
    data = _data(payload)
    if not isinstance(data, list):
        raise SchemaError("expiry list 'data' is not a list")
    try:
        return tuple(sorted({date.fromisoformat(str(d)[:10]) for d in data}))
    except ValueError as e:
        raise SchemaError(f"bad expiry date: {e}") from None


def _ts(v: Any) -> datetime:
    x = _f(v)
    if x is None:
        raise SchemaError(f"bad timestamp {v!r}")
    return datetime.fromtimestamp(x, tz=UTC)


def _columns(src: Mapping, names: tuple[str, ...]) -> tuple[int, dict[str, list]]:
    ts = src.get("timestamp")
    if not isinstance(ts, list):
        raise SchemaError("series missing 'timestamp' array")
    cols = {"timestamp": ts}
    for n in names:
        arr = src.get(n)
        if arr is None:
            continue
        if not isinstance(arr, list) or len(arr) != len(ts):
            raise SchemaError(f"series column {n!r} length mismatch")
        cols[n] = arr
    for req in ("open", "high", "low", "close"):
        if req not in cols:
            raise SchemaError(f"series missing {req!r}")
    return len(ts), cols


def normalize_candles(payload: Any) -> tuple[Bar, ...]:
    src = payload.get("data", payload) if isinstance(payload, Mapping) else None
    if not isinstance(src, Mapping):
        raise SchemaError("candles payload is not an object")
    n, c = _columns(src, ("open", "high", "low", "close", "volume", "open_interest"))
    g = lambda k, i: _f(c[k][i]) if k in c else None
    out = []
    for i in range(n):
        o, h, lo, cl = g("open", i), g("high", i), g("low", i), g("close", i)
        if None in (o, h, lo, cl):
            raise SchemaError(f"non-finite OHLC at index {i}")
        out.append(Bar(_ts(c["timestamp"][i]), o, h, lo, cl, g("volume", i), g("open_interest", i)))
    return tuple(out)


def normalize_rolling_option(payload: Any, option_type: Literal["CALL", "PUT"]) -> tuple[OptionBar, ...]:
    data = _data(payload)
    if not isinstance(data, Mapping):
        raise SchemaError("rolling-option 'data' is not an object")
    side = data.get("ce" if option_type == "CALL" else "pe")
    if side is None:
        return ()
    if not isinstance(side, Mapping):
        raise SchemaError("rolling-option side is not an object")
    n, c = _columns(side, ("open", "high", "low", "close", "volume", "oi", "iv", "strike", "spot"))
    g = lambda k, i: _f(c[k][i]) if k in c else None
    out = []
    for i in range(n):
        o, h, lo, cl = g("open", i), g("high", i), g("low", i), g("close", i)
        if None in (o, h, lo, cl):
            raise SchemaError(f"non-finite OHLC at index {i}")
        out.append(
            OptionBar(_ts(c["timestamp"][i]), o, h, lo, cl, g("volume", i), g("oi", i), g("iv", i), g("strike", i), g("spot", i))
        )
    return tuple(out)
