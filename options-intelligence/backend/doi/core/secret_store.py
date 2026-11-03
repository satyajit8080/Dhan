"""File-backed secret store for keys entered from the Settings page.

Values live only on the server, one file per secret, mode 0600, written atomically.
They are never returned by the API: callers get a fingerprint (sha256 prefix) and metadata.
Environment variables, when set, take precedence over files.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class SecretSpec:
    name: str
    label: str
    env: str
    filename: str
    pattern: str
    hot_reload: bool  # applied without restart
    help: str


SPECS: dict[str, SecretSpec] = {
    s.name: s
    for s in (
        SecretSpec("dhan_client_id", "Dhan client ID", "DHAN_CLIENT_ID", "dhan_client_id", r"^[0-9]{4,20}$", False,
                   "Numeric Dhan client ID (web.dhan.co → profile)."),
        SecretSpec("dhan_access_token", "Dhan access token", "DHAN_ACCESS_TOKEN", "dhan.token", r"^[A-Za-z0-9._\-]{20,4096}$", True,
                   "Daily access token from web.dhan.co → DhanHQ Trading APIs. Read-only use; rotates without restart."),
        SecretSpec("xai_api_key", "Grok (xAI) API key", "XAI_API_KEY", "xai.key", r"^xai-[A-Za-z0-9_\-]{16,200}$", True,
                   "console.x.ai → API keys. Spend is capped at ₹3,000/month by the server."),
    )
}  # fmt: skip


class SecretError(ValueError):
    pass


class SecretDirError(OSError):
    """The secrets directory is not readable/writable by this process."""


def _perm_hint(directory: Path) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else "?"
    return (f"secrets folder {directory} is not accessible by the app (process uid {uid}). "
            f"On the server run: sudo chown -R {uid}:{uid} secrets && chmod 700 secrets && docker compose restart api ai-worker")  # fmt: skip


def fingerprint(value: str | None) -> str | None:
    return None if not value else "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:10]


class SecretStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        return self.dir / SPECS[name].filename

    def read(self, name: str) -> str | None:
        try:
            return self.path(name).read_text().strip() or None
        except (FileNotFoundError, NotADirectoryError):
            return None
        except PermissionError:
            raise SecretDirError(_perm_hint(self.dir)) from None

    def write(self, name: str, value: str) -> Path:
        spec = SPECS.get(name)
        if spec is None:
            raise SecretError(f"unknown secret {name}")
        value = value.strip()
        if not re.fullmatch(spec.pattern, value):
            raise SecretError(f"{spec.label}: invalid format")
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        except PermissionError:
            raise SecretDirError(_perm_hint(self.dir)) from None
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(value + "\n")
            os.replace(tmp, self.path(name))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.path(name)

    def delete(self, name: str) -> bool:
        p = self.path(name)
        try:
            if p.exists():
                p.unlink()
                return True
        except PermissionError:
            raise SecretDirError(_perm_hint(self.dir)) from None
        return False

    def status(self, name: str, env: dict[str, str] | None = None) -> dict:
        spec = SPECS[name]
        env = os.environ if env is None else env
        file_val = self.read(name)
        env_val = env.get(spec.env) or None
        p = self.path(name)
        return {
            "name": name,
            "label": spec.label,
            "help": spec.help,
            "configured": bool(env_val or file_val),
            "source": "env" if env_val else "file" if file_val else None,
            "env_overrides_file": bool(env_val and file_val),
            "fingerprint": fingerprint(env_val or file_val),
            "updated_at": datetime.fromtimestamp(p.stat().st_mtime, UTC) if file_val else None,
            "hot_reload": spec.hot_reload,
        }
