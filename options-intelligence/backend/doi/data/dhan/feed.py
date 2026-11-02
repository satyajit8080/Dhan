"""Dhan Live Market Feed v2: request builders and binary packet decoder.

Layout follows the official DhanHQ-py `marketfeed.py` (commit 8c6583e, read for
reference only, no code copied). Every packet is little-endian with an 8-byte header:
    <B response code><H message length><B exchange segment><I security id>
Full packet (code 8, 162 bytes):
    <f LTP><H LTQ><I LTT><f ATP><I volume><I total sell qty><I total buy qty>
    <I OI><I OI day high><I OI day low><f open><f close><f high><f low>
    then 5 depth levels of <I bid qty><I ask qty><H bid orders><H ask orders><f bid><f ask>
This is market data only. The feed has no order capability.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from datetime import datetime

from doi.core.errors import SchemaError
from doi.core.market import Quote

FEED_URL = "wss://api-feed.dhan.co"
SEGMENTS = {"IDX_I": 0, "NSE_EQ": 1, "NSE_FNO": 2, "NSE_CURRENCY": 3, "BSE_EQ": 4, "MCX_COMM": 5, "BSE_CURRENCY": 7, "BSE_FNO": 8}
SEGMENT_NAMES = {v: k for k, v in SEGMENTS.items()}
REQ_TICKER, REQ_QUOTE, REQ_FULL, REQ_DISCONNECT = 15, 17, 21, 12
MAX_PER_MESSAGE = 100
MAX_PER_CONNECTION = 5000
DISCONNECT_REASONS = {
    805: "too many websocket connections",
    806: "data APIs not subscribed",
    807: "access token expired",
    808: "invalid client id",
    809: "authentication failed",
}
FATAL_DISCONNECTS = frozenset({806, 807, 808, 809})

_HEADER = struct.Struct("<BHBI")
_TICKER = struct.Struct("<fI")
_QUOTE = struct.Struct("<fHIfIIIffff")
_FULL = struct.Struct("<fHIfIIIIIIffff")
_DEPTH = struct.Struct("<IIHHff")
_OI = struct.Struct("<I")
_PREV = struct.Struct("<fI")
_DISC = struct.Struct("<H")


def feed_url(client_id: str, token: str) -> str:
    return f"{FEED_URL}?version=2&token={token}&clientId={client_id}&authType=2"


def subscribe_messages(instruments: list[tuple[str, str]], request_code: int = REQ_FULL) -> list[str]:
    """instruments: (exchange_segment name, security_id). Batches of 100."""
    if len(instruments) > MAX_PER_CONNECTION:
        raise ValueError(f"{len(instruments)} instruments exceed {MAX_PER_CONNECTION} per connection")
    for seg, _ in instruments:
        if seg not in SEGMENTS:
            raise ValueError(f"unknown exchange segment {seg}")
    out = []
    for i in range(0, len(instruments), MAX_PER_MESSAGE):
        batch = instruments[i : i + MAX_PER_MESSAGE]
        out.append(
            json.dumps(
                {
                    "RequestCode": request_code,
                    "InstrumentCount": len(batch),
                    "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(sid)} for s, sid in batch],
                }
            )
        )
    return out


def unsubscribe_messages(instruments: list[tuple[str, str]], request_code: int = REQ_FULL) -> list[str]:
    return subscribe_messages(instruments, request_code + 1)


@dataclass(frozen=True, slots=True)
class DepthLevel:
    bid_qty: int
    ask_qty: int
    bid_orders: int
    ask_orders: int
    bid: float
    ask: float


@dataclass(frozen=True, slots=True)
class FeedPacket:
    code: int
    segment: str
    security_id: str
    ltp: float | None = None
    ltt: int | None = None
    volume: int | None = None
    oi: int | None = None
    prev_close: float | None = None
    depth: tuple[DepthLevel, ...] = ()
    disconnect_code: int | None = None


def _pos(x: float) -> float | None:
    return float(x) if x == x and x > 0 else None  # NaN/0 mean "no price"


def decode(data: bytes) -> FeedPacket:
    if len(data) < _HEADER.size:
        raise SchemaError(f"feed packet too short: {len(data)} bytes")
    code, _length, seg, sid = _HEADER.unpack_from(data, 0)
    base = {"code": code, "segment": SEGMENT_NAMES.get(seg, str(seg)), "security_id": str(sid)}
    body = 8

    def need(n: int) -> None:
        if len(data) < body + n:
            raise SchemaError(f"feed packet code {code} truncated: {len(data)} bytes")

    if code == 2:
        need(_TICKER.size)
        ltp, ltt = _TICKER.unpack_from(data, body)
        return FeedPacket(**base, ltp=_pos(ltp), ltt=ltt)
    if code == 4:
        need(_QUOTE.size)
        ltp, _ltq, ltt, _atp, vol, *_ = _QUOTE.unpack_from(data, body)
        return FeedPacket(**base, ltp=_pos(ltp), ltt=ltt, volume=vol)
    if code == 5:
        need(_OI.size)
        return FeedPacket(**base, oi=_OI.unpack_from(data, body)[0])
    if code == 6:
        need(_PREV.size)
        pc, poi = _PREV.unpack_from(data, body)
        return FeedPacket(**base, prev_close=_pos(pc), oi=poi)
    if code == 8:
        need(_FULL.size + 5 * _DEPTH.size)
        ltp, _ltq, ltt, _atp, vol, _tsq, _tbq, oi, *_ = _FULL.unpack_from(data, body)
        off = body + _FULL.size
        depth = tuple(DepthLevel(*_DEPTH.unpack_from(data, off + i * _DEPTH.size)) for i in range(5))
        return FeedPacket(**base, ltp=_pos(ltp), ltt=ltt, volume=vol, oi=oi, depth=depth)
    if code == 50:
        need(_DISC.size)
        return FeedPacket(**base, disconnect_code=_DISC.unpack_from(data, body)[0])
    return FeedPacket(**base)  # market status / other: header only


def to_quote(p: FeedPacket, received_at: datetime, underlying_ltp: float | None = None) -> Quote | None:
    """Only full packets carry the bid/ask a paper fill needs."""
    if p.code != 8 or not p.depth:
        return None
    top = p.depth[0]
    return Quote(
        security_id=p.security_id,
        ts=received_at,
        bid=_pos(top.bid),
        bid_qty=top.bid_qty or None,
        ask=_pos(top.ask),
        ask_qty=top.ask_qty or None,
        ltp=p.ltp,
        underlying_ltp=underlying_ltp,
        oi=p.oi,
        volume=p.volume,
    )
