"""Error taxonomy. Dhan failures are classified so callers can react distinctly
(retry, re-authenticate, or reject) instead of treating every failure alike."""

from __future__ import annotations


class DoiError(Exception):
    """Base for all platform errors."""


class ConfigError(DoiError):
    pass


class ForbiddenEndpoint(DoiError):
    """Raised when code tries to reach a Dhan path outside the read-only allowlist."""


class InstrumentError(DoiError):
    """Instrument master missing, ambiguous or inconsistent."""


class SchemaError(DoiError):
    """Upstream payload did not match the expected shape."""


class StaleData(DoiError):
    pass


class CircuitOpen(DoiError):
    pass


class DhanError(DoiError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class TokenRejected(DhanError):
    """Missing, expired or invalid access token. Never retried automatically."""


class RateLimited(DhanError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class UpstreamError(DhanError):
    """5xx or transport-level failure."""


class UpstreamTimeout(DhanError):
    pass


class DhanRequestError(DhanError):
    """Non-retryable 4xx / business failure (bad input, no subscription...)."""


RETRYABLE: tuple[type[DhanError], ...] = (RateLimited, UpstreamError, UpstreamTimeout)
