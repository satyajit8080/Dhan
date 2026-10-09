"""
LOCAL-ONLY file and environment access for engine/scan_local.py.

Kept out of every other module so the Dhan Cloud bundle (cloud/build_bundle.py
never includes this file) contains no environment reads, file reads/writes or
pathlib: the Cloud scanner blocks them (observed 9 Oct 2026).
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from .dhan_client import AuthError, Credentials
from .instruments import InstrumentMapping, MappingMissingError
from .recording import RECORDED_PATHS


def credentials_from_environment(env=None) -> Credentials:
    """DHAN_CLIENT_ID plus either DHAN_ACCESS_TOKEN or DHAN_TOKEN_FILE (a file
    containing only the token). Same variables as the TypeScript server."""
    env = os.environ if env is None else env
    cid = (env.get("DHAN_CLIENT_ID") or "").strip()
    tok = (env.get("DHAN_ACCESS_TOKEN") or "").strip()
    path = (env.get("DHAN_TOKEN_FILE") or "").strip()
    if not tok and path:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    if not cid or not tok:
        raise AuthError("No Dhan credentials: set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN or DHAN_TOKEN_FILE "
                        "in your local environment (see docs/PHASE5_DATA_CLIENT_DESIGN.md).")
    return Credentials(cid, tok)


def load_mapping(path: str | None) -> InstrumentMapping:
    if not path:
        raise MappingMissingError(
            "No verified instrument-master mapping. Futures lookup is BLOCKED until the real "
            "header is verified; pass --futures-security-id/--futures-expiry instead.")
    with open(path, "r", encoding="utf-8") as f:
        return InstrumentMapping.from_dict(json.load(f))


def load_holidays(path: str | None) -> frozenset:
    if not path:
        return frozenset()
    out = set()
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            s = line.split("#", 1)[0].strip()
            if not s:
                continue
            try:
                out.add(date.fromisoformat(s))
            except ValueError:
                raise ValueError("holiday file line %d is not YYYY-MM-DD: %r" % (n, s)) from None
    return frozenset(out)


def file_sink(path: str):
    """Line appender for JsonLogger(sink=...)."""
    def write(line: str):
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return write


class RecordingTransport:
    """Saves, per scan under <dir>/scan-NNN/: bodies.json, receipts.json, meta.json.
    Never saved: request headers, credentials, /profile, or anything else."""

    def __init__(self, inner, directory: str):
        self.inner, self.root = inner, Path(directory)
        self.scan_dir = None
        self.bodies, self.receipts = {}, {}

    def begin_scan(self, n: int, meta: dict):
        self.flush()
        self.scan_dir = self.root / ("scan-%03d" % n)
        self.scan_dir.mkdir(parents=True, exist_ok=True)
        (self.scan_dir / "meta.json").write_text(json.dumps(meta, indent=1))
        self.bodies, self.receipts = {}, {}

    def send(self, method, url, headers, body, timeout):
        r = self.inner.send(method, url, headers, body, timeout)
        path = url.split("/v2", 1)[1]
        if path in RECORDED_PATHS and r.status == 200:
            try:
                self.bodies[path] = json.loads(r.body.decode("utf-8"))
                self.receipts[path] = r.received_at_ms
            except ValueError:
                pass
        return r

    def flush(self):
        if self.scan_dir is not None and self.bodies:
            (self.scan_dir / "bodies.json").write_text(json.dumps({"note": "RECORDED live market data", "bodies": self.bodies}))
            (self.scan_dir / "receipts.json").write_text(json.dumps(self.receipts))
