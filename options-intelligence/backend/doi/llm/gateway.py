"""LLM gateway: the single path from any agent to Grok.

The order of checks on every call never varies:
  kill switch -> AI enabled -> budget-aware model routing (automatic downgrade)
  -> exact-match cache -> atomic budget reservation -> provider call (timeout,
  circuit breaker, bounded concurrency) -> settlement at actual cost -> call log.

Failures never propagate into market data or paper trading. Callers get
`AIUnavailable`, and the meeting engine turns that into NO_ACTION.

The xAI REST shape (OpenAI-compatible POST /v1/chat/completions, Bearer key)
is UNVERIFIED from this environment. The usage/cost field names were checked
against the official xai-proto definitions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import httpx
import psycopg
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from doi.core.canonical import canonical
from doi.core.errors import CircuitOpen, DoiError
from doi.data.scheduler.chain_scheduler import CircuitBreaker
from doi.llm.budget import BudgetExhausted, MemoryBudget, PgBudget
from doi.llm.pricing import (
    SEED_PRICES,
    CostModel,
    ModelPrice,
    Tier,
    TierModels,
    Usage,
    estimate_prompt_tokens,
    parse_usage,
    usd_actual,
    usd_estimate,
)

log = logging.getLogger(__name__)
D = Decimal


class AIUnavailable(DoiError):
    """AI is disabled, the budget is exhausted, the kill switch is on, or the provider failed."""


class ProviderError(DoiError):
    def __init__(self, message: str, *, retryable: bool, billed_unknown: bool = False) -> None:
        super().__init__(message)
        self.retryable, self.billed_unknown = retryable, billed_unknown


class ChatClient(Protocol):
    async def chat(self, *, model: str, messages: list[dict], max_tokens: int, temperature: float) -> dict: ...


class XaiClient:
    def __init__(
        self,
        api_key: SecretStr,
        base_url: str = "https://api.x.ai/v1",
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key, self._base, self._timeout = api_key, base_url.rstrip("/"), timeout_s
        self._client = client or httpx.AsyncClient()

    async def chat(self, *, model: str, messages: list[dict], max_tokens: int, temperature: float) -> dict:
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        try:
            r = await self._client.post(
                f"{self._base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ProviderError("xAI timeout", retryable=True, billed_unknown=True) from None
        except httpx.TransportError as e:
            raise ProviderError(f"xAI transport {type(e).__name__}", retryable=True) from None
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"xAI HTTP {r.status_code}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"xAI HTTP {r.status_code}", retryable=False)
        try:
            return r.json()
        except ValueError:
            raise ProviderError("xAI non-JSON response", retryable=True, billed_unknown=True) from None

    async def list_models(self) -> list[dict]:
        r = await self._client.get(
            f"{self._base}/language-models",
            headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("models", data.get("data", []))


class FakeGrokClient:
    """Offline stand-in. `responder(model, messages)` returns text, or raises ProviderError."""

    def __init__(
        self, responder: Callable[[str, list[dict]], str], usage: Callable[[str, list[dict], str], dict] | None = None
    ) -> None:
        self.responder, self.usage, self.calls = responder, usage, []

    async def chat(self, *, model: str, messages: list[dict], max_tokens: int, temperature: float) -> dict:
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        await asyncio.sleep(0)
        text = self.responder(model, messages)
        prompt = sum(len(m["content"]) for m in messages) // 4
        usage = (
            self.usage(model, messages, text)
            if self.usage
            else {"prompt_tokens": prompt, "completion_tokens": min(max_tokens, len(text) // 4 + 1)}
        )
        return {"model": model, "choices": [{"message": {"role": "assistant", "content": text}}], "usage": usage}


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Automatic downgrade as the month's budget is consumed (utilisation of usable budget)."""

    downgrade_at: float = 0.60  # above this, only `protected_agents` keep the reasoning tier
    all_fast_at: float = 0.80  # above this, every call uses the fast tier
    stop_at: float = 0.97  # above this, no new AI calls at all
    protected_agents: frozenset[str] = frozenset({"cio", "contrarian"})

    def tier(self, agent_id: str | None, preferred: Tier, utilisation: float) -> Tier | None:
        if utilisation >= self.stop_at:
            return None
        if preferred == "fast" or utilisation >= self.all_fast_at:
            return "fast"
        if utilisation >= self.downgrade_at and agent_id not in self.protected_agents:
            return "fast"
        return "reasoning"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    purpose: str
    system: str
    user: str
    preferred_tier: Tier = "fast"
    max_tokens: int = 600
    temperature: float = 0.2
    agent_id: str | None = None
    agent_version: str | None = None
    meeting_id: str | None = None
    cache_ttl_s: int = 6 * 3600


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    tier: Tier
    usage: Usage
    cost_inr: Decimal
    cache_hit: bool
    call_id: str
    attempts: int


