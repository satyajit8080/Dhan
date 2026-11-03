"""Turn raw rolling-option responses into 09:30 / 15:20 rows.

Rules:
  * Values come straight from Dhan. Nothing is interpolated, carried forward or
    filled: a missing price, OI, IV or volume stays blank and is reported.
  * ltp = the 1-minute bar's close.
  * If the exact snapshot minute has no bar, the nearest bar within the tolerance
    is used and its real time is written in the `time` column (and reported).
  * Duplicates (same symbol, minute, strike, side) are dropped; conflicting
    duplicates keep the first value and are counted.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from . import config
from .expiry import ExpiryMapper

IST_OFFSET_S = 19800
LEG_FIELDS = (("close", "ltp"), ("oi", "oi"), ("iv", "iv"), ("volume", "volume"))


def _minute(ts: int) -> int:
    return ((ts + IST_OFFSET_S) % 86400) // 60


def _day(ts: int) -> date:
    return date.fromordinal(719163 + (ts + IST_OFFSET_S) // 86400)   # 719163 = 1970-01-01


def _target_minutes() -> list[int]:
    return [t.hour * 60 + t.minute for t in config.SNAPSHOTS]


def trading_days(records: Iterable[dict]) -> set[date]:
    days = set()
    for rec in records:
        data = (rec.get("response") or {}).get("data") or {}
        for side in ("ce", "pe"):
            for ts in (data.get(side) or {}).get("timestamp") or []:
                days.add(_day(int(ts)))
    return days


@dataclass
class SymbolStats:
    symbol: str
    requests_ok: int = 0
    requests_no_data: int = 0
    bars_kept: int = 0
    duplicates: int = 0
    conflicts: int = 0
    spot_mismatch: int = 0
    first_day: Optional[str] = None
    last_day: Optional[str] = None
    days_with_data: int = 0
    rows: int = 0
    complete_rows: int = 0
    snapshots_expected: int = 0
    snapshots_found: int = 0
    snapshots_substituted: int = 0
    issues: list = field(default_factory=list)     # (date, snapshot, issue, detail)
    straddles: list = field(default_factory=list)  # (expiry, straddle_pct_of_spot)


def build_symbol(symbol: str, records: Iterable[dict], mapper: ExpiryMapper,
                 lot_size: Optional[int], tolerance_min: int) -> tuple[list[dict], SymbolStats]:
    st = SymbolStats(symbol)
    targets = _target_minutes()
    keep_min = {t + d for t in targets for d in range(-tolerance_min, tolerance_min + 1)}
    bars: dict[tuple[int, float], dict] = {}
    days: set[date] = set()

    for rec in records:
        if rec.get("status") == "no_data":
            st.requests_no_data += 1
            continue
        st.requests_ok += 1
        data = (rec.get("response") or {}).get("data") or {}
        for side in ("ce", "pe"):
            leg = data.get(side) or {}
            ts_list = leg.get("timestamp") or []
            for i, ts in enumerate(ts_list):
                ts = int(ts)
                days.add(_day(ts))
                if _minute(ts) not in keep_min:
                    continue
                k = _val(leg, "strike", i)
                if k is None:
                    continue
                bar = bars.setdefault((ts, float(k)), {})
                if side + "_seen" in bar:
                    same = all(bar.get(f"{side}_{out}") == _val(leg, src, i) for src, out in LEG_FIELDS)
                    st.duplicates += 1
                    st.conflicts += 0 if same else 1
                    continue
                bar[side + "_seen"] = True
                for src, out in LEG_FIELDS:
                    bar[f"{side}_{out}"] = _val(leg, src, i)
                bar[side + "_spot"] = _val(leg, "spot", i)
                st.bars_kept += 1

    if days:
        st.first_day, st.last_day = min(days).isoformat(), max(days).isoformat()
        st.days_with_data = len(days)

    by_day: dict[date, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for (ts, k), bar in bars.items():
        by_day[_day(ts)][_minute(ts)].append((k, ts, bar))

    rows: list[dict] = []
    for d in sorted(days):
        minutes = by_day.get(d, {})
        for t in targets:
            label = f"{t // 60:02d}:{t % 60:02d}"
            st.snapshots_expected += 1
            chosen = _choose(minutes, t, tolerance_min)
            if chosen is None:
                st.issues.append((d.isoformat(), label, "SNAPSHOT_MISSING",
                                  f"no bar within +/-{tolerance_min} min"))
                continue
            st.snapshots_found += 1
            if chosen != t:
                st.snapshots_substituted += 1
                st.issues.append((d.isoformat(), label, "SNAPSHOT_SUBSTITUTED",
                                  f"used {chosen // 60:02d}:{chosen % 60:02d}"))
            strikes = sorted(minutes[chosen], key=lambda x: x[0])
            if len(strikes) < len(config.OFFSETS):
                st.issues.append((d.isoformat(), label, "STRIKES_INCOMPLETE",
                                  f"{len(strikes)}/{len(config.OFFSETS)} strikes"))
            expiry = mapper.expiry_for(d).isoformat()
            snap_rows = []
            for k, ts, bar in strikes:
                row = _row(symbol, d, chosen, expiry, k, bar, lot_size, st, label)
                snap_rows.append(row)
            rows.extend(snap_rows)
            if t == targets[-1] and expiry == d.isoformat():
                pct = _straddle_pct(snap_rows)
                if pct is not None:
                    st.straddles.append((expiry, pct))
    st.rows = len(rows)
    return rows, st


def _val(leg: dict, name: str, i: int):
    arr = leg.get(name)
    if not isinstance(arr, list) or i >= len(arr):
        return None
    return arr[i]


def _choose(minutes: dict, target: int, tol: int) -> Optional[int]:
    if target in minutes:
        return target
    for d in range(1, tol + 1):
        for m in (target - d, target + d):     # earlier bar wins a tie
            if m in minutes:
                return m
    return None


def _num(v):
    if v is None:
        return None
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _row(symbol, d, minute, expiry, k, bar, lot, st: SymbolStats, label) -> dict:
    ce_spot, pe_spot = bar.get("ce_spot"), bar.get("pe_spot")
    if ce_spot is not None and pe_spot is not None and abs(ce_spot - pe_spot) > 0.01:
        st.spot_mismatch += 1
    row = {"date": d.isoformat(), "time": f"{minute // 60:02d}:{minute % 60:02d}",
           "symbol": symbol, "expiry": expiry, "strike": _num(k),
           "spot": ce_spot if ce_spot is not None else pe_spot, "lot_size": lot}
    complete = True
    for side in ("ce", "pe"):
        if not bar.get(side + "_seen"):
            complete = False
            st.issues.append((row["date"], label, f"{side.upper()}_LEG_MISSING", f"strike {row['strike']}"))
        for _, out in LEG_FIELDS:
            v = bar.get(f"{side}_{out}")
            row[f"{side}_{out}"] = _num(v)
            if bar.get(side + "_seen") and v is None:
                complete = False
                st.issues.append((row["date"], label, f"{side.upper()}_{out.upper()}_MISSING",
                                  f"strike {row['strike']}"))
    if row["spot"] is None:
        complete = False
        st.issues.append((row["date"], label, "SPOT_MISSING", f"strike {row['strike']}"))
    st.complete_rows += complete
    return row


def _straddle_pct(rows: list[dict]) -> Optional[float]:
    best = None
    for r in rows:
        if None in (r.get("spot"), r.get("ce_ltp"), r.get("pe_ltp")) or not r["spot"]:
            continue
        dist = abs(r["strike"] - r["spot"])
        if best is None or dist < best[0]:
            best = (dist, r)
    if best is None:
        return None
    r = best[1]
    return round((r["ce_ltp"] + r["pe_ltp"]) / r["spot"] * 100, 3)


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    seen, out, dropped = set(), [], 0
    for r in rows:
        key = (r["symbol"], r["date"], r["time"], r["strike"])
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dropped


def to_datetime(d: str, t: str) -> datetime:
    return datetime.fromisoformat(f"{d}T{t}")
