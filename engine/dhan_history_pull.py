#!/usr/bin/env python3
"""
dhan_history_pull.py — pull historical stock-option chains from Dhan for backtesting.

Source: Dhan v2 "Expired Options Data"  POST /v2/charts/rollingoption
  * Rolling strikes relative to ATM.  Stock options: ATM-3 .. ATM+3 max
    (Dhan only gives ATM+/-10 for index options), so ATM+/-5 is not possible
    for stocks — this script pulls the full +/-3 (7 strikes x CE/PE).
  * Max 30 days per call, up to 5 years back, intervals 1/5/15/25/60 min.

Output CSV (one row per bar x strike):
  date,time,symbol,expiry,strike,ce_oi,pe_oi,ce_ltp,pe_ltp,spot,
  strike_offset,ce_iv,pe_iv,ce_volume,pe_volume,lot_size

  ltp = bar close.  Default keeps the ~09:30 and ~15:20 IST snapshots per
  day; --all-bars keeps every bar.

Read-only: market data only, never places orders. The token is read from the
environment or a file and never printed or written anywhere.

Usage
  export DHAN_ACCESS_TOKEN=...   DHAN_CLIENT_ID=...
  python3 dhan_history_pull.py --from 2025-11-01 --to 2026-11-01
  python3 dhan_history_pull.py --symbols RELIANCE,TCS --interval 1 --all-bars --zip
  python3 dhan_history_pull.py --dry-run          # show requests, no token needed
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.dhan.co/v2/charts/rollingoption"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
IST = timezone(timedelta(hours=5, minutes=30))
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
MAX_OFFSET_STOCK = 3
CHUNK_DAYS = 30
MIN_GAP_S = 0.25            # data APIs allow ~5 req/s; stay under it
SNAP_TIMES = ("09:30", "15:20")
# NSE moved monthly stock-option expiry from last Thursday to last Tuesday from Sep 2025.
TUESDAY_FROM = date(2025, 9, 1)

# NIFTY 50 constituents (late 2026, best known). Edit or pass --symbols / --symbols-file.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO",
]

OUT_COLS = ["date", "time", "symbol", "expiry", "strike", "ce_oi", "pe_oi", "ce_ltp",
            "pe_ltp", "spot", "strike_offset", "ce_iv", "pe_iv", "ce_volume",
            "pe_volume", "lot_size"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ expiry calendar
def _last_weekday(y: int, m: int, wd: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def monthly_expiry(y: int, m: int, holidays: set[date]) -> date:
    d = _last_weekday(y, m, 3)                      # Thursday
    if d >= TUESDAY_FROM:
        d = _last_weekday(y, m, 1)                  # Tuesday
    while d in holidays or d.weekday() >= 5:        # holiday -> previous trading day
        d -= timedelta(days=1)
    return d


def expiry_for(day: date, code: int, holidays: set[date]) -> date:
    """code 1 = nearest monthly expiry on/after `day`, 2 = next, 3 = far."""
    y, m = day.year, day.month
    if day > monthly_expiry(y, m, holidays):
        y, m = (y + (m == 12), m % 12 + 1)
    for _ in range(code - 1):
        y, m = (y + (m == 12), m % 12 + 1)
    return monthly_expiry(y, m, holidays)


# ------------------------------------------------------------------ scrip master
def load_scrip_master(cache: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Return ({symbol: NSE equity securityId}, {symbol: current option lot size})."""
    path = cache / "api-scrip-master.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        log(f"downloading scrip master -> {path}")
        with urllib.request.urlopen(SCRIP_MASTER, timeout=120) as r:
            path.write_bytes(r.read())
    ids: dict[str, str] = {}
    lots: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            if row.get("SEM_EXM_EXCH_ID") != "NSE":
                continue
            seg, inst = row.get("SEM_SEGMENT"), row.get("SEM_INSTRUMENT_NAME")
            sym = row.get("SEM_TRADING_SYMBOL", "")
            if seg == "E" and row.get("SEM_SERIES") == "EQ":
                ids[sym] = row["SEM_SMST_SECURITY_ID"].strip()
            elif seg == "D" and inst == "OPTSTK":
                under = sym.split("-")[0]
                try:
                    lots.setdefault(under, int(float(row.get("SEM_LOT_UNITS") or 0)))
                except ValueError:
                    pass
    return ids, lots


