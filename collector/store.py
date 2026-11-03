"""Raw-response store. Every API response is kept (gzip JSON) for audit.

A request whose raw file exists is complete and is never re-requested, which is
what makes runs resumable. Failed requests are not stored as raw; they go to
errors.jsonl and are retried on the next run.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


def leg_name(offset: int, side: str) -> str:
    strike = "ATM" if offset == 0 else f"ATM{offset:+d}"
    return f"{strike}_{side.upper()}"


class RawStore:
    def __init__(self, root: Path):
        self.root = root
        self.raw = root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.errors = root / "logs" / "errors.jsonl"
        self.errors.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(symbol: str) -> str:
        return symbol.replace("&", "_AND_").replace("/", "_")

    def path(self, symbol: str, frm: str, to: str, offset: int, side: str) -> Path:
        return self.raw / self._safe(symbol) / f"{frm}_{to}" / f"{leg_name(offset, side)}.json.gz"

    def has(self, *key) -> bool:
        return self.path(*key).exists()

    def put(self, key: tuple, request: dict, status: str, http_status: Optional[int],
            payload: dict, attempts: int) -> None:
        p = self.path(*key)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "status": status, "http_status": http_status, "attempts": attempts,
               "request": request, "response": payload}
        tmp = p.with_name(p.name + ".part")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(rec, f, separators=(",", ":"))
        os.replace(tmp, p)                      # atomic: a crash never leaves half a file

    def get(self, *key) -> Optional[dict]:
        p = self.path(*key)
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            p.unlink(missing_ok=True)           # corrupt -> refetch next run
            return None

    def drop(self, *key) -> None:
        self.path(*key).unlink(missing_ok=True)

    def log_error(self, symbol: str, key: tuple, request: dict, error: str,
                  http_status: Optional[int]) -> None:
        rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "symbol": symbol, "window": f"{key[1]}_{key[2]}",
               "leg": leg_name(key[3], key[4]), "http_status": http_status,
               "error": error, "request": request}
        with self.errors.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def iter_symbol(self, symbol: str) -> Iterator[dict]:
        d = self.raw / self._safe(symbol)
        if not d.exists():
            return
        for p in sorted(d.glob("*/*.json.gz")):
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    yield json.load(f)
            except (OSError, ValueError):
                continue
