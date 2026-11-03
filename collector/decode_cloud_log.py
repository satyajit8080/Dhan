"""Decode a Dhan Cloud run log of cloud/stock_options_history_cloud.py into CSV/ZIP/report.

  python -m collector.decode_cloud_log run1.log [run2.log ...] [--data-dir DIR] [--tolerance N]

Reads the BX|STK*, BX|STKINST and BX|STKMETA lines (log prefixes such as timestamps are
ignored), stores every leg in the collector's raw store (data/options_history/raw),
then runs the normal build: per-symbol CSVs, all_symbols.csv, missing_data.csv,
validation_report.md, summary.json and the ZIP. Several logs (e.g. resumed runs) can
be passed at once; a record seen twice keeps the last copy.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from . import collect, config
from .store import RawStore

KINDS = ("STK", "STKINST", "STKMETA")


def read_records(paths: list[Path]) -> tuple[dict, list[str]]:
    chunks: dict[tuple, dict] = {}
    for p in paths:
        with p.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                i = raw.find("BX|")
                if i < 0:
                    continue
                parts = raw[i:].strip().split("|")
                if len(parts) != 5 or parts[1] not in KINDS or "/" not in parts[3]:
                    continue
                _, kind, rid, pos, hexpart = parts
                k, n = (int(x) for x in pos.split("/"))
                slot = chunks.setdefault((kind, rid), {"n": n, "parts": {}})
                if slot["n"] != n:               # a newer run re-emitted it with a new size
                    slot["n"], slot["parts"] = n, {}
                slot["parts"][k] = hexpart
    records, broken = {}, []
    for (kind, rid), slot in chunks.items():
        if len(slot["parts"]) != slot["n"]:
            broken.append(f"{kind}|{rid}: {len(slot['parts'])}/{slot['n']} chunks")
            continue
        blob = "".join(slot["parts"][i] for i in range(1, slot["n"] + 1))
        try:
            records[(kind, rid)] = json.loads(gzip.decompress(bytes.fromhex(blob)))
        except (ValueError, OSError) as e:
            broken.append(f"{kind}|{rid}: {type(e).__name__}")
    return records, broken


def offset_of(label: str) -> int:
    return 0 if label == "ATM" else int(label[3:])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m collector.decode_cloud_log", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path)
    ap.add_argument("--data-dir", default=str(config.DEFAULT_DATA_DIR))
    ap.add_argument("--tolerance", type=int, default=None,
                    help="minutes around 09:30/15:20 (default: what the cloud run kept)")
    a = ap.parse_args(argv)

    records, broken = read_records(a.logs)
    metas = [v for (k, _), v in records.items() if k == "STKMETA"]
    if not metas:
        print("no BX|STKMETA record found — is this a log of stock_options_history_cloud.py?")
        return 2
    data_dir = Path(a.data_dir)
    store = RawStore(data_dir)

    inst: dict = {}
    for (k, _), v in records.items():
        if k == "STKINST":
            inst.update(v)
    if inst:
        p = data_dir / "instruments" / "resolved.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        saved = json.loads(p.read_text()) if p.exists() else {}
        saved.update(inst)
        p.write_text(json.dumps(saved, indent=2))

    n_legs = n_err = 0
    for (k, rid), rec in records.items():
        if k != "STK":
            continue
        sym, frm, to = rec["symbol"], rec["from"], rec["to"]
        for leg in rec["legs"]:
            key = (sym, frm, to, offset_of(leg["strike"]), leg["side"])
            if leg["status"] in ("ok", "no_data"):
                resp = {"data": leg.get("data") or {}, "barsReturned": leg.get("barsReturned"),
                        "barsKept": leg.get("barsKept"), "source": "dhan-cloud-log"}
                store.put(key, leg["request"], leg["status"], leg.get("httpStatus"), resp,
                          leg.get("attempts", 1))
                n_legs += 1
            else:
                store.log_error(sym, key, leg["request"],
                                f"cloud run: HTTP {leg.get('httpStatus')} {leg.get('errorCode')}", leg.get("httpStatus"))
                n_err += 1
    print(f"decoded {len(records)} records: {n_legs} legs stored, {n_err} failed legs logged")
    if broken:
        print(f"WARNING {len(broken)} incomplete records (log cut off?): " + "; ".join(broken[:10]))

    ranges: dict = {}
    for m in metas:
        r = ranges.setdefault((m["from"], m["to"]), {"symbols": [], "tol": m["keepToleranceMin"]})
        for s in m["symbols"]:
            if s not in r["symbols"]:
                r["symbols"].append(s)
    rc = 0
    for (frm, to), r in ranges.items():
        tol = r["tol"] if a.tolerance is None else min(a.tolerance, r["tol"])
        rc |= collect.main(["build", "--from", frm, "--to", to, "--symbols", ",".join(r["symbols"]),
                            "--data-dir", str(data_dir), "--tolerance", str(tol)])
    return rc


if __name__ == "__main__":
    sys.exit(main())