# ------------------------------------------------------------------ HTTP
class Dhan:
    def __init__(self, token: str, client_id: str):
        self._h = {"access-token": token, "client-id": client_id,
                   "Content-Type": "application/json", "Accept": "application/json"}
        self._last = 0.0
        self.calls = 0

    def post(self, body: dict) -> dict:
        for attempt in range(6):
            gap = MIN_GAP_S - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()
            req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                         headers=self._h, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.calls += 1
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")[:300]
                if e.code in (429, 500, 502, 503, 504) or "DH-904" in txt:
                    wait = 2 ** attempt
                    log(f"  HTTP {e.code}, retry in {wait}s")
                    time.sleep(wait)
                    continue
                if e.code in (401, 403):
                    sys.exit(f"auth failed (HTTP {e.code}) — refresh DHAN_ACCESS_TOKEN")
                log(f"  HTTP {e.code}: {txt}")
                return {}
            except (urllib.error.URLError, TimeoutError) as e:
                wait = 2 ** attempt
                log(f"  network error {e}, retry in {wait}s")
                time.sleep(wait)
        log("  giving up on this request")
        return {}


def body_for(sec_id: str, interval: int, code: int, offset: int, opt: str,
             frm: date, to: date) -> dict:
    strike = "ATM" if offset == 0 else f"ATM{offset:+d}"
    return {"exchangeSegment": "NSE_FNO", "interval": str(interval), "securityId": sec_id,
            "instrument": "OPTSTK", "expiryFlag": "MONTH", "expiryCode": code,
            "strike": strike, "drvOptionType": opt, "requiredData": FIELDS,
            "fromDate": frm.isoformat(), "toDate": to.isoformat()}


def fetch_cached(api: Dhan | None, cache: Path, body: dict) -> dict:
    key = "_".join(str(body[k]) for k in ("securityId", "expiryCode", "interval", "strike",
                                          "drvOptionType", "fromDate", "toDate"))
    path = cache / "raw" / f"{key.replace('+', 'p').replace('-', 'm', 1)}.json"
    if path.exists():
        return json.loads(path.read_text())
    if api is None:
        return {}
    resp = api.post(body)
    if resp:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(resp))
    return resp


def series(resp: dict, side: str) -> list[dict]:
    d = ((resp or {}).get("data") or {}).get(side) or {}
    ts = d.get("timestamp") or []
    out = []
    for i, t in enumerate(ts):
        row = {"ts": int(t)}
        for f in FIELDS:
            arr = d.get(f) or []
            row[f] = arr[i] if i < len(arr) else None
        out.append(row)
    return out


