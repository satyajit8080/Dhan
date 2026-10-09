"""
MOCK Dhan transport for offline tests and `scan_local.py --mock`.

Serves the Stage A HTTP bodies recorded in parity/stage_a/<scenario>/ (built
from the repo's GM 18-Sep / 21-Sep fixtures; futures quotes synthetic). They
are MOCK DATA, not live market data, and every record produced from them is
labelled so. No network, no credentials.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .dhan_client import Credentials, DhanClient, RateLimiter, RawResponse
from .scanner import JsonLogger, Scanner, ScannerConfig

STAGE_A = Path(__file__).resolve().parents[2] / "parity" / "stage_a"
MOCK_CREDENTIALS = Credentials("MOCKCLIENT", "mock-token-not-a-real-credential-000")


def load_scenario(name: str):
    bodies = json.loads((STAGE_A / name / "bodies.json").read_text())["bodies"]
    ref = json.loads((STAGE_A / name / "reference.json").read_text())
    return bodies, ref


class MockTransport:
    """Replays fixed bodies. `receipts` maps path -> received_at_ms; `script`
    maps path -> list of RawResponse to return first (for fault injection)."""

    def __init__(self, bodies: dict, receipts: dict | None = None, default_ms: int = 0, script=None):
        self.bodies, self.receipts, self.default_ms = bodies, receipts or {}, default_ms
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def send(self, method, url, headers, body, timeout):
        path = url.split("/v2", 1)[1]
        self.calls.append({"method": method, "path": path, "headers": dict(headers),
                           "body": json.loads(body) if body else None, "timeout": timeout})
        queued = self.script.get(path)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path not in self.bodies:
            return RawResponse(404, b'{"status":"failure","errorMessage":"no route"}', {}, self.default_ms)
        return RawResponse(200, json.dumps(self.bodies[path]).encode(), {},
                           self.receipts.get(path, self.default_ms))


def replay_client(name: str, logger=None, sleep=lambda s: None):
    bodies, ref = load_scenario(name)
    receipts = {"/optionchain": ref["receipts"]["chainMs"],
                "/marketfeed/quote": ref["receipts"]["futuresMs"] or ref["receipts"]["chainMs"]}
    t = MockTransport(bodies, receipts, default_ms=ref["receipts"]["chainMs"])
    clock = [0.0]
    limiter = RateLimiter(clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    return DhanClient(MOCK_CREDENTIALS, transport=t, limiter=limiter, logger=logger, sleep=sleep), t, ref


def mock_scanner(redactor, name: str = "s21Sep"):
    import io
    log = JsonLogger(redactor, stream=io.StringIO())
    client, _, ref = replay_client(name, logger=log)
    at = datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, timezone.utc)
    cfg = ScannerConfig(strikes=ref["strikes"], futures_security_id="844615", futures_expiry="2026-09-24",
                        ignore_session=True)
    return Scanner(client, cfg, log, clock=lambda: at), log
