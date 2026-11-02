import asyncio
import random
import time
from itertools import pairwise

import pytest

from doi.core.errors import CircuitOpen, RateLimited, TokenRejected, UpstreamError
from doi.data.scheduler.chain_scheduler import ApiRequest, KeyedScheduler, Priority

CD = 0.15


def req(exp="2026-11-10", und=13):
    return ApiRequest("/optionchain", {"UnderlyingScrip": und, "UnderlyingSeg": "IDX_I", "Expiry": exp})


class Recorder:
    def __init__(self, delay=0.0, script=None):
        self.calls: list[tuple[str, float]] = []
        self.delay, self.script = delay, list(script or [])
        self.active = self.peak = 0

    async def __call__(self, r):
        self.calls.append((r.key, time.monotonic()))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.script:
                item = self.script.pop(0)
                if isinstance(item, Exception):
                    raise item
            return {"ok": r.key}
        finally:
            self.active -= 1


def sched(fetch, **kw):
    kw.setdefault("cooldown_s", CD)
    kw.setdefault("backoff_base_s", 0.01)
    kw.setdefault("backoff_cap_s", 0.02)
    return KeyedScheduler(fetch, rng=random.Random(1), **kw)


def test_key_is_order_independent():
    a = ApiRequest("/optionchain", {"a": 1, "b": 2})
    b = ApiRequest("/optionchain", {"b": 2, "a": 1})
    assert a.key == b.key and a.key != req().key


def test_same_key_respects_cooldown():
    rec = Recorder()
    s = sched(rec)

    async def go():
        for _ in range(3):
            await s.get(req())

    asyncio.run(go())
    t = [c[1] for c in rec.calls]
    assert len(t) == 3 and all(b - a >= CD * 0.95 for a, b in pairwise(t))


def test_distinct_keys_run_concurrently_not_global_lock():
    rec = Recorder(delay=0.1)
    s = sched(rec, max_concurrency=4)

    async def go():
        start = time.monotonic()
        await asyncio.gather(*(s.get(req(und=u)) for u in (13, 25, 27, 51)))
        return time.monotonic() - start

    elapsed = asyncio.run(go())
    assert rec.peak == 4 and elapsed < 0.35


def test_concurrency_bounded():
    rec = Recorder(delay=0.05)
    s = sched(rec, max_concurrency=2)

    async def go():
        await asyncio.gather(*(s.get(req(und=u)) for u in range(6)))

    asyncio.run(go())
    assert rec.peak == 2 and len(rec.calls) == 6


def test_duplicate_requests_coalesce():
    rec = Recorder(delay=0.05)
    s = sched(rec)

    async def go():
        return await asyncio.gather(*(s.get(req()) for _ in range(10)))

    res = asyncio.run(go())
    assert len(rec.calls) == 1 and len({id(r) for r in res}) == 1 and s.metrics.coalesced == 9


def test_cache_served_within_max_age_only():
    rec = Recorder()
    s = sched(rec)

    async def go():
        a = await s.get(req())
        b = await s.get(req(), max_age_s=5)
        c = await s.get(req())  # no max_age -> fresh request (after cooldown)
        return a, b, c

    a, b, c = asyncio.run(go())
    assert not a.from_cache and b.from_cache and b.received_at == a.received_at and not c.from_cache
    assert len(rec.calls) == 2


def test_retry_on_rate_limit_then_success():
    rec = Recorder(script=[RateLimited("429", retry_after=0.01), UpstreamError("503")])
    s = sched(rec, cooldown_s=0.02)
    res = asyncio.run(s.get(req()))
    assert res.attempts == 3 and s.metrics.retries == 2 and s.metrics.rate_limited == 1
    t = [c[1] for c in rec.calls]
    assert all(b - a >= 0.02 * 0.95 for a, b in pairwise(t))  # retries also respect per-key cooldown


def test_token_error_not_retried():
    rec = Recorder(script=[TokenRejected("expired")])
    s = sched(rec)
    with pytest.raises(TokenRejected):
        asyncio.run(s.get(req()))
    assert len(rec.calls) == 1


def test_retries_exhausted_raises():
    rec = Recorder(script=[UpstreamError("x")] * 5)
    s = sched(rec, cooldown_s=0.0, max_retries=2, breaker_threshold=10)
    with pytest.raises(UpstreamError):
        asyncio.run(s.get(req()))
    assert len(rec.calls) == 3


def test_circuit_breaker_opens_and_recovers():
    clock = [0.0]
    rec = Recorder(script=[UpstreamError("x")] * 3)
    s = sched(rec, cooldown_s=0.0, max_retries=0, breaker_threshold=3, breaker_reset_s=30, clock=lambda: clock[0])

    async def go():
        for u in range(3):
            with pytest.raises(UpstreamError):
                await s.get(req(und=u))
        with pytest.raises(CircuitOpen):
            await s.get(req(und=99))
        clock[0] = 31.0
        return await s.get(req(und=99))  # half-open trial succeeds

    assert asyncio.run(go()).value
    assert s.breaker.state == "closed" and s.metrics.circuit_rejections == 1


def test_priority_order_when_saturated():
    order = []

    async def go():
        release = asyncio.Event()

        async def fetch(r):
            order.append(r.params["UnderlyingScrip"])
            if r.params["UnderlyingScrip"] == 0:
                await release.wait()
            return 1

        s = sched(fetch, max_concurrency=1)
        first = asyncio.ensure_future(s.get(req(und=0)))
        await asyncio.sleep(0.01)
        bg = asyncio.ensure_future(s.get(req(und=1), priority=Priority.BACKGROUND))
        await asyncio.sleep(0.01)
        live = asyncio.ensure_future(s.get(req(und=2), priority=Priority.LIVE))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, bg, live)

    asyncio.run(go())
    assert order == [0, 2, 1]
