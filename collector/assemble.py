"""Turn raw rolling-option responses into 09:30 / 15:20 rows.

Rules:
  * Values come straight from Dhan. Nothing is interpolated, carried forward or
    filled: a missing price, OI, IV or volume stays blank and is reported.
  * ltp = the 1-minute bar's close.
  * Snapshots are "as of": each leg's last trade at or before 09:30 / 15:20, no older
    than the tolerance (default 15 min). The `time` column is the snapshot time; how
    stale the legs are is reported.
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
    legs_total: int = 0
    legs_lagged: int = 0
    lag_max: int = 0
    issues: list = field(default_factory=list)     # (date, snapshot, issue, detail)
    straddles: list = field(default_factory=list)  # (expiry, straddle_pct_of_spot)


def _offset(rec: dict) -> Optional[int]:
    label = ((rec.get("request") or {}).get("strike") or "")
    if label == "ATM":
        return 0
    try:
        return int(label[3:])
    except ValueError:
        return None


def build_symbol(symbol: str, records: Iterable[dict], mapper: ExpiryMapper,
                 lot_size: Optional[int], tolerance_min: int) -> tuple[list[dict], SymbolStats]:
    """As-of snapshots: for each day, snapshot time, rolling strike offset and side, take the
    LAST bar at or before the snapshot minute, no older than `tolerance_min` minutes. That is
    the price a trader saw at 09:30 / 15:20 (stock options do not trade every minute).
    Values are copied unchanged; nothing is filled. The row's `time` is the snapshot time;
    how stale each leg is goes into the stats."""
    st = SymbolStats(symbol)
    targets = _target_minutes()
    window = {t: (t - tolerance_min, t) for t in targets}
    picks: dict[tuple, tuple] = {}          # (day, target, offset, side) -> (ts, values)
    days: set[date] = set()

    for rec in records:
        if rec.get("status") == "no_data":
            st.requests_no_data += 1
            continue
        st.requests_ok += 1
        off = _offset(rec)
        data = (rec.get("response") or {}).get("data") or {}
        for side in ("ce", "pe"):
            leg = data.get(side) or {}
            for i, ts in enumerate(leg.get("timestamp") or []):
                ts = int(ts)
                d, m = _day(ts), _minute(ts)
                days.add(d)
                for t, (lo, hi) in window.items():
                    if not lo <= m <= hi:
                        continue
                    k = _val(leg, "strike", i)
                    if k is None:
                        continue
                    vals = {"strike": float(k), "spot": _val(leg, "spot", i)}
                    for src, out in LEG_FIELDS:
                        vals[out] = _val(leg, src, i)
                    key = (d, t, off, side)
                    prev = picks.get(key)
                    if prev is not None and prev[0] == ts:
                        st.duplicates += 1
                        st.conflicts += 0 if prev[1] == vals else 1
                        continue
                    st.bars_kept += 1
                    if prev is None or ts > prev[0]:
                        picks[key] = (ts, vals)

    if days:
        st.first_day, st.last_day = min(days).isoformat(), max(days).isoformat()
        st.days_with_data = len(days)

    by_snap: dict[tuple, list] = defaultdict(list)
    for (d, t, off, side), (ts, vals) in picks.items():
        by_snap[(d, t)].append((off, side, ts, vals))

    rows: list[dict] = []
    for d in sorted(days):
        for t in targets:
            label = f"{t // 60:02d}:{t % 60:02d}"
            st.snapshots_expected += 1
            got = by_snap.get((d, t))
            if not got:
                st.issues.append((d.isoformat(), label, "SNAPSHOT_MISSING",
                                  f"no trade in any leg from {tolerance_min} min before to {label}"))
                continue
            st.snapshots_found += 1
            offsets = {off for off, _, _, _ in got}
            if len(offsets) < len(config.OFFSETS):
                st.issues.append((d.isoformat(), label, "STRIKES_INCOMPLETE",
                                  f"{len(offsets)}/{len(config.OFFSETS)} rolling strikes traded"))
            by_strike: dict[float, dict] = {}
            latest = None
            for off, side, ts, vals in got:
                bar = by_strike.setdefault(vals["strike"], {})
                if bar.get(side + "_seen") and bar[side + "_ts"] >= ts:
                    continue                    # same strike reached via two offsets: keep newer
                bar.update({side + "_seen": True, side + "_ts": ts, side + "_spot": vals["spot"]})
                for _, out in LEG_FIELDS:
                    bar[f"{side}_{out}"] = vals[out]
                lag = t - _minute(ts)
                st.legs_total += 1
                if lag:
                    st.legs_lagged += 1
                    st.lag_max = max(st.lag_max, lag)
                if latest is None or ts > latest[0]:
                    latest = (ts, vals["spot"])
            if any(t - _minute(ts) for _, _, ts, _ in got):
                st.snapshots_substituted += 1
            expiry = mapper.expiry_for(d).isoformat()
            snap_rows = []
            for k in sorted(by_strike):
                bar = by_strike[k]
                if latest and latest[1] is not None:
                    bar["spot_latest"] = latest[1]
                snap_rows.append(_row(symbol, d, t, expiry, k, bar, lot_size, st, label))
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


def _num(v):
    if v is None:
        return None
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _row(symbol, d, minute, expiry, k, bar, lot, st: SymbolStats, label) -> dict:
    ce_spot, pe_spot = bar.get("ce_spot"), bar.get("pe_spot")
    if ce_spot is not None and pe_spot is not None and abs(ce_spot - pe_spot) > 0.01:
        st.spot_mismatch += 1
    spot = bar.get("spot_latest")
    if spot is None:
        spot = ce_spot if ce_spot is not None else pe_spot
    row = {"date": d.isoformat(), "time": f"{minute // 60:02d}:{minute % 60:02d}",
           "symbol": symbol, "expiry": expiry, "strike": _num(k),
           "spot": spot, "lot_size": lot}
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
