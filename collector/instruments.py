"""Resolve underlying security ids, lot sizes and listed expiries from Dhan's instrument master.

The master lists only live contracts, so it proves *current* F&O eligibility.
Historical eligibility (a stock entering or leaving F&O) is checked from the data
itself in report.py: the first/last date Dhan returns bars for each symbol.
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

from . import config


@dataclass
class Instrument:
    symbol: str
    status: str                             # OK | NOT_IN_FNO | NOT_FOUND | ID_MISMATCH
    security_id: Optional[str] = None       # underlying (NSE equity) id the API wants
    equity_id: Optional[str] = None
    fno_underlying_id: Optional[str] = None
    lot_size: Optional[int] = None
    listed_expiries: list = field(default_factory=list)
    note: str = ""


def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _underlying(tsym: str) -> str:
    """'BAJAJ-AUTO-Nov2026-9000-CE' -> 'BAJAJ-AUTO' (symbols can contain hyphens)."""
    m = re.match(r"^(.+?)-[A-Za-z]{3}\d{4}-", tsym)
    return m.group(1) if m else tsym.split("-")[0]


def _normalise(row: dict) -> dict:
    """Map either master format (detailed or compact) to one shape."""
    tsym = _pick(row, "SEM_TRADING_SYMBOL", "SYMBOL_NAME")
    return {
        "exch": _pick(row, "EXCH_ID", "SEM_EXM_EXCH_ID"),
        "segment": _pick(row, "SEGMENT", "SEM_SEGMENT"),
        "sec_id": _pick(row, "SECURITY_ID", "SEM_SMST_SECURITY_ID"),
        "instrument": _pick(row, "INSTRUMENT", "SEM_INSTRUMENT_NAME"),
        "series": _pick(row, "SERIES", "SEM_SERIES"),
        "underlying_id": _pick(row, "UNDERLYING_SECURITY_ID"),
        "underlying": _pick(row, "UNDERLYING_SYMBOL") or _underlying(tsym),
        "tsym": tsym,
        "lot": _pick(row, "LOT_SIZE", "SEM_LOT_UNITS"),
        "expiry": _pick(row, "SM_EXPIRY_DATE", "SEM_EXPIRY_DATE")[:10],
    }


def download_master(dest_dir: Path, urls: Iterable[str] = config.SCRIP_MASTER_URLS,
                    max_age_days: int = 0) -> Path:
    """Fetch the master into dest_dir/YYYY-MM-DD.csv (kept for audit). Reuses today's copy."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"scrip_master_{date.today().isoformat()}.csv"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    last = None
    for url in urls:
        try:
            tmp = dest.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f)
            tmp.replace(dest)
            return dest
        except Exception as e:  # try the next format
            last = e
    raise RuntimeError(f"could not download Dhan instrument master: {last}")


def resolve(symbols: list[str], master_csv: Path, today: Optional[date] = None) -> dict[str, Instrument]:
    today = today or date.today()
    wanted = {s.upper() for s in symbols}
    equity: dict[str, str] = {}
    opt_ids: dict[str, set] = {s: set() for s in wanted}
    opt_lots: dict[str, dict] = {s: {} for s in wanted}
    with master_csv.open(newline="", encoding="utf-8", errors="replace") as f:
        for raw in csv.DictReader(f):
            r = _normalise(raw)
            if r["exch"] != "NSE":
                continue
            if r["segment"] == "E" and r["series"] == "EQ":
                key = r["tsym"].upper() if r["tsym"].upper() in wanted else r["underlying"].upper()
                if key in wanted:
                    equity.setdefault(key, r["sec_id"])
            elif r["instrument"] == "OPTSTK" and r["underlying"].upper() in wanted:
                u = r["underlying"].upper()
                if r["underlying_id"]:
                    opt_ids[u].add(r["underlying_id"])
                try:
                    lot = int(float(r["lot"]))
                except ValueError:
                    lot = None
                if r["expiry"]:
                    opt_lots[u].setdefault(r["expiry"], lot)

    out: dict[str, Instrument] = {}
    for s in symbols:
        u = s.upper()
        eq, ids, lots = equity.get(u), opt_ids.get(u, set()), opt_lots.get(u, {})
        expiries = sorted(e for e in lots if e >= today.isoformat())
        inst = Instrument(symbol=s, status="OK", equity_id=eq, listed_expiries=expiries)
        if not lots:
            inst.status = "NOT_FOUND" if not eq else "NOT_IN_FNO"
            inst.note = ("symbol not in instrument master" if not eq
                         else "no live OPTSTK contracts; not currently in F&O")
            out[s] = inst
            continue
        inst.fno_underlying_id = sorted(ids)[0] if ids else None
        inst.lot_size = lots[expiries[0]] if expiries else next(iter(lots.values()))
        if inst.fno_underlying_id and eq and inst.fno_underlying_id != eq:
            inst.status = "ID_MISMATCH"
            inst.note = f"equity id {eq} != F&O underlying id {inst.fno_underlying_id}; using F&O id"
        inst.security_id = inst.fno_underlying_id or eq
        if not inst.security_id:
            inst.status, inst.note = "NOT_FOUND", "no underlying security id"
        lot_set = {v for v in lots.values() if v}
        if len(lot_set) > 1:
            inst.note = (inst.note + "; " if inst.note else "") + \
                f"lot sizes differ across live expiries {sorted(lot_set)}; using nearest"
        out[s] = inst
    return out


def master_snapshot(instruments: dict[str, Instrument]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["symbol", "status", "security_id", "equity_id", "fno_underlying_id",
                "lot_size", "listed_expiries", "note"])
    for i in instruments.values():
        w.writerow([i.symbol, i.status, i.security_id or "", i.equity_id or "",
                    i.fno_underlying_id or "", i.lot_size or "", " ".join(i.listed_expiries),
                    i.note])
    return buf.getvalue()