# ------------------------------------------------------------------ assembly
def pick_snapshots(rows: list[dict]) -> list[dict]:
    """Per day, keep the last bar at or before each SNAP_TIME (fallback: first after)."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    keep = []
    for day_rows in by_day.values():
        times = sorted({r["time"] for r in day_rows})
        chosen = set()
        for target in SNAP_TIMES:
            before = [t for t in times if t <= target]
            after = [t for t in times if t > target]
            if before:
                chosen.add(before[-1])
            elif after:
                chosen.add(after[0])
        keep.extend(r for r in day_rows if r["time"] in chosen)
    return keep


def pull_symbol(api, cache, sym, sec_id, lot, args, holidays) -> list[dict]:
    merged: dict[tuple, dict] = {}
    d = args.frm
    while d < args.to:
        end = min(d + timedelta(days=CHUNK_DAYS), args.to)
        for off in range(-args.width, args.width + 1):
            for opt, side, pre in (("CALL", "ce", "ce_"), ("PUT", "pe", "pe_")):
                body = body_for(sec_id, args.interval, args.expiry_code, off, opt, d, end)
                if args.dry_run:
                    print(json.dumps(body))
                    continue
                for b in series(fetch_cached(api, cache, body), side):
                    if b["strike"] is None:
                        continue
                    key = (b["ts"], float(b["strike"]))
                    r = merged.setdefault(key, {"ts": b["ts"], "strike": float(b["strike"]),
                                                "strike_offset": off, "spot": b["spot"]})
                    r[pre + "ltp"] = b["close"]
                    r[pre + "oi"] = b["oi"]
                    r[pre + "iv"] = b["iv"]
                    r[pre + "volume"] = b["volume"]
                    if r.get("spot") is None:
                        r["spot"] = b["spot"]
        d = end
    rows = []
    for r in merged.values():
        dt = datetime.fromtimestamp(r["ts"], IST)
        r["date"], r["time"] = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        r["symbol"] = sym
        r["expiry"] = expiry_for(dt.date(), args.expiry_code, holidays).isoformat()
        r["lot_size"] = lot or ""
        s = r["strike"]
        r["strike"] = int(s) if s == int(s) else s
        rows.append(r)
    if not args.all_bars:
        rows = pick_snapshots(rows)
    rows.sort(key=lambda r: (r["ts"], r["strike"]))
    return rows


def write_rows(writer, rows):
    for r in rows:
        writer.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in OUT_COLS})


def main(argv=None) -> int:
    today = datetime.now(IST).date()
    p = argparse.ArgumentParser(description="Pull Dhan historical stock-option chains to CSV.")
    p.add_argument("--from", dest="frm", type=date.fromisoformat,
                   default=today - timedelta(days=365), help="YYYY-MM-DD (default: 1y ago)")
    p.add_argument("--to", type=date.fromisoformat, default=today,
                   help="YYYY-MM-DD, non-inclusive (default: today)")
    p.add_argument("--symbols", help="comma list, e.g. RELIANCE,TCS (default: NIFTY 50)")
    p.add_argument("--symbols-file", help="text file, one symbol per line")
    p.add_argument("--interval", type=int, choices=[1, 5, 15, 25, 60], default=5)
    p.add_argument("--width", type=int, default=MAX_OFFSET_STOCK,
                   help="strikes each side of ATM (max 3 for stocks)")
    p.add_argument("--expiry-code", type=int, choices=[1, 2, 3], default=1,
                   help="1=near month, 2=next, 3=far")
    p.add_argument("--all-bars", action="store_true", help="keep every bar, not just 09:30/15:20")
    p.add_argument("--holidays", help="file of YYYY-MM-DD NSE holidays (shifts expiry dates)")
    p.add_argument("--out", default="stock_options_history.csv")
    p.add_argument("--zip", action="store_true", help="also write <out>.zip")
    p.add_argument("--per-symbol", action="store_true", help="also write one CSV per symbol")
    p.add_argument("--cache", default=".dhan_cache", help="raw-response cache dir (resumable)")
    p.add_argument("--token-file", help="file holding the access token (else DHAN_ACCESS_TOKEN)")
    p.add_argument("--dry-run", action="store_true", help="print request bodies, no network")
    args = p.parse_args(argv)

    if args.width > MAX_OFFSET_STOCK:
        log(f"Dhan allows ATM+/-{MAX_OFFSET_STOCK} for stock options; using {MAX_OFFSET_STOCK}")
        args.width = MAX_OFFSET_STOCK
    if args.frm >= args.to:
        p.error("--from must be before --to")

    if args.symbols_file:
        symbols = [s.strip() for s in Path(args.symbols_file).read_text().split() if s.strip()]
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = NIFTY50
    holidays = set()
    if args.holidays:
        holidays = {date.fromisoformat(x.strip()) for x in Path(args.holidays).read_text().split()
                    if x.strip()}

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        ids = {s: f"<{s}_secid>" for s in symbols}
        lots: dict[str, int] = {}
        api = None
    else:
        token = (Path(args.token_file).read_text().strip() if args.token_file
                 else os.environ.get("DHAN_ACCESS_TOKEN", "").strip())
        client_id = os.environ.get("DHAN_CLIENT_ID", "").strip()
        if not token or not client_id:
            sys.exit("set DHAN_ACCESS_TOKEN (or --token-file) and DHAN_CLIENT_ID")
        log(f"token ...{token[-4:]}  client {client_id}")
        ids, lots = load_scrip_master(cache)
        api = Dhan(token, client_id)

    n_windows = -(-(args.to - args.frm).days // CHUNK_DAYS)
    log(f"{len(symbols)} symbols x {n_windows} windows x {2 * (2 * args.width + 1)} legs "
        f"= {len(symbols) * n_windows * 2 * (2 * args.width + 1)} calls (cached calls skipped)")

    out = Path(args.out)
    total, missing = 0, []
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        for i, sym in enumerate(symbols, 1):
            sec = ids.get(sym)
            if not sec:
                log(f"[{i}/{len(symbols)}] {sym}: not in scrip master, skipped")
                missing.append(sym)
                continue
            log(f"[{i}/{len(symbols)}] {sym} (secId {sec}, lot {lots.get(sym, '?')})")
            rows = pull_symbol(api, cache, sym, sec, lots.get(sym), args, holidays)
            if args.dry_run:
                continue
            write_rows(w, rows)
            f.flush()
            total += len(rows)
            log(f"    {len(rows)} rows")
            if args.per_symbol and rows:
                d = out.parent / "by_symbol"
                d.mkdir(exist_ok=True)
                with (d / f"{sym.replace('&', '_')}.csv").open("w", newline="") as g:
                    gw = csv.DictWriter(g, fieldnames=OUT_COLS)
                    gw.writeheader()
                    write_rows(gw, rows)

    if args.dry_run:
        return 0
    if args.zip:
        z = out.with_suffix(out.suffix + ".zip")
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(out, out.name)
        log(f"zip -> {z}")
    log(f"done: {total} rows -> {out}  ({api.calls} API calls)")
    if missing:
        log(f"not found (check symbol): {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
