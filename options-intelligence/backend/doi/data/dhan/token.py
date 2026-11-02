"""Access-token source and expiry monitor. Dhan tokens last at most 24 h.

The raw token is only returned by `TokenSource.get()` for the transport header.
Everything else (status, logs, API) sees the fingerprint (last 4 chars)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from doi.core.errors import TokenRejected

TokenState = Literal["missing", "valid", "expiring", "expired", "unknown_expiry"]


@dataclass(frozen=True, slots=True)
class TokenStatus:
    state: TokenState
    fingerprint: str | None
    expires_at: datetime | None
    seconds_left: float | None


def _jwt_exp(token: str) -> datetime | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return datetime.fromtimestamp(float(claims["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError):
        return None


def inspect_token(token: str | None, now: datetime, warn_before: timedelta = timedelta(hours=1)) -> TokenStatus:
    if not token:
        return TokenStatus("missing", None, None, None)
    fp = "…" + token[-4:]
    exp = _jwt_exp(token)
    if exp is None:
        return TokenStatus("unknown_expiry", fp, None, None)
    left = (exp - now).total_seconds()
    state: TokenState = "expired" if left <= 0 else "expiring" if left <= warn_before.total_seconds() else "valid"
    return TokenStatus(state, fp, exp, left)


class TokenSource:
    """Reads the token from a static value or a file (re-read when it changes),
    so rotation needs no restart."""

    def __init__(self, *, value: str | None = None, path: Path | None = None) -> None:
        self._value = value
        self._path = path
        self._mtime: float | None = None

    def _reload(self) -> None:
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._mtime:
            self._value = self._path.read_text().strip() or None
            self._mtime = mtime

    def peek_status(self, now: datetime | None = None) -> TokenStatus:
        self._reload()
        return inspect_token(self._value, now or datetime.now(UTC))

    def get(self) -> str:
        status = self.peek_status()
        if status.state == "missing":
            raise TokenRejected("no Dhan access token configured")
        if status.state == "expired":
            raise TokenRejected(f"Dhan access token {status.fingerprint} expired at {status.expires_at}")
        assert self._value is not None
        return self._value