class CallLog(Protocol):
    def log(self, row: dict) -> None: ...
    def cache_get(self, sha: str, now: datetime) -> dict | None: ...
    def cache_put(self, sha: str, model: str, response: dict, expires: datetime) -> None: ...


class MemoryCallLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.cache: dict[str, tuple[dict, datetime]] = {}

    def log(self, row: dict) -> None:
        self.rows.append(row)

    def cache_get(self, sha: str, now: datetime) -> dict | None:
        hit = self.cache.get(sha)
        return hit[0] if hit and hit[1] > now else None

    def cache_put(self, sha: str, model: str, response: dict, expires: datetime) -> None:
        self.cache[sha] = (response, expires)


class PgCallLog:
    COLS = ("id", "reservation_id", "agent_id", "agent_version", "meeting_id", "purpose", "model", "tier", "request_sha256", "cache_hit",
            "attempt", "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "cost_usd", "cost_source",
            "fx_inr_per_usd", "cost_inr", "latency_ms", "status", "error")  # fmt: skip

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def log(self, row: dict) -> None:
        with psycopg.connect(self.dsn) as c:
            c.execute(
                f"INSERT INTO llm_calls ({', '.join(self.COLS)}) VALUES ({', '.join(['%s'] * len(self.COLS))})",
                [row.get(k) for k in self.COLS],
            )

    def cache_get(self, sha: str, now: datetime) -> dict | None:
        with psycopg.connect(self.dsn) as c:
            r = c.execute("SELECT response FROM llm_cache WHERE request_sha256 = %s AND expires_at > %s", (sha, now)).fetchone()
        return r[0] if r else None

    def cache_put(self, sha: str, model: str, response: dict, expires: datetime) -> None:
        with psycopg.connect(self.dsn) as c:
            c.execute(
                "INSERT INTO llm_cache (request_sha256, model, response, expires_at) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (request_sha256) DO UPDATE SET response = EXCLUDED.response, expires_at = EXCLUDED.expires_at",
                (sha, model, Jsonb(response), expires),
            )


