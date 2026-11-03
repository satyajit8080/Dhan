"""Missing-data report, validation report and run summary."""
from __future__ import annotations

import csv
import json
import statistics
from datetime import date
from pathlib import Path

from . import config
from .assemble import SymbolStats
from .instruments import Instrument


def write_missing_csv(path: Path, stats: dict[str, SymbolStats], global_days: set[date],
                      sym_days: dict[str, set], pending: dict[str, list]) -> int:
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "snapshot", "issue", "detail"])
        for sym, st in stats.items():
            for window, leg, err in pending.get(sym, []):
                w.writerow([sym, window, "", "REQUEST_NOT_FETCHED", f"{leg}: {err}"]); n += 1
            have = sym_days.get(sym, set())
            if have:
                lo, hi = min(have), max(have)
                for d in sorted(x for x in global_days if lo <= x <= hi and x not in have):
                    w.writerow([sym, d.isoformat(), "", "NO_DATA_DAY",
                                "market traded (other symbols have bars) but no bars for this symbol"]); n += 1
            for d, snap, issue, detail in st.issues:
                w.writerow([sym, d, snap, issue, detail]); n += 1
    return n


def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "-"


def write_report(path: Path, *, frm: date, to: date, instruments: dict[str, Instrument],
                 stats: dict[str, SymbolStats], global_days: set[date],
                 sym_days: dict[str, set], pending: dict[str, list],
                 expiry_adjusted: dict, listed_check: dict[str, list], dedup_dropped: int,
                 tolerance: int, files: list[str]) -> dict:
    straddles = [p for st in stats.values() for _, p in st.straddles]
    median_straddle = statistics.median(straddles) if straddles else None
    suspect = [(s, e, p) for s, st in stats.items() for e, p in st.straddles
               if p > config.EXPIRY_STRADDLE_MAX_PCT]
    tot = lambda f: sum(getattr(st, f) for st in stats.values())  # noqa: E731
    n_pending = sum(len(v) for v in pending.values())

    L = []
    L.append("# Bull50 stock-options history — validation report\n")
    L.append(f"Range **{frm} → {to}** (to is exclusive) · 1-min bars · ATM±3 CE/PE · "
             f"snapshots {', '.join(t.strftime('%H:%M') for t in config.SNAPSHOTS)} IST "
             f"(nearest bar within ±{tolerance} min) · source Dhan `/v2/charts/rollingoption`\n")
    L.append("All values are as returned by Dhan. Nothing is interpolated or filled; "
             "blanks in the CSV are data Dhan did not return.\n")
    L.append("## Overall\n")
    L.append("| Metric | Value |\n|---|---|")
    L += [f"| Symbols requested | {len(instruments)} |",
          f"| Symbols with data | {sum(1 for s in stats.values() if s.rows)} |",
          f"| Market days seen (any symbol) | {len(global_days)} |",
          f"| Rows exported | {tot('rows')} |",
          f"| Rows with every field present | {tot('complete_rows')} ({_pct(tot('complete_rows'), tot('rows'))}) |",
          f"| Snapshots found / expected | {tot('snapshots_found')} / {tot('snapshots_expected')} "
          f"({_pct(tot('snapshots_found'), tot('snapshots_expected'))}) |",
          f"| Snapshots using a nearby minute | {tot('snapshots_substituted')} |",
          f"| Requests not yet fetched (failed or pending) | {n_pending} |",
          f"| Duplicate bars dropped | {tot('duplicates')} (conflicting: {tot('conflicts')}) |",
          f"| Duplicate rows dropped at export | {dedup_dropped} |",
          f"| CE/PE spot disagreements | {tot('spot_mismatch')} |", ""]

    L.append("## Expiry mapping\n")
    L.append("Expiry = last Thursday of the month (last Tuesday from Sep 2025), moved back to "
             "the previous market day when no symbol traded on it.\n")
    if median_straddle is None:
        L.append("- Expiry-day check: no expiry-day 15:20 rows yet.")
    else:
        L.append(f"- Expiry-day 15:20 ATM straddle, median {median_straddle:.2f}% of spot over "
                 f"{len(straddles)} symbol-expiries (near 0 means the mapping is right; a "
                 f"next-month mapping would show several %).")
        L.append(f"- Over {config.EXPIRY_STRADDLE_MAX_PCT}%: {len(suspect)}"
                 + ("" if not suspect else " — " + ", ".join(f"{s} {e} ({p}%)" for s, e, p in suspect[:15])))
    if expiry_adjusted:
        L.append("- Moved for holidays: " + ", ".join(f"{a}→{b}" for a, b in sorted(expiry_adjusted.items())))
    bad_listed = {s: v for s, v in listed_check.items() if v}
    L.append("- Live expiries in instrument master vs rule: "
             + ("all match" if not bad_listed else
                "; ".join(f"{s}: {', '.join(v)}" for s, v in bad_listed.items())))
    L.append("")

    L.append("## Instruments and F&O eligibility\n")
    L.append("Current eligibility from the instrument master; historical eligibility from the "
             "first/last day Dhan returned bars.\n")
    L.append("| Symbol | Master | SecId | Lot | Data from | Data to | Days | Rows | Complete | "
             "Snapshots | Missing days | Pending req | Note |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sym, inst in instruments.items():
        st = stats.get(sym) or SymbolStats(sym)
        have = sym_days.get(sym, set())
        missing_days = (sum(1 for x in global_days if have and min(have) <= x <= max(have) and x not in have))
        note = inst.note
        if have and global_days and min(have) > min(global_days):
            note = (note + "; " if note else "") + f"no data before {min(have)} (not in F&O or not covered)"
        L.append(f"| {sym} | {inst.status} | {inst.security_id or ''} | {inst.lot_size or ''} | "
                 f"{st.first_day or ''} | {st.last_day or ''} | {st.days_with_data} | {st.rows} | "
                 f"{_pct(st.complete_rows, st.rows)} | {_pct(st.snapshots_found, st.snapshots_expected)} | "
                 f"{missing_days} | {len(pending.get(sym, []))} | {note} |")
    L.append("")
    L.append("## Caveats\n")
    L += ["- `lot_size` is the current lot size from today's instrument master; NSE revises lot "
          "sizes, so older rows may have traded at a different lot.",
          "- `strike` is the actual strike Dhan reports for each rolling ATM offset at that minute.",
          "- Dhan gives at most ATM±3 for stock options.",
          "- Renamed stocks (e.g. ETERNAL, TMPV) only have history under the security id Dhan maps them to.",
          ""]
    L.append("## Files\n")
    L += [f"- `{f}`" for f in files]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")

    summary = {"from": frm.isoformat(), "to": to.isoformat(), "rows": tot("rows"),
               "complete_rows": tot("complete_rows"), "pending_requests": n_pending,
               "median_expiry_straddle_pct": median_straddle, "suspect_expiries": len(suspect),
               "symbols": {s: {"status": instruments[s].status, "rows": st.rows,
                               "first_day": st.first_day, "last_day": st.last_day}
                           for s, st in stats.items()}}
    return summary


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
