"""Live Market Feed codec and collector (offline, fake socket)."""

import asyncio
import json
import struct
from datetime import UTC, datetime

import pytest

from doi.core.errors import SchemaError, TokenRejected
from doi.data.dhan.collector import MarketFeedCollector
from doi.data.dhan.feed import MAX_PER_CONNECTION, REQ_FULL, decode, feed_url, subscribe_messages, to_quote, unsubscribe_messages


def full_packet(sid=50001, seg=2, ltp=100.0, bid=99.5, ask=100.0, bq=750, aq=600, oi=50000, vol=12345):
    body = struct.pack("<fHIfIIIIIIffff", ltp, 75, 1762054500, 99.8, vol, 1000, 2000, oi, oi + 10, oi - 10, 98, 97, 101, 95)
    depth = struct.pack("<IIHHff", bq, aq, 3, 4, bid, ask) + b"".join(
        struct.pack("<IIHHff", 1, 1, 1, 1, bid - i, ask + i) for i in range(1, 5)
    )
    pkt = body + depth
    return struct.pack("<BHBI", 8, 8 + len(pkt), seg, sid) + pkt


def disconnect(code):
    return struct.pack("<BHBIH", 50, 10, 2, 0, code)


def test_subscribe_batches_of_100_and_unsubscribe_codes():
    inst = [("NSE_FNO", str(i)) for i in range(250)]
    msgs = [json.loads(m) for m in subscribe_messages(inst)]
    assert [m["InstrumentCount"] for m in msgs] == [100, 100, 50] and msgs[0]["RequestCode"] == REQ_FULL
    assert msgs[0]["InstrumentList"][0] == {"ExchangeSegment": "NSE_FNO", "SecurityId": "0"}
    assert json.loads(unsubscribe_messages(inst[:1])[0])["RequestCode"] == REQ_FULL + 1
    with pytest.raises(ValueError):
        subscribe_messages([("NSE_FNO", "1")] * (MAX_PER_CONNECTION + 1))
    with pytest.raises(ValueError):
        subscribe_messages([("NASDAQ", "1")])


def test_url_carries_auth_params():
    assert feed_url("cid", "tok") == "wss://api-feed.dhan.co?version=2&token=tok&clientId=cid&authType=2"


def test_full_packet_decodes_top_of_book():
    p = decode(full_packet())
    assert (p.code, p.segment, p.security_id, p.oi, p.volume) == (8, "NSE_FNO", "50001", 50000, 12345)
    q = to_quote(p, datetime(2026, 11, 2, 4, 30, tzinfo=UTC))
    assert (q.bid, q.bid_qty, q.ask, q.ask_qty) == (99.5, 750, 100.0, 600) and q.ltp == 100.0


def test_zero_prices_are_missing_not_quotes():
    q = to_quote(decode(full_packet(bid=0.0, bq=0)), datetime.now(UTC))
    assert q.bid is None and q.bid_qty is None and not q.two_sided


def test_other_packets_and_errors():
    ticker = struct.pack("<BHBIfI", 2, 16, 0, 13, 24010.5, 1762054500)
    assert decode(ticker).ltp == pytest.approx(24010.5) and to_quote(decode(ticker), datetime.now(UTC)) is None
    assert decode(struct.pack("<BHBII", 5, 12, 2, 7, 999)).oi == 999
    assert decode(disconnect(807)).disconnect_code == 807
    with pytest.raises(SchemaError):
        decode(b"\x08\x00")
    with pytest.raises(SchemaError):
        decode(full_packet()[:100])


class FakeWS:
    def __init__(self, frames):
        self.frames, self.sent, self.closed = list(frames), [], False

    async def send(self, m):
        self.sent.append(m)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


def collector(sockets, token=lambda: "tok"):
    got, urls = [], []
    socks = iter(sockets)

    async def connect(url):
        urls.append(url)
        return next(socks)

    async def on_quote(q):
        got.append(q)

    c = MarketFeedCollector("cid", token, on_quote, connect=connect, backoff_base_s=0.001, backoff_cap_s=0.002)
    return c, got, urls


def test_collector_streams_reconnects_and_resubscribes():
    s1 = FakeWS([full_packet(), b"\x01", disconnect(805)])  # bad frame, then non-fatal disconnect
    s2 = FakeWS([full_packet(bid=99.0), disconnect(807)])  # token expired: fatal
    c, got, urls = collector([s1, s2])

    async def go():
        await c.set_subscriptions({("NSE_FNO", "50001")})
        await c.run()

    asyncio.run(go())
    assert [q.bid for q in got] == [99.5, 99.0]
    assert c.status.reconnects == 1 and c.status.decode_errors == 1 and "807" in c.status.fatal
    assert json.loads(s1.sent[0])["InstrumentList"] == json.loads(s2.sent[0])["InstrumentList"]  # replayed
    assert s1.closed and s2.closed and len(urls) == 2


def test_collector_stops_on_missing_token():
    def no_token():
        raise TokenRejected("none")

    c, _, urls = collector([], token=no_token)
    asyncio.run(c.run())
    assert c.status.state == "stopped" and c.status.fatal and urls == []


def test_live_subscription_diff_and_read_only_messages():
    ws = FakeWS([])
    c, _, _ = collector([ws])
    c._ws = ws

    async def go():
        await c.set_subscriptions({("NSE_FNO", "1"), ("BSE_FNO", "2")})
        await c.set_subscriptions({("BSE_FNO", "2")})
        await c.stop()

    asyncio.run(go())
    codes = [json.loads(m)["RequestCode"] for m in ws.sent]
    assert codes == [21, 22, 12]  # subscribe, unsubscribe, disconnect: nothing else is ever sent
