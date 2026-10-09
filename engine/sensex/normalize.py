"""
Raw Dhan v2 JSON -> canonical dicts. Port of server/src/normalize.ts.

Keys of the returned dicts are the TypeScript schema's camelCase names
(types.ts), so outputs compare one-to-one with the TS implementation.
Vendor IV and Greeks are moved into `vendorQuarantined` and are never read by
any pricing function. PURE (no I/O, no clock); the caller supplies fetch id
and receipt time.
"""

from __future__ import annotations

import math
import re

from .jscompat import (UNDEFINED, date_utc, is_js_object, is_number, js_get, js_number, js_str,
                       js_truthy, object_keys)

NEVER_TRADED = "01/01/1980 00:00:00"
IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000
_ASCII_WS = " \t\n\r" + chr(11) + chr(12)   # chr(): no escape sequences (Dhan Cloud scanner)
_LTT = r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})"


def make_provenance(fetch_id: str, epoch_ms: float, endpoint: str) -> dict:
    return {"fetchId": fetch_id, "epochMs": epoch_ms, "source": "dhan-rest-v2", "endpoint": endpoint}


def parse_last_trade_time(raw):
    """'DD/MM/YYYY HH:MM:SS' IST wall clock -> epoch ms; None for the 1980
    'never traded' sentinel or anything unparseable."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s == NEVER_TRADED:
        return None
    m = re.fullmatch(_LTT, s)
    if not m:
        return None
    d, mo, y, h, mi, sec = (int(g) for g in m.groups())
    return date_utc(y, mo - 1, d, h, mi, sec) - IST_OFFSET_MS


def num(v):
    """Finite number, or a numeric string; else None. Booleans are not numbers."""
    if is_number(v) and math.isfinite(v):
        return v
    if isinstance(v, str) and v.strip(_ASCII_WS) != "":
        n = js_number(v)
        if math.isfinite(n):
            return n
    return None


def _depth_side(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for lvl in raw:
        o = lvl if lvl is not None else {}  # `lvl ?? {}`
        out.append({
            "price": _or0(num(js_get(o, "price"))),
            "quantity": _or0(num(js_get(o, "quantity"))),
            "orders": _or0(num(js_get(o, "orders"))),
        })
    return out


def _or0(v):
    return 0 if v is None else v


def _ohlc(raw):
    if not js_truthy(raw) or not is_js_object(raw):
        return None
    o, h, l, c = (num(js_get(raw, k)) for k in ("open", "high", "low", "close"))
    if o is None and h is None and l is None and c is None:
        return None
    return {"open": _or0(o), "high": _or0(h), "low": _or0(l), "close": _or0(c)}


def normalize_quote(raw, security_id: str, segment: str, fetch_id: str, received_at_ms, endpoint: str) -> dict:
    o = raw if raw is not None else {}  # `raw ?? {}`
    g = lambda k: js_get(o, k)  # noqa: E731
    quarantined = {}
    ltp = num(g("last_price"))
    if ltp is None:
        quarantined["last_price"] = g("last_price")
    raw_ltt = g("last_trade_time")
    ltt = parse_last_trade_time(raw_ltt)
    if raw_ltt is not UNDEFINED and ltt is None:
        quarantined["last_trade_time"] = raw_ltt
    depth = None
    rd = g("depth")
    if js_truthy(rd) and is_js_object(rd):
        depth = {"buy": _depth_side(js_get(rd, "buy")), "sell": _depth_side(js_get(rd, "sell"))}
    return {
        "securityId": security_id,
        "segment": segment,
        "ltp": math.nan if ltp is None else ltp,
        "ohlc": _ohlc(g("ohlc")),
        "volume": num(g("volume")),
        "oi": num(g("oi")),
        "oiDayHigh": num(g("oi_day_high")),
        "oiDayLow": num(g("oi_day_low")),
        "averagePrice": num(g("average_price")),
        "buyQuantity": num(g("buy_quantity")),
        "sellQuantity": num(g("sell_quantity")),
        "netChange": num(g("net_change")),
        "upperCircuit": num(g("upper_circuit_limit")),
        "lowerCircuit": num(g("lower_circuit_limit")),
        "lastTradeTimeMs": ltt,
        "depth": depth,
        "provenance": make_provenance(fetch_id, received_at_ms, endpoint),
        "quarantined": quarantined,
    }


def _quarantine_vendor(o) -> dict:
    iv = num(js_get(o, "implied_volatility"))
    g = js_get(o, "greeks")
    reasons = ["Vendor analytics are computed against the index LTP, not the forward."]
    if iv is None:
        reasons.append("IV missing or unparseable.")
    elif iv <= 0:
        reasons.append("IV <= 0.")
    elif iv > 60:
        reasons.append("IV > 60% — implausible for an index option.")
    greeks = None
    if js_truthy(g):  # `const g = o.greeks ?? null; g ? {...} : null`
        greeks = {k: num(js_get(g, k)) for k in ("delta", "gamma", "theta", "vega")}
    return {"impliedVolatility": iv, "greeks": greeks, "reason": " ".join(reasons)}


def _normalize_leg(raw):
    if not js_truthy(raw) or not is_js_object(raw):
        return None
    g = lambda k: js_get(raw, k)  # noqa: E731
    sid = g("security_id")
    return {
        "securityId": None if sid is None or sid is UNDEFINED else js_str(sid),
        "lastPrice": num(g("last_price")),
        "oi": num(g("oi")),
        "previousOi": num(g("previous_oi")),
        "volume": num(g("volume")),
        "previousVolume": num(g("previous_volume")),
        "previousClosePrice": num(g("previous_close_price")),
        "averagePrice": num(g("average_price")),
        "topBidPrice": num(g("top_bid_price")),
        "topBidQuantity": num(g("top_bid_quantity")),
        "topAskPrice": num(g("top_ask_price")),
        "topAskQuantity": num(g("top_ask_quantity")),
        "vendorQuarantined": _quarantine_vendor(raw),
    }


def normalize_chain(raw, underlying: str, underlying_scrip: int, underlying_seg: str, expiry: str,
                    fetch_id: str, received_at_ms, endpoint: str) -> dict:
    o = raw if raw is not None else {}  # `raw ?? {}`
    quarantined = {}
    oc = js_get(o, "oc")
    if oc is None or oc is UNDEFINED:  # `o.oc ?? {}`
        oc = {}
    if isinstance(oc, (list, str)):  # Object.entries(array|string) -> index keys
        oc = {str(i): v for i, v in enumerate(oc)}
    elif not isinstance(oc, dict):  # numbers / booleans: Object.entries(...) is []
        oc = {}
    strikes = []
    for key in object_keys(oc):
        strike = js_number(key)
        if not math.isfinite(strike):
            quarantined["oc." + key] = "unparseable strike key"
            continue
        v = oc[key] if oc[key] is not None else {}  # `value ?? {}`
        strikes.append({
            "strike": strike,
            "ce": _normalize_leg(js_get(v, "ce")),
            "pe": _normalize_leg(js_get(v, "pe")),
        })
    strikes.sort(key=lambda s: s["strike"])
    return {
        "underlying": underlying,
        "underlyingScrip": underlying_scrip,
        "underlyingSeg": underlying_seg,
        "expiry": expiry,
        "underlyingLtpDoNotUseAsSpot": num(js_get(o, "last_price")),
        "strikes": strikes,
        "provenance": make_provenance(fetch_id, received_at_ms, endpoint),
        "quarantined": quarantined,
    }


def to_candles(raw) -> list:
    """Port of endpoints/historical.ts toCandles: zip Dhan's parallel arrays
    (timestamp in epoch SECONDS), drop a ragged tail, sort oldest-first."""
    raw = raw if isinstance(raw, dict) else {}

    def arr(k):
        v = raw.get(k)
        return v if isinstance(v, list) else None

    ts = arr("timestamp") or []
    o, h, l, c = arr("open"), arr("high"), arr("low"), arr("close")
    n = min(len(ts), len(o or []), len(h or []), len(l or []), len(c or []))
    vol, oi = arr("volume"), arr("open_interest")
    out = []
    for i in range(n):
        t = ts[i]
        if not (is_number(t) and math.isfinite(t)):
            continue
        out.append({
            "timestampMs": t * 1000,
            "open": o[i], "high": h[i], "low": l[i], "close": c[i],
            "volume": vol[i] if vol is not None and i < len(vol) and vol[i] is not None else None,
            "openInterest": oi[i] if oi is not None and i < len(oi) and oi[i] is not None else None,
        })
    out.sort(key=lambda x: x["timestampMs"])
    return out
