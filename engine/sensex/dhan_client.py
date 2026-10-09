"""
Read-only Dhan v2 REST client (stdlib only).

Why not the official SDK: it pulls in pandas/numpy (a reported dependency
conflict on Dhan Cloud), hides HTTP status inside `remarks`, and its
`generate_token` logs the PIN/TOTP on network errors (Phase 3, tested). The
request shapes below are taken from the SDK source (dhan-oss/DhanHQ-py @
8c6583e) and match the TypeScript client that has been used live.

SAFETY
  * Allow-list: only the exact (method, path) pairs in READ_ONLY_ENDPOINTS can
    be requested. Anything else — orders, positions, kill switch, P&L exit, IP
    whitelist, token renewal, … — is refused before a socket is opened.
  * Credentials are read from the environment or a file on THIS machine, are
    sent only as headers (never in a URL), and are redacted from every log line
    and exception message.
  * No login, no token generation, no token renewal.

Transport, normalisation and calculation stay separate: this module returns
raw payloads plus receipt time; sensex.validation / sensex.normalize / the
engine do the rest.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE_URL = "https://api.dhan.co/v2"

# (method, path) -> rate class. Paths verified in DhanHQ-py source:
#   _option_chain.py  POST /optionchain, POST /optionchain/expirylist
#   _market_feed.py   POST /marketfeed/ltp, /marketfeed/ohlc, /marketfeed/quote
#   _historical_data.py POST /charts/intraday, POST /charts/historical
#   auth.py           GET /profile (read-only token/plan check)
READ_ONLY_ENDPOINTS = {
    ("POST", "/optionchain/expirylist"): "optionchain",
    ("POST", "/optionchain"): "optionchain",
    ("POST", "/marketfeed/quote"): "quote",
    ("POST", "/marketfeed/ltp"): "quote",
    ("POST", "/marketfeed/ohlc"): "quote",
    ("POST", "/charts/intraday"): "data",
    ("POST", "/charts/historical"): "data",
    ("GET", "/profile"): "nontrading",
}

# Defence in depth, mirroring server/src/transport.ts: even a future edit to the
# allow-list cannot add one of these.
FORBIDDEN_FRAGMENTS = ("order", "trade", "super", "forever", "edis", "fund", "margin", "position",
                       "holding", "alert", "kill", "pnlexit", "/ip/", "renewtoken", "globalstocks",
                       "generateaccesstoken", "consent")

# Published Dhan limits (dhanhq-skills error-codes.md / SKILL.md).
RATE_SPECS = {"data": (5, 5.0), "quote": (1, 1.0), "optionchain": (1, 1 / 3), "nontrading": (20, 20.0)}

# Error-code families (DhanHQ v2 annexure, via dhanhq-skills references/error-codes.md).
THROTTLE_CODES = {"DH-904", "805"}
AUTH_CODES = {"DH-901", "807", "808", "809", "810"}
PLAN_CODES = {"DH-902", "806"}
RETRYABLE_CODES = {"800", "DH-908", "DH-909"}


class DhanClientError(Exception):
    retryable = False
    attempts = None
    retry_after_s = None

    def __init__(self, message: str, *, code: str = "", http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class ForbiddenEndpointError(DhanClientError):
    """The request is not on the read-only allow-list."""


class AuthError(DhanClientError):
    """Bad or expired credentials. Not retried: a person must supply a new token."""


class PlanError(DhanClientError):
    """Account lacks Data API access. Not retried."""


class RateLimitedError(DhanClientError):
    retryable = True


class TransientError(DhanClientError):
    """Network fault, timeout, 5xx or a retryable Dhan code."""
    retryable = True


class RequestRejectedError(DhanClientError):
    """4xx for a bad request (e.g. 811 invalid expiry). Not retried."""


class ResponseFormatError(DhanClientError):
    """Not JSON, or not a recognisable envelope."""


# ---------------------------------------------------------------- credentials

@dataclass(frozen=True)
class Credentials:
    client_id: str = field(repr=False)
    access_token: str = field(repr=False)

    def __repr__(self) -> str:  # never show the values, even in a debugger
        return "Credentials(client_id=<redacted>, access_token=<redacted>)"

    def token_expiry_ms(self):
        """JWT `exp` claim in epoch ms (decoded WITHOUT verification); None if not a JWT."""
        parts = self.access_token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = json.loads(_b64url_decode(parts[1]))
            return int(payload["exp"]) * 1000 if isinstance(payload.get("exp"), (int, float)) else None
        except Exception:
            return None


# ------------------------------------------------------------------ redaction

_JWT = r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"
_B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _b64url_decode(text: str) -> bytes:
    """Base-64url decoding in plain Python (the Dhan Cloud scanner blocks the standard module)."""
    bits = nbits = 0
    out = bytearray()
    for ch in text.rstrip("="):
        v = _B64URL.find(ch)
        if v < 0:
            raise ValueError("not base64url")
        bits, nbits = (bits << 6) | v, nbits + 6
        if nbits >= 8:
            nbits -= 8
            out.append((bits >> nbits) & 0xFF)
    return bytes(out)


class Redactor:
    """Removes registered secrets and anything JWT-shaped from text."""

    def __init__(self):
        self._secrets: set[str] = set()

    def register(self, *secrets: str) -> None:
        for s in secrets:
            if s and len(s) >= 4:
                self._secrets.add(s)

    def __call__(self, text) -> str:
        s = text if isinstance(text, str) else str(text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            s = s.replace(secret, "[REDACTED]")
        return re.sub(_JWT, "[REDACTED_JWT]", s)


# ---------------------------------------------------------------- rate limits

class RateLimiter:
    """Token buckets per class, plus Dhan's '1 unique option-chain request per
    3 s' rule per key. Clock and sleep are injectable (tests never wait)."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        now = clock()
        self._buckets = {k: [float(cap), now] for k, (cap, _) in RATE_SPECS.items()}
        self._last_key: dict[str, float] = {}

    def acquire(self, cls: str, unique_key: str | None = None) -> float:
        """Block until a permit is available. Returns seconds waited."""
        waited = 0.0
        if cls == "optionchain" and unique_key in self._last_key:
            gap = 3.0 - (self.clock() - self._last_key[unique_key])
            if gap > 0:
                self.sleep(gap)
                waited += gap
        cap, rate = RATE_SPECS[cls]
        b = self._buckets[cls]
        while True:
            now = self.clock()
            b[0] = min(cap, b[0] + (now - b[1]) * rate)
            b[1] = now
            if b[0] >= 1:
                b[0] -= 1
                break
            need = (1 - b[0]) / rate
            self.sleep(need)
            waited += need
        if cls == "optionchain" and unique_key is not None:
            self._last_key[unique_key] = self.clock()
        return waited


