"""Structured logging with secret redaction. Tokens, PINs and auth headers must
never reach a log sink."""

from __future__ import annotations

import logging
import re

_JWT = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")
# platform API keys (doi_…), xAI keys (xai-…) and any bearer credential
_TOKENS = re.compile(r"(doi_[\w-]{16,}|xai-[\w-]{16,}|(?i:bearer)\s+[\w.~+/=-]{8,})")
_HEADER = re.compile(r"(?i)(access-token|authorization|client-secret|pin|totp)(['\"]?\s*[:=]\s*['\"]?)([^\s,'\"}]+)")


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 6:
            self._secrets.append(secret)

    def redact(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        text = _JWT.sub("***", text)
        text = _TOKENS.sub("***", text)
        return _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> RedactingFilter:
    flt = RedactingFilter(secrets)
    handler = logging.StreamHandler()
    handler.addFilter(flt)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return flt
