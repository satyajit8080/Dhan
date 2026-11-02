"""dhan-market-collector: Live Market Feed connection, subscriptions, reconnects
and packet normalisation (blueprint section 4).

- Read-only: the socket only sends subscribe/unsubscribe/disconnect requests.
- Reconnects use exponential backoff with full jitter, and subscriptions are replayed on every connect.
- Auth/subscription disconnects (806-809) stop the collector. It doesn't retry
  with a bad token. Token errors surface in `status`.
- The token appears only in the connect URL. It's never logged, because the URL is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from doi.core.errors import SchemaError, TokenRejected
from doi.core.market import Quote
from doi.data.dhan.feed import (
    DISCONNECT_REASONS,
    FATAL_DISCONNECTS,
    REQ_DISCONNECT,
    decode,
    feed_url,
    subscribe_messages,
    to_quote,
    unsubscribe_messages,
)

log = logging.getLogger(__name__)
Connector = Callable[[str], Awaitable[Any]]  # returns an object with send(), recv()/async-iter, close()


async def _default_connect(url: str):
    import websockets

    return await websockets.connect(url, ping_interval=10, ping_timeout=20, max_size=2**20)


@dataclass
class CollectorStatus:
    state: str = "idle"  # idle / connecting / streaming / backoff / stopped
    connects: int = 0
    reconnects: int = 0
    packets: int = 0
    decode_errors: int = 0
    last_packet_at: datetime | None = None
    last_error: str | None = None
    fatal: str | None = None
    subscribed: int = 0


@dataclass
class MarketFeedCollector:
    client_id: str
    token: Callable[[], str]
    on_quote: Callable[[Quote], Awaitable[None]]
    connect: Connector = _default_connect
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    status: CollectorStatus = field(default_factory=CollectorStatus)

    def __post_init__(self) -> None:
        self._wanted: set[tuple[str, str]] = set()
        self._sent: set[tuple[str, str]] = set()
        self._ws: Any = None
        self._stop = asyncio.Event()

    async def set_subscriptions(self, instruments: set[tuple[str, str]]) -> None:
        """Desired (segment, security_id) set; diffs are applied on the live socket."""
        self._wanted = set(instruments)
        if self._ws is not None:
            await self._sync()

    async def _sync(self) -> None:
        add, drop = sorted(self._wanted - self._sent), sorted(self._sent - self._wanted)
        for msg in subscribe_messages(add) if add else []:
            await self._ws.send(msg)
        for msg in unsubscribe_messages(drop) if drop else []:
            await self._ws.send(msg)
        self._sent = set(self._wanted)
        self.status.subscribed = len(self._sent)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"RequestCode": REQ_DISCONNECT}))
                await self._ws.close()
            except Exception:  # best effort on shutdown
                pass

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            self.status.state = "connecting"
            try:
                url = feed_url(self.client_id, self.token())
            except TokenRejected as e:
                self._fatal(f"token: {e}")
                return
            try:
                self._ws = await self.connect(url)
                self.status.connects += 1
                self._sent = set()
                await self._sync()
                attempt = 0
                self.status.state = "streaming"
                fatal = await self._pump()
                if fatal:
                    self._fatal(fatal)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network/protocol: reconnect
                self.status.last_error = type(e).__name__
            finally:
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            attempt += 1
            self.status.reconnects += 1
            self.status.state = "backoff"
            await asyncio.sleep(self.rng.uniform(0, min(self.backoff_cap_s, self.backoff_base_s * 2 ** (attempt - 1))))
        self.status.state = "stopped"

    def _fatal(self, reason: str) -> None:
        self.status.fatal = reason
        self.status.state = "stopped"
        log.error("market feed stopped: %s", reason)

    async def _pump(self) -> str | None:
        async for raw in self._ws:
            if self._stop.is_set():
                return None
            if isinstance(raw, str):
                continue  # server text frames carry no market data
            try:
                pkt = decode(raw)
            except SchemaError:
                self.status.decode_errors += 1
                continue
            now = self.clock()
            self.status.packets += 1
            self.status.last_packet_at = now
            if pkt.code == 50:
                code = pkt.disconnect_code or 0
                reason = f"server disconnect {code}: {DISCONNECT_REASONS.get(code, 'unknown')}"
                if code in FATAL_DISCONNECTS:
                    return reason
                self.status.last_error = reason
                raise ConnectionError(reason)
            q = to_quote(pkt, now)
            if q is not None:
                await self.on_quote(q)
        raise ConnectionError("feed closed by server")
