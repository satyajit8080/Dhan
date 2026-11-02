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
        # Redact the template and each argument in place. Keeping the args matters: formatters
        # such as uvicorn's access log unpack record.args (found in the Phase 8 deployment drill).
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self.redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self.redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
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


def settings_secrets(settings) -> list[str]:
    """Every configured secret value, including the password inside DATABASE_URL."""
    import re as _re

    out = []
    for name in ("xai_api_key", "dhan_access_token", "database_url"):
        v = getattr(settings, name, None)
        if v is not None:
            raw = v.get_secret_value()
            out.append(raw)
            m = _re.search(r"://[^:/@]+:([^@]+)@", raw) or _re.search(r"password=([^\s]+)", raw)
            if m:
                out.append(m.group(1))
    return out


def install_redaction(
    secrets: list[str], logger_names: tuple[str, ...] = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
) -> RedactingFilter:
    """Attach one redacting filter to every handler of the given loggers (API process).
    Loggers without handlers get a redacting stream handler so nothing bypasses it."""
    flt = RedactingFilter(secrets)
    for name in logger_names:
        lg = logging.getLogger(name)
        if not lg.handlers and name == "":
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            lg.addHandler(h)
        for h in lg.handlers:
            h.addFilter(flt)
        lg.addFilter(flt)
    return flt