@dataclass
class LLMGateway:
    client: ChatClient | None
    budget: PgBudget | MemoryBudget
    calls: CallLog = field(default_factory=MemoryCallLog)
    tiers: TierModels = field(default_factory=TierModels)
    prices: dict[str, ModelPrice] = field(default_factory=lambda: dict(SEED_PRICES))
    cost: CostModel = field(default_factory=CostModel)
    routing: RoutingPolicy = field(default_factory=RoutingPolicy)
    kill_switch: Callable[[], bool] = lambda: False
    enabled: bool = True
    max_retries: int = 1
    max_concurrency: int = 4
    breaker_threshold: int = 4
    breaker_reset_s: float = 120.0

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self.breaker = CircuitBreaker(self.breaker_threshold, self.breaker_reset_s, time.monotonic)

    def _refuse(self, req: LLMRequest, status: str, why: str) -> AIUnavailable:
        self.calls.log({"id": "CALL-" + secrets.token_hex(8), "agent_id": req.agent_id, "agent_version": req.agent_version,
                        "meeting_id": req.meeting_id, "purpose": req.purpose, "model": "-", "tier": "-", "request_sha256": "0" * 64,
                        "cache_hit": False, "attempt": 0, "cost_inr": D(0), "status": status, "error": why})  # fmt: skip
        return AIUnavailable(why)

    async def complete(self, req: LLMRequest, now: datetime | None = None) -> LLMResult:
        now = now or datetime.now(UTC)
        if self.kill_switch():
            raise self._refuse(req, "REFUSED_KILL_SWITCH", "kill switch engaged")
        if not self.enabled or self.client is None:
            raise self._refuse(req, "REFUSED_DISABLED", "AI disabled (no API key or switched off)")
        st = await asyncio.to_thread(self.budget.status, now)
        tier = self.routing.tier(req.agent_id, req.preferred_tier, st.utilisation)
        if tier is None or st.frozen:
            raise self._refuse(req, "REFUSED_BUDGET", f"budget stop: utilisation {st.utilisation:.0%}, frozen={st.frozen}")
        model = getattr(self.tiers, tier)
        price = self.prices.get(model)
        if price is None:
            raise self._refuse(req, "REFUSED_NO_PRICE", f"no price for {model}: refusing to call an unpriced model")
        messages = [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}]
        sha = hashlib.sha256(canonical([model, messages, req.max_tokens, req.temperature]).encode()).hexdigest()

        cached = await asyncio.to_thread(self.calls.cache_get, sha, now)
        if cached is not None:
            cid = "CALL-" + secrets.token_hex(8)
            u = parse_usage(cached.get("usage"))
            self.calls.log({"id": cid, "agent_id": req.agent_id, "agent_version": req.agent_version, "meeting_id": req.meeting_id,
                            "purpose": req.purpose, "model": model, "tier": tier, "request_sha256": sha, "cache_hit": True, "attempt": 0,
                            "cost_inr": D(0), "cost_source": "CACHE", "status": "CACHE_HIT"})  # fmt: skip
            return LLMResult(_text(cached), model, tier, u, D(0), True, cid, 0)

        max_out = req.max_tokens + self.cost.allowance(tier)
        est_inr = self.cost.inr(usd_estimate(price, estimate_prompt_tokens(req.system + req.user) + 8, max_out))
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            try:
                self.breaker.before()
            except CircuitOpen as e:
                raise self._refuse(req, "REFUSED_CIRCUIT", str(e)) from None
            try:
                rid = await asyncio.to_thread(
                    self.budget.reserve, est_inr, purpose=req.purpose, agent_id=req.agent_id, meeting_id=req.meeting_id, now=now
                )
            except BudgetExhausted as e:
                raise self._refuse(req, "REFUSED_BUDGET", str(e)) from None
            cid, t0 = "CALL-" + secrets.token_hex(8), time.monotonic()
            base = {"id": cid, "reservation_id": rid, "agent_id": req.agent_id, "agent_version": req.agent_version, "meeting_id": req.meeting_id,
                    "purpose": req.purpose, "model": model, "tier": tier, "request_sha256": sha, "cache_hit": False, "attempt": attempt,
                    "fx_inr_per_usd": self.cost.fx_inr_per_usd}  # fmt: skip
            try:
                async with self._sem:
                    resp = await self.client.chat(
                        model=model, messages=messages, max_tokens=req.max_tokens, temperature=req.temperature
                    )
            except ProviderError as e:
                last_err = e
                self.breaker.failure()
                if e.billed_unknown:  # may have been billed: count the full estimate, never less
                    await asyncio.to_thread(self.budget.settle, rid, est_inr)
                    self.calls.log({**base, "cost_inr": est_inr, "cost_source": "ESTIMATE_ON_FAILURE", "status": "TIMEOUT", "error": str(e),
                                    "latency_ms": int((time.monotonic() - t0) * 1000)})  # fmt: skip
                else:
                    await asyncio.to_thread(self.budget.release, rid)
                    self.calls.log(
                        {
                            **base,
                            "cost_inr": D(0),
                            "status": "ERROR",
                            "error": str(e),
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                        }
                    )
                if not e.retryable:
                    break
                continue
            except Exception as e:  # unknown failure: assume billed, stay conservative
                last_err = e
                self.breaker.failure()
                await asyncio.to_thread(self.budget.settle, rid, est_inr)
                self.calls.log(
                    {
                        **base,
                        "cost_inr": est_inr,
                        "cost_source": "ESTIMATE_ON_FAILURE",
                        "status": "ERROR",
                        "error": type(e).__name__,
                    }
                )
                break
            self.breaker.success()
            usage = parse_usage(resp.get("usage"))
            usd, source = usd_actual(price, usage)
            actual = self.cost.inr(usd)
            await asyncio.to_thread(self.budget.settle, rid, actual)
            self.calls.log({**base, "prompt_tokens": usage.prompt_tokens, "cached_tokens": usage.cached_tokens,
                            "completion_tokens": usage.completion_tokens, "reasoning_tokens": usage.reasoning_tokens, "cost_usd": usd,
                            "cost_source": source, "cost_inr": actual, "latency_ms": int((time.monotonic() - t0) * 1000),
                            "status": "OK"})  # fmt: skip
            await asyncio.to_thread(self.calls.cache_put, sha, model, resp, now + timedelta(seconds=req.cache_ttl_s))
            return LLMResult(_text(resp), resp.get("model", model), tier, usage, actual, False, cid, attempt)
        raise AIUnavailable(f"provider failed: {last_err}")


def _text(resp: dict) -> str:
    try:
        return resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


_JSON = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """First JSON object in the text (models sometimes wrap JSON in prose or code fences)."""
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


AsyncFn = Callable[..., Awaitable[Any]]
