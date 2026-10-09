"""Replay recorded live scans through the Python scanner and compare with the
TypeScript reference produced by server/scripts/replay-recorded.ts.

  cd server && npx tsx scripts/replay-recorded.ts ../scan-records      # writes ts_reference.json per scan
  cd engine && python3 tests/replay_recorded.py ../scan-records         # PASS/FAIL per scan
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from parity_support import _hook, compare  # noqa: E402
from sensex.dhan_client import DhanClient, RateLimiter, Redactor  # noqa: E402
from sensex.mock_dhan import MOCK_CREDENTIALS, MockTransport  # noqa: E402
from sensex.scan import run_refresh  # noqa: E402
from sensex.scanner import JsonLogger, Scanner, ScannerConfig  # noqa: E402


def replay(scan_dir: Path) -> dict:
    bodies = json.loads((scan_dir / "bodies.json").read_text())["bodies"]
    receipts = json.loads((scan_dir / "receipts.json").read_text())
    meta = json.loads((scan_dir / "meta.json").read_text())
    t = MockTransport(bodies, {k: int(v) for k, v in receipts.items()}, default_ms=int(receipts.get("/optionchain", 0)))
    sim = [0.0]  # simulated time: advances only when the limiter sleeps (never a real wait)
    lim = RateLimiter(clock=lambda: sim[0], sleep=lambda s: sim.__setitem__(0, sim[0] + s))
    import io
    red = Redactor()
    client = DhanClient(MOCK_CREDENTIALS, transport=t, limiter=lim, redactor=red, sleep=lambda s: None)
    at = datetime.fromisoformat(meta["clockUtc"])
    cfg = ScannerConfig(strikes=meta["strikes"], futures_security_id=meta["futuresSecurityId"],
                        futures_expiry=meta["futuresExpiry"], ignore_session=True)
    return Scanner(client, cfg, JsonLogger(red, stream=io.StringIO()), clock=lambda: at).run_once(), at


def main(root: str) -> int:
    failures = 0
    for d in sorted(Path(root).glob("scan-*")):
        ref_path = d / "ts_reference.json"
        if not (d / "bodies.json").exists() or not ref_path.exists():
            print("%s: SKIPPED (missing bodies or ts_reference.json)" % d.name)
            continue
        rec, at = replay(d)
        ts = json.loads(ref_path.read_text(), object_hook=_hook)["expected"]
        py = rec["scanInputs"] if rec["status"] == "OK" else {"status": rec["status"], "reasons": (rec.get("reason") or "").split(" | ")}
        mism = compare(py, ts, d.name)
        same_table = rec["status"] != "OK" or run_refresh(py, at)[1] == run_refresh(ts, at)[1]
        ok = not mism and same_table
        failures += not ok
        print("%s: %s status=%s%s" % (d.name, "PASS" if ok else "FAIL", rec["status"],
                                      "" if ok else "\n  " + "\n  ".join(mism[:10])))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "scan-records"))
