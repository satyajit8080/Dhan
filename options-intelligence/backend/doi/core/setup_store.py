"""Non-secret setup choices saved from the Setup Wizard (setup.json in SECRETS_DIR, mode 0600).

Environment variables still win. Applied at process start (the wizard triggers a restart)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

UNIVERSE = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX")
FILENAME = "setup.json"


def path(directory: Path) -> Path:
    return Path(directory) / FILENAME


def load(directory: Path) -> dict:
    try:
        data = json.loads(path(directory).read_text())
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return {}
    out: dict = {}
    for k in ("ai_enabled", "dhan_feed_enabled"):
        if isinstance(data.get(k), bool):
            out[k] = data[k]
    if isinstance(data.get("live_indices"), list):
        out["live_indices"] = [x for x in data["live_indices"] if x in UNIVERSE]
    return out


def save(directory: Path, changes: dict) -> dict:
    cur = load(directory) | {k: v for k, v in changes.items() if v is not None}
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cur, f, sort_keys=True)
        os.replace(tmp, path(d))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return cur
