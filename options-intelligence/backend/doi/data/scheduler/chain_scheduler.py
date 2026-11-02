"""Request-key scheduler for Dhan rate-limited endpoints.

Dhan allows one *unique* option-chain request every 3 s; distinct underlyings or
expiries may run concurrently. So this enforces a cooldown per request key and a
bounded, prioritised concurrency pool across keys - never a global 3 s lock.

Also: duplicate-request coalescing, short-lived cache, exponential backoff with
full jitter on 429/5xx/timeouts, a circuit breaker, and request timing metadata.
Token errors are never retried. Historical downloads use a separate instance
(separate quota lane)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any, Generic, TypeVar

from doi.core.errors import RETRYABLE, CircuitOpen, RateLimited

T = TypeVar("T")


class Priority(IntEnum):
    LIVE = 0  # near-ATM, current-session chains
    INTERACTIVE = 1
    BACKGROUND = 2


@dataclass(frozen=True, slots=True)
class ApiRequest:
    endpoint: str
    params: Mapping[str, Any]

    @property
    def key(self) -> str:
        return self.endpoint + "|" + json.dumps(dict(self.params), sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class Fetched(Generic[T]):
    value: T
    key: str
    requested_at: datetime
    received_at: datetime
    queue_delay_s: float
    duration_s: float
    attempts: int
    from_cache: bool = False
    cache_age_s: float = 0.0


@dataclass
class SchedulerMetrics:
    requests: int = 0
    cache_hits: int = 0
    coalesced: int = 0
    retries: int = 0
    rate_limited: int = 0
    failures: int = 0
    circuit_rejections: int = 0


class _PriorityGate:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("concurrency limit must be >= 1")
        self._free = limit
        self._heap: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    async def acquire(self, priority: int) -> None:
        if self._free > 0 and not self._heap:
            self._free -= 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release()  # granted, then cancelled: hand the slot on
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)
                return
        self._free += 1


class CircuitBreaker:
    def __init__(self, threshold: int, reset_s: float, clock: Callable[[], float]) -> None:
        self.threshold, self.reset_s, self._clock = threshold, reset_s, clock
        self.state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial = False

    def before(self) -> None:
        if self.state == "open":
            if self._clock() - self._opened_at < self.reset_s:
                raise CircuitOpen(f"circuit open for {self.reset_s - (self._clock() - self._opened_at):.1f}s")
            self.state, self._trial = "half_open", False
        if self.state == "half_open":
            if self._trial:
                raise CircuitOpen("circuit half-open; trial request in flight")
            self._trial = True

    def success(self) -> None:
        self.state, self._failures, self._trial = "closed", 0, False

    def failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self.threshold:
            self.state, self._opened_at, self._trial = "open", self._clock(), False

    def release_trial(self) -> None:
        """Non-health outcome (e.g. token error) during a half-open trial."""
        self._trial = False


@dataclass
class _Cached:
    result: Fetched
    at: float


@dataclass
class KeyedScheduler:
    fetch: Callable[[ApiRequest], Awaitable[Any]]
    cooldown_s: float = 3.0
    max_concurrency: int = 4
    max_retries: int = 3
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 20.0
    breaker_threshold: int = 5
    breaker_reset_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=random.Random)
    metrics: SchedulerMetrics = field(default_factory=SchedulerMetrics)

    def __post_init__(self) -> None:
        self._gate = _PriorityGate(self.max_concurrency)
        self.breaker = CircuitBreaker(self.breaker_threshold, self.breaker_reset_s, self.clock)
        self._next_allowed: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._cache: dict[str, _Cached] = {}

    async def get(self, req: ApiRequest, *, priority: Priority = Priority.LIVE, max_age_s: float | None = None) -> Fetched:
        key = req.key
        cached = self._cache.get(key)
        if cached is not None and max_age_s is not None:
            age = self.clock() - cached.at
            if age <= max_age_s:
                self.metrics.cache_hits += 1
                return replace(cached.result, from_cache=True, cache_age_s=age)
        task = self._inflight.get(key)
        if task is not None:
            self.metrics.coalesced += 1
        else:
            task = asyncio.ensure_future(self._run(req, key, priority))
            self._inflight[key] = task
            task.add_done_callback(lambda t, k=key: self._inflight.pop(k, None) if self._inflight.get(k) is t else None)
        return await asyncio.shield(task)

    def _backoff(self, attempt: int, err: Exception) -> float:
        delay = self.rng.uniform(0, min(self.backoff_cap_s, self.backoff_base_s * 2 ** (attempt - 1)))
        if isinstance(err, RateLimited) and err.retry_after:
            delay = max(delay, err.retry_after)
        return delay

    async def _run(self, req: ApiRequest, key: str, priority: Priority) -> Fetched:
        enqueued = self.clock()
        attempts = 0
        while True:
            # Only one task per key exists (coalescing), so waiting outside the
            # gate cannot race another request for the same key.
            while (wait := self._next_allowed.get(key, 0.0) - self.clock()) > 0:
                await asyncio.sleep(wait)
            try:
                self.breaker.before()
            except CircuitOpen:
                self.metrics.circuit_rejections += 1
                raise
            await self._gate.acquire(priority)
            start = self.clock()
            self._next_allowed[key] = start + self.cooldown_s
            requested_at = datetime.now(UTC)
            attempts += 1
            self.metrics.requests += 1
            err: Exception | None = None
            try:
                value = await self.fetch(req)
            except RETRYABLE as e:
                err = e
            except Exception:
                self.breaker.release_trial()
                self.metrics.failures += 1
                raise
            finally:
                self._gate.release()
            if err is None:
                self.breaker.success()
                result = Fetched(value, key, requested_at, datetime.now(UTC), start - enqueued, self.clock() - start, attempts)
                self._cache[key] = _Cached(result, self.clock())
                return result
            self.breaker.failure()
            if isinstance(err, RateLimited):
                self.metrics.rate_limited += 1
            if attempts > self.max_retries or self.breaker.state == "open":
                self.metrics.failures += 1
                raise err
            self.metrics.retries += 1
            await asyncio.sleep(self._backoff(attempts, err))
