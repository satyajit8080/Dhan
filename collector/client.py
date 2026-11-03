"""Read-only HTTP client for Dhan's rolling (expired) options endpoint.

Only POST /v2/charts/rollingoption is ever called. No order endpoints exist here.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from . import config
from .credentials import Credentials, redact

# Dhan error codes (DhanHQ v2 annexure).
AUTH_CODES = {"DH-901", "DH-902"}         # invalid/expired token, no data-API access
RATE_CODES = {"DH-904"}                   # rate limit
NO_DATA_CODES = {"DH-907"}                # no data for the request


class AuthError(RuntimeError):
    """Token rejected. The run stops; everything fetched so far is kept."""


class QuotaExceeded(RuntimeError):
    """Local daily request budget used up. Re-run tomorrow to resume."""


@dataclass
class Result:
    status: str                 # ok | no_data | failed
    http_status: Optional[int]
    payload: dict
    error: str = ""
    attempts: int = 1


# transport(url, body_bytes, headers, timeout) -> (http_status, response_bytes)
Transport = Callable[[str, bytes, dict, float], "tuple[int, bytes]"]


def urllib_transport(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _error_code(payload: dict) -> str:
    for k in ("errorCode", "error_code", "code"):
        v = payload.get(k) if isinstance(payload, dict) else None
        if isinstance(v, str):
            return v
    return ""


def _has_bars(payload: dict) -> bool:
    data = (payload or {}).get("data") or {}
    return any(isinstance(v, dict) and v.get("timestamp") for v in data.values())


class DhanClient:
    def __init__(self, creds: Credentials, rps: float = config.DEFAULT_RPS,
                 daily_quota: int = config.DEFAULT_DAILY_QUOTA,
                 transport: Transport = urllib_transport, base_url: str = config.BASE_URL,
                 sleep: Callable[[float], None] = time.sleep):
        self._creds = creds
        self._gap = 1.0 / rps if rps > 0 else 0.0
        self._quota = daily_quota
        self._transport = transport
        self._url = base_url.rstrip("/") + config.ROLLING_PATH
        self._sleep = sleep
        self._last = 0.0
        self.calls = 0

    def _headers(self) -> dict:
        return {"access-token": self._creds.token, "client-id": self._creds.client_id,
                "Content-Type": "application/json", "Accept": "application/json"}

    def _throttle(self) -> None:
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def rolling_option(self, body: dict) -> Result:
        raw_body = json.dumps(body).encode()
        last_err, status = "", None
        for attempt in range(1, config.MAX_RETRIES + 1):
            if self.calls >= self._quota:
                raise QuotaExceeded(f"daily budget of {self._quota} requests used")
            self._throttle()
            self.calls += 1
            try:
                status, raw = self._transport(self._url, raw_body, self._headers(), 60.0)
            except Exception as e:  # network error, timeout, DNS, reset
                last_err = f"{type(e).__name__}: {redact(str(e))[:200]}"
                self._backoff(attempt)
                continue
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {"_unparsed": redact(raw[:500].decode(errors="replace"))}
            code = _error_code(payload)
            if status in (401, 403) or code in AUTH_CODES:
                raise AuthError(f"Dhan rejected the token (HTTP {status} {code}). "
                                "Refresh DHAN_ACCESS_TOKEN and re-run to resume.")
            if status == 200 and not code:
                return Result("ok" if _has_bars(payload) else "no_data", status, payload,
                              attempts=attempt)
            if code in NO_DATA_CODES:
                return Result("no_data", status, payload, error=code, attempts=attempt)
            last_err = f"HTTP {status} {code} {redact(json.dumps(payload))[:200]}".strip()
            if status == 429 or code in RATE_CODES or (status or 0) >= 500:
                self._backoff(attempt)
                continue
            return Result("failed", status, payload, error=last_err, attempts=attempt)
        return Result("failed", status, {}, error=last_err or "retries exhausted",
                      attempts=config.MAX_RETRIES)

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(60.0, 2.0 ** attempt) + random.uniform(0, 0.5))
