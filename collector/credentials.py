"""Dhan credentials, resolved the same way as the Bull50 MCP server (server/src/config.ts).

Order (first hit wins):
  1. env DHAN_ACCESS_TOKEN
  2. file named by env DHAN_TOKEN_FILE
  3. server/.env  (DHAN_ACCESS_TOKEN / DHAN_TOKEN_FILE / DHAN_CLIENT_ID lines)
Client id: env DHAN_CLIENT_ID, else server/.env.

The token is never printed, logged or written. Only its last 4 characters are shown.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_DIR

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")
_KNOWN: set[str] = set()


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    client_id: str

    @property
    def fingerprint(self) -> str:
        return f"...{self.token[-4:]}"

    def __str__(self) -> str:  # never leak the token through str()/repr()
        return f"Credentials(token={self.fingerprint}, client_id={self.client_id})"

    __repr__ = __str__


def redact(text: str) -> str:
    """Strip any known token or JWT-shaped string from text."""
    for secret in _KNOWN:
        text = text.replace(secret, "***")
    return _JWT_RE.sub("***", text)


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_credentials(env: dict[str, str] | None = None, dotenv: Path | None = None) -> Credentials:
    env = dict(os.environ) if env is None else env
    file_env = _read_dotenv(dotenv or REPO_DIR / "server" / ".env")

    def get(name: str) -> str:
        return (env.get(name) or file_env.get(name) or "").strip()

    token = get("DHAN_ACCESS_TOKEN")
    if not token and get("DHAN_TOKEN_FILE"):
        p = Path(get("DHAN_TOKEN_FILE")).expanduser()
        if not p.is_file():
            raise CredentialError("DHAN_TOKEN_FILE is set but the file does not exist")
        token = p.read_text(encoding="utf-8").strip()
    client_id = get("DHAN_CLIENT_ID")
    if not token:
        raise CredentialError("No Dhan token: set DHAN_ACCESS_TOKEN or DHAN_TOKEN_FILE "
                              "(env or server/.env)")
    if not client_id:
        raise CredentialError("No Dhan client id: set DHAN_CLIENT_ID (env or server/.env)")
    if len(token) >= 8:
        _KNOWN.add(token)
    return Credentials(token=token, client_id=client_id)
