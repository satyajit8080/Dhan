"""Read-only HTTP transport for the Dhan v2 Data API.

Safety boundary: only paths in READ_ONLY_ENDPOINTS can be reached, and the class
exposes no method other than `post` for those paths. There is no order, position,
funds or margin capability anywhere in this package. The official SDK object is
deliberately not instantiated in the runtime because it carries order methods.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from doi.core.errors import (
    DhanRequestError,
    ForbiddenEndpoint,
    RateLimited,
    TokenRejected,
    UpstreamError,
    UpstreamTimeout,
)

READ_ONLY_ENDPOINTS: frozenset[str] = frozenset(
    {
        "/optionchain",
        "/optionchain/expirylist",
        "/charts/intraday",
        "/charts/historical",
        "/charts/rollingoption",
        "/marketfeed/ltp",
        "/marketfeed/ohlc",
        "/marketfeed/quote",
    }
)

_TOKEN_CODES = {"DH-901"}
_RATE_CODES = {"DH-904"}
_UPSTREAM_CODES = {"DH-908", "DH-909"}


def _error_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for src in (
        payload,
        payload.get("remarks") if isinstance(payload.get("remarks"), dict) else {},
        payload.get("data") if isinstance(payload.get("data"), dict) else {},
    ):
        for k in ("errorCode", "error_code"):
            v = src.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


class DhanTransport:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        token: Callable[[], str],
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._token = token
        self._client = client or httpx.AsyncClient()
        self._timeout = timeout_s

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        if path not in READ_ONLY_ENDPOINTS:
            raise ForbiddenEndpoint(f"path not in read-only allowlist: {path}")
        headers = {
            "access-token": self._token(),
            "client-id": self._client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = await self._client.post(self._base + path, json=body, headers=headers, timeout=self._timeout)
        except httpx.TimeoutException:
            raise UpstreamTimeout(f"{path} timed out") from None
        except httpx.TransportError as e:
            raise UpstreamError(f"{path} transport failure: {type(e).__name__}") from None

        try:
            payload = resp.json()
        except ValueError:
            payload = None
        code = _error_code(payload)
        status = resp.status_code
        msg = f"{path} -> HTTP {status}" + (f" {code}" if code else "")

        if status == 429 or code in _RATE_CODES:
            raise RateLimited(msg, retry_after=_retry_after(resp), status=status, code=code)
        if status in (401, 403) or code in _TOKEN_CODES:
            raise TokenRejected(msg, status=status, code=code)
        if status >= 500 or code in _UPSTREAM_CODES:
            raise UpstreamError(msg, status=status, code=code)
        if status >= 400 or code or (isinstance(payload, dict) and payload.get("status") == "failure"):
            raise DhanRequestError(msg, status=status, code=code)
        if payload is None:
            raise UpstreamError(f"{path} returned non-JSON body", status=status)
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()
