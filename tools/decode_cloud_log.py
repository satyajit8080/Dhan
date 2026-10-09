#!/usr/bin/env python3
"""
Decode a Dhan Cloud log exported from the Cloud interface (Phase 6).

  python3 tools/decode_cloud_log.py <exported-log.txt> [more logs...] --out observation/2026-10-12

Writes:
  <out>/events.jsonl           every "BX|" JSON event, in order
  <out>/scan-records/scan-*/   replay records (bodies.json, receipts.json, meta.json),
                               the same layout as `scan_local.py --record`, so the existing
                               replay tools run unchanged:
                                 cd server && npx tsx scripts/replay-recorded.ts <out>/scan-records
                                 cd engine && python3 tests/replay_recorded.py <out>/scan-records
  <out>/decode_report.json     counts, truncated lines, incomplete records, secret check

Tolerates timestamps or other text the log viewer puts before "BX|".
Refuses to write anything if a line looks like it contains a token.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
from pathlib import Path

JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
SECRET_HINTS = re.compile(r"access[-_]?token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9]{12,}", re.I)
HEX = re.compile(r"(?:[0-9a-f]{2})+")
REC = re.compile(r"BX\|REC\|([^|]+)\|(\d+)/(\d+)\|([A-Za-z0-9+/=]*)")


def decode_lines(lines):
    events, chunks, bad, secret_lines = [], {}, [], []
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        if JWT.search(line) or SECRET_HINTS.search(line):
            secret_lines.append(n)
        i = line.find("BX|")
        if i < 0:
            continue
        body = line[i + 3:]
        if body.startswith("REC|"):
            m = REC.search(line[i:])
            if not m:
                bad.append({"line": n, "why": "malformed replay chunk"})
                continue
            sid, k, total, data = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
            chunks.setdefault(sid, {"total": total, "parts": {}})["parts"][k] = data
            continue
        try:
            events.append(json.loads(body))
        except ValueError:
            bad.append({"line": n, "why": "JSON event truncated or wrapped by the log viewer"})
    records, incomplete = {}, []
    for sid, c in chunks.items():
        if sorted(c["parts"]) != list(range(1, c["total"] + 1)):
            incomplete.append({"scanId": sid, "have": sorted(c["parts"]), "total": c["total"]})
            continue
        try:
            blob = "".join(c["parts"][k] for k in sorted(c["parts"]))
            raw = bytes.fromhex(blob) if HEX.fullmatch(blob) else base64.b64decode(blob)   # 6.2+: hex; 6.0/6.1: base64
            records[sid] = json.loads(gzip.decompress(raw))
        except (ValueError, OSError) as e:
            incomplete.append({"scanId": sid, "error": type(e).__name__})
    return events, records, bad, incomplete, secret_lines


def write_out(out: Path, events, records):
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    for sid, r in sorted(records.items()):
        d = out / "scan-records" / ("scan-%s" % sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "bodies.json").write_text(json.dumps({"note": "RECORDED live market data (Dhan Cloud)", "bodies": r["bodies"]}))
        (d / "receipts.json").write_text(json.dumps(r["receipts"]))
        (d / "meta.json").write_text(json.dumps(r["meta"], indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    lines = []
    for p in a.logs:
        lines += Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
    events, records, bad, incomplete, secret_lines = decode_lines(lines)
    if secret_lines:
        print("REFUSED: %d line(s) look like they contain a token or auth header (lines %s). "
              "Nothing written. Delete the log file and report this as a defect." % (len(secret_lines), secret_lines[:10]),
              file=sys.stderr)
        return 3
    write_out(Path(a.out), events, records)
    report = {"lines": len(lines), "events": len(events), "scans": sum(1 for e in events if e.get("event") == "scan"),
              "replayRecords": len(records), "incompleteRecords": incomplete, "unparsedLines": bad[:50],
              "unparsedCount": len(bad), "secretCheck": "PASS (no token-shaped strings)"}
    (Path(a.out) / "decode_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "unparsedLines"}, indent=1))
    return 0 if not bad and not incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
