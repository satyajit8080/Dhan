"""Bull50 NSE historical stock-options collector (research/backtesting only, read-only).

  python -m collector.collect run              # fetch (resumable) + build exports + zip
  python -m collector.collect fetch            # only download raw responses
  python -m collector.collect build            # only rebuild CSV/ZIP/report from raw

Never places orders. Never prints the token.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import zipfile
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from . import config
from .assemble import build_symbol, dedupe_rows, trading_days
from .client import AuthError, DhanClient, QuotaExceeded
from .credentials import CredentialError, load_credentials, redact
from .expiry import ExpiryMapper, parse_holidays
from .instruments import Instrument, download_master, master_snapshot, resolve
from .store import RawStore, leg_name


def log(msg: str) -> None:
    print(redact(msg), flush=True)


def windows(frm: date, to: date) -> list[tuple[str, str]]:
    out, d = [], frm
    while d < to:
        e = min(d + timedelta(days=config.WINDOW_DAYS), to)
        out.append((d.isoformat(), e.isoformat()))
        d = e
    return out


def request_body(security_id: str, offset: int, opt: str, frm: str, to: str, code: int) -> dict:
    return {"exchangeSegment": config.EXCHANGE_SEGMENT, "interval": str(config.INTERVAL),
            "securityId": str(security_id), "instrument": config.INSTRUMENT,
            "expiryFlag": config.EXPIRY_FLAG, "expiryCode": code,
            "strike": "ATM" if offset == 0 else f"ATM{offset:+d}", "drvOptionType": opt,
            "requiredData": config.REQUIRED_DATA, "fromDate": frm, "toDate": to}


def legs():
    for off in config.OFFSETS:
        for opt, side in config.OPTION_TYPES:
            yield off, opt, side


def read_symbols(args) -> list[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    text = Path(args.symbols_file).read_text(encoding="utf-8")
    seen, out = set(), []
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ------------------------------------------------------------------ instruments
def load_instruments(args, symbols: list[str], data_dir: Path, refresh: bool) -> dict[str, Instrument]:
    cache = data_dir / "instruments" / "resolved.json"
    if cache.exists() and not refresh:
        saved = json.loads(cache.read_text())
        if all(s in saved for s in symbols):
            return {s: Instrument(**saved[s]) for s in symbols}
    master = Path(args.master) if args.master else download_master(data_dir / "instruments")
    log(f"instrument master: {master}")
    inst = resolve(symbols, master)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({s: asdict(i) for s, i in inst.items()}, indent=2))
    (data_dir / "instruments" / "resolved.csv").write_text(master_snapshot(inst), encoding="utf-8")
    return inst


# ------------------------------------------------------------------ fetch
def fetch(args, symbols, instruments, store: RawStore, wins) -> bool:
    creds = load_credentials()
    log(f"credentials: token {creds.fingerprint}, client {creds.client_id}")
    client = DhanClient(creds, rps=args.rps, daily_quota=args.max_requests)
    usable = [s for s in symbols if instruments[s].security_id]
    total = len(usable) * len(wins) * 14
    done = sum(store.has(s, a, b, o, sd) for s in usable for a, b in wins for o, _, sd in legs())
    if args.retry_empty:
        for s in usable:
            for a, b in wins:
                for o, _, sd in legs():
                    rec = store.get(s, a, b, o, sd)
                    if rec and rec.get("status") == "no_data":
                        store.drop(s, a, b, o, sd)
                        done -= 1
    log(f"requests: {total} total, {done} already stored, {total - done} to fetch "
        f"(~{(total - done) / max(args.rps, 0.1) / 3600:.1f} h at {args.rps}/s)")
    t0, fetched, failed = time.monotonic(), 0, 0
    try:
        for si, s in enumerate(usable, 1):
            sec = instruments[s].security_id
            for a, b in wins:
                for off, opt, side in legs():
                    key = (s, a, b, off, side)
                    if store.has(*key):
                        continue
                    body = request_body(sec, off, opt, a, b, args.expiry_code)
                    res = client.rolling_option(body)
                    if res.status in ("ok", "no_data"):
                        store.put(key, body, res.status, res.http_status, res.payload, res.attempts)
                    else:
                        failed += 1
                        store.log_error(s, key, body, res.error, res.http_status)
                    fetched += 1
                    done += 1
                    if fetched % 50 == 0 or done == total:
                        rate = fetched / max(time.monotonic() - t0, 1e-6)
                        eta = (total - done) / rate if rate else 0
                        log(f"[{100 * done / total:5.1f}%] {done}/{total} | {s} ({si}/{len(usable)}) "
                            f"{a} | {rate:.1f} req/s | failed {failed} | ETA {eta / 3600:.1f} h")
            log(f"  {s}: complete")
    except AuthError as e:
        log(f"STOPPED: {e}")
        return False
    except QuotaExceeded as e:
        log(f"STOPPED: {e}. Re-run tomorrow; it resumes where it stopped.")
        return False
    except KeyboardInterrupt:
        log("interrupted — everything fetched so far is saved; re-run to resume")
        return False
    log(f"fetch finished: {fetched} requests this run, {failed} failed "
        f"(see {store.errors}; they are retried on the next run)")
    return True


# ------------------------------------------------------------------ build
def last_errors(store: RawStore) -> dict:
    out = {}
    if store.errors.exists():
        for line in store.errors.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                out[(r["symbol"], r["window"], r["leg"])] = r.get("error", "")
            except (ValueError, KeyError):
                continue
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(config.CSV_COLUMNS)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in config.CSV_COLUMNS])


def build(args, symbols, instruments, store: RawStore, wins, frm: date, to: date) -> Path:
    from . import report

    out_dir = Path(args.data_dir) / "exports" / f"{frm}_{to}"
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    errs = last_errors(store)

    log("build: pass 1 — market days")
    sym_days: dict[str, set] = {}
    pending: dict[str, list] = {}
    for s in symbols:
        recs = [store.get(s, a, b, 0, sd) for a, b in wins for sd in ("ce", "pe")]
        sym_days[s] = trading_days(r for r in recs if r)
        if instruments[s].security_id:
            for a, b in wins:
                for off, _, sd in legs():
                    if not store.has(s, a, b, off, sd):
                        w = f"{a}_{b}"
                        pending.setdefault(s, []).append(
                            (w, leg_name(off, sd), errs.get((s, w, leg_name(off, sd)), "not fetched yet")))
    global_days = set().union(*sym_days.values()) if sym_days else set()
    holidays = parse_holidays(Path(args.holidays).read_text()) if args.holidays else set()
    mapper = ExpiryMapper(global_days, holidays, args.expiry_code)

    log("build: pass 2 — snapshots")
    stats, combined, dropped = {}, [], 0
    for i, s in enumerate(symbols, 1):
        rows, st = build_symbol(s, store.iter_symbol(s), mapper, instruments[s].lot_size,
                                args.tolerance)
        rows, d = dedupe_rows(rows)
        dropped += d
        rows.sort(key=lambda r: (r["date"], r["time"], r["strike"]))
        stats[s] = st
        if rows:
            write_csv(csv_dir / f"{RawStore._safe(s)}.csv", rows)
        combined.extend(rows)
        log(f"  [{i}/{len(symbols)}] {s}: {len(rows)} rows, {st.days_with_data} days")
    write_csv(out_dir / "all_symbols.csv", combined)

    listed = {s: mapper.check_listed(instruments[s].listed_expiries) for s in symbols}
    n_missing = report.write_missing_csv(out_dir / "missing_data.csv", stats, global_days,
                                         sym_days, pending)
    (out_dir / "instruments.csv").write_text(master_snapshot(instruments), encoding="utf-8")
    files = ["all_symbols.csv", "csv/<SYMBOL>.csv", "missing_data.csv", "instruments.csv",
             "summary.json", "validation_report.md"]
    summary = report.write_report(out_dir / "validation_report.md", frm=frm, to=to,
                                  instruments=instruments, stats=stats, global_days=global_days,
                                  sym_days=sym_days, pending=pending,
                                  expiry_adjusted=mapper.adjusted, listed_check=listed,
                                  dedup_dropped=dropped, tolerance=args.tolerance, files=files)
    summary["missing_data_rows"] = n_missing
    report.write_json(out_dir / "summary.json", summary)

    zpath = Path(args.data_dir) / "exports" / f"bull50_stock_options_{frm}_{to}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out_dir.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out_dir))
    log(f"build: {len(combined)} rows, {n_missing} missing-data entries")
    log(f"report: {out_dir / 'validation_report.md'}")
    log(f"zip:    {zpath}")
    return zpath


# ------------------------------------------------------------------ CLI
def parse_args(argv=None):
    today = date.today()
    p = argparse.ArgumentParser(prog="python -m collector.collect", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="run", choices=["run", "fetch", "build"])
    p.add_argument("--from", dest="frm", type=date.fromisoformat,
                   default=today.replace(year=today.year - config.MAX_YEARS) + timedelta(days=1),
                   help="start date YYYY-MM-DD (default: 5 years back)")
    p.add_argument("--to", type=date.fromisoformat, default=today,
                   help="end date YYYY-MM-DD, exclusive (default: today)")
    p.add_argument("--symbols", help="comma list; overrides --symbols-file")
    p.add_argument("--symbols-file", default=str(config.DEFAULT_SYMBOLS_FILE))
    p.add_argument("--data-dir", default=str(config.DEFAULT_DATA_DIR))
    p.add_argument("--rps", type=float, default=config.DEFAULT_RPS, help="requests per second")
    p.add_argument("--max-requests", type=int, default=config.DEFAULT_DAILY_QUOTA,
                   help="stop after this many API calls (daily budget)")
    p.add_argument("--expiry-code", type=int, choices=[1, 2, 3], default=1,
                   help="1 = near month (default), 2 = next, 3 = far")
    p.add_argument("--tolerance", type=int, default=config.DEFAULT_TOLERANCE_MIN,
                   help="minutes either side of 09:30/15:20 to look for a bar (0 = exact only)")
    p.add_argument("--holidays", help="optional file of NSE holiday dates YYYY-MM-DD")
    p.add_argument("--master", help="use this local instrument-master CSV instead of downloading")
    p.add_argument("--refresh-instruments", action="store_true")
    p.add_argument("--retry-empty", action="store_true",
                   help="re-request windows Dhan previously answered with no data")
    a = p.parse_args(argv)
    if a.frm >= a.to:
        p.error("--from must be before --to")
    oldest = today.replace(year=today.year - config.MAX_YEARS)
    if a.frm < oldest:
        log(f"note: Dhan keeps about {config.MAX_YEARS} years; data before {oldest} may be empty")
    return a


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)
    store = RawStore(data_dir)
    symbols = read_symbols(args)
    wins = windows(args.frm, args.to)
    log(f"Bull50 options collector | {len(symbols)} symbols | {args.frm} -> {args.to} | "
        f"{len(wins)} x {config.WINDOW_DAYS}-day windows | data dir {data_dir}")
    try:
        instruments = load_instruments(args, symbols, data_dir,
                                       args.refresh_instruments or args.command != "build")
    except Exception as e:
        log(f"ERROR resolving instruments: {e}")
        return 2
    bad = [f"{s} ({i.status})" for s, i in instruments.items() if i.status not in ("OK", "ID_MISMATCH")]
    if bad:
        log("skipped (not resolvable / not in F&O now): " + ", ".join(bad))
    ok = True
    if args.command in ("run", "fetch"):
        try:
            ok = fetch(args, symbols, instruments, store, wins)
        except CredentialError as e:
            log(f"ERROR: {e}")
            return 2
    if args.command in ("run", "build"):
        build(args, symbols, instruments, store, wins, args.frm, args.to)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
