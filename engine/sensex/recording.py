"""
Record a live read-only scan so it can be replayed OFFLINE through both the
Python scanner and the TypeScript functions (exact live-data parity).

Saved per scan, under <dir>/scan-NNN/:
  bodies.json    response bodies of the four market-data calls (parsed JSON)
  receipts.json  receive time (epoch ms) per path
  meta.json      strikes, futures contract, scan clock
Never saved: request headers, credentials, /profile, or anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

RECORDED_PATHS = ("/optionchain/expirylist", "/optionchain", "/marketfeed/quote", "/charts/intraday")


class RecordingTransport:
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