# ------------------------------------------------------------------ transport

@dataclass
class RawResponse:
    status: int
    body: bytes
    headers: dict
    received_at_ms: int


class UrllibTransport:
    """The only code that opens a socket. HTTPS to BASE_URL only."""

    def send(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> RawResponse:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return RawResponse(r.status, data, dict(r.headers), int(time.time() * 1000))
        except urllib.error.HTTPError as e:
            return RawResponse(e.code, e.read() or b"", dict(e.headers or {}), int(time.time() * 1000))


# ----------------------------------------------------------------- the client

@dataclass
class DhanResponse:
    endpoint: str
    payload: object          # the business payload (inner `data` when wrapped)
    envelope: str            # "wrapped" ({status,data}) or "bare" — logged, see design doc
    received_at_ms: int
    attempts: int


class DhanClient:
    def __init__(self, credentials: Credentials, *, transport=None, limiter: RateLimiter | None = None,
                 redactor: Redactor | None = None, logger=None, timeout_s: float = 15.0, max_attempts: int = 4,
                 sleep=time.sleep, base_url: str = BASE_URL, jitter=random.random):
        self._creds = credentials
        self.transport = transport or UrllibTransport()
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self.redact = redactor or Redactor()
        self.redact.register(credentials.access_token, credentials.client_id)
        self.log = logger
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.base_url = base_url
        self.jitter = jitter
        self.on_call = None   # optional observer: receives one outcome dict per request (no headers, no body)

    # ---- guard -------------------------------------------------------------
    @staticmethod
    def assert_read_only(method: str, path: str) -> str:
        p = path.lower()
        for frag in FORBIDDEN_FRAGMENTS:
            if frag in p:
                raise ForbiddenEndpointError("Refusing %s %s: account-changing or order path (matched %r)."
                                             % (method, path, frag))
        cls = READ_ONLY_ENDPOINTS.get((method.upper(), path))
        if cls is None:
            raise ForbiddenEndpointError("Refusing %s %s: not on the read-only allow-list." % (method, path))
        return cls

    # ---- core --------------------------------------------------------------
    def _emit(self, level: str, event: str, **fields):
        if self.log is not None:
            self.log(level, event, **{k: self.redact(v) if isinstance(v, str) else v for k, v in fields.items()})

    def request(self, method: str, path: str, body: dict | None = None, unique_key: str | None = None,
                accept_bare=None) -> DhanResponse:
        if self.on_call is None:
            return self._request(method, path, body, unique_key, accept_bare)
        t0 = time.monotonic()
        out = {"endpoint": path, "ok": False}
        try:
            r = self._request(method, path, body, unique_key, accept_bare)
            out.update(ok=True, attempts=r.attempts, envelope=r.envelope, receivedAtMs=r.received_at_ms)
            return r
        except DhanClientError as e:
            out.update(error=type(e).__name__, code=e.code, httpStatus=e.http_status,
                       attempts=e.attempts, message=self.redact(str(e))[:300])
            raise
        finally:
            out["durationMs"] = round((time.monotonic() - t0) * 1000, 1)
            self.on_call(out)

    def _request(self, method, path, body, unique_key, accept_bare) -> DhanResponse:
        cls = self.assert_read_only(method, path)
        headers = {"access-token": self._creds.access_token, "client-id": self._creds.client_id,
                   "Content-Type": "application/json", "Accept": "application/json"}
        if path == "/profile":  # DhanHQ-py auth.py user_profile sends dhanClientId for this endpoint
            headers["dhanClientId"] = self._creds.client_id
        data = json.dumps(body).encode() if body is not None else None
        last: DhanClientError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.acquire(cls, unique_key)
            try:
                raw = self.transport.send(method, self.base_url + path, headers, data, self.timeout_s)
            except Exception as e:  # DNS, reset, TLS, timeout: message may contain nothing secret, but redact anyway
                last = TransientError("Network error on %s: %s" % (path, self.redact(type(e).__name__)))
            else:
                try:
                    return self._interpret(path, raw, attempt, accept_bare)
                except DhanClientError as e:
                    last = e
            if not last.retryable or attempt == self.max_attempts:
                last.attempts = attempt
                self._emit("error", "request_failed", path=path, attempt=attempt, error=type(last).__name__,
                           code=last.code, http_status=last.http_status, message=str(last))
                raise last
            wait = last.retry_after_s or min(20.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + self.jitter() / 2)
            self._emit("warn", "request_retry", path=path, attempt=attempt, error=type(last).__name__,
                       code=last.code, wait_s=round(wait, 3))
            self.sleep(wait)
        raise last  # pragma: no cover

    def _interpret(self, path: str, raw: RawResponse, attempt: int, accept_bare) -> DhanResponse:
        text = raw.body.decode("utf-8", errors="replace") if raw.body else ""
        try:
            parsed = json.loads(text) if text else {}
        except ValueError:
            if raw.status >= 500:
                raise TransientError("HTTP %d with non-JSON body on %s" % (raw.status, path), http_status=raw.status)
            raise ResponseFormatError("Non-JSON response on %s (HTTP %d)" % (path, raw.status), http_status=raw.status)
        code = ""
        msg = "HTTP %d" % raw.status
        if isinstance(parsed, dict):
            code = str(parsed.get("errorCode") or parsed.get("internalErrorCode") or "").upper()
            msg = str(parsed.get("errorMessage") or parsed.get("internalErrorMessage") or msg)
        msg = self.redact(msg)
        if raw.status == 429 or code in THROTTLE_CODES:
            e = RateLimitedError("Throttled on %s: %s" % (path, msg), code=code, http_status=raw.status)
            ra = raw.headers.get("Retry-After") or raw.headers.get("retry-after")
            try:
                e.retry_after_s = float(ra) if ra else None
            except ValueError:
                e.retry_after_s = None
            raise e
        if code in PLAN_CODES:
            raise PlanError("Data API access missing on %s (%s): check the Data API plan in Dhan web; a new "
                            "token will not fix this." % (path, code), code=code, http_status=raw.status)
        if raw.status in (401, 403) or code in AUTH_CODES:
            raise AuthError("Dhan rejected the credentials on %s (%s). Generate a fresh token in Dhan web and "
                            "update your local environment." % (path, code or raw.status), code=code,
                            http_status=raw.status)
        if raw.status >= 500 or code in RETRYABLE_CODES:
            raise TransientError("Dhan server error on %s: %s" % (path, msg), code=code, http_status=raw.status)
        if raw.status >= 400:
            raise RequestRejectedError("Dhan rejected the request on %s: %s (%s)" % (path, msg, code),
                                       code=code, http_status=raw.status)
        if isinstance(parsed, dict) and parsed.get("status") not in (None, "success"):
            raise RequestRejectedError("Dhan returned status=%r on %s: %s" % (parsed.get("status"), path, msg),
                                       code=code)
        # Envelope: the TS client (used live) expects {"status","data"}; Dhan's
        # skills reference shows the same for /optionchain. Whether /charts and
        # /marketfeed bodies are wrapped is UNVERIFIED, so both are accepted and
        # the shape is reported for the live smoke test to settle.
        if isinstance(parsed, dict) and "data" in parsed:
            return DhanResponse(path, parsed["data"], "wrapped", raw.received_at_ms, attempt)
        if accept_bare is not None and accept_bare(parsed):
            return DhanResponse(path, parsed, "bare", raw.received_at_ms, attempt)
        raise ResponseFormatError("Unrecognised response envelope on %s" % path)

    # ---- typed read-only calls --------------------------------------------
    def expiry_list(self, scrip: int = 51, segment: str = "IDX_I") -> DhanResponse:
        return self.request("POST", "/optionchain/expirylist",
                            {"UnderlyingScrip": scrip, "UnderlyingSeg": segment},
                            unique_key="expirylist:%s:%s" % (segment, scrip),
                            accept_bare=lambda p: isinstance(p, list))

    def option_chain(self, expiry: str, scrip: int = 51, segment: str = "IDX_I") -> DhanResponse:
        return self.request("POST", "/optionchain",
                            {"UnderlyingScrip": scrip, "UnderlyingSeg": segment, "Expiry": expiry},
                            unique_key="%s:%s:%s" % (segment, scrip, expiry),
                            accept_bare=lambda p: isinstance(p, dict) and "oc" in p)

    def quote(self, instruments: dict) -> DhanResponse:
        """{segment: [security_id, ...]} -> 5-level depth, LTP, OI, OHLC, last_trade_time."""
        total = sum(len(v) for v in instruments.values())
        if total == 0 or total > 1000:
            raise RequestRejectedError("marketfeed/quote needs 1..1000 instruments, got %d" % total)
        return self.request("POST", "/marketfeed/quote", {k: list(v) for k, v in instruments.items()},
                            accept_bare=lambda p: isinstance(p, dict) and any(k in p for k in instruments))

    def intraday_candles(self, security_id: str, segment: str, instrument: str, interval: int,
                         from_datetime: str, to_datetime: str, oi: bool = False) -> DhanResponse:
        if interval not in (1, 5, 15, 25, 60):
            raise RequestRejectedError("interval must be 1, 5, 15, 25 or 60")
        return self.request("POST", "/charts/intraday",
                            {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                             "interval": interval, "oi": oi, "fromDate": from_datetime, "toDate": to_datetime},
                            accept_bare=lambda p: isinstance(p, dict) and "timestamp" in p)

    def profile(self) -> DhanResponse:
        """Read-only token/plan check (tokenValidity, dataPlan, dataValidity)."""
        return self.request("GET", "/profile", None, accept_bare=lambda p: isinstance(p, dict))

    def daily_candles(self, security_id: str, segment: str, instrument: str, from_date: str, to_date: str,
                      expiry_code: int = 0, oi: bool = False) -> DhanResponse:
        return self.request("POST", "/charts/historical",
                            {"securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
                             "expiryCode": expiry_code, "oi": oi, "fromDate": from_date, "toDate": to_date},
                            accept_bare=lambda p: isinstance(p, dict) and "timestamp" in p)
