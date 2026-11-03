"""Dhan Cloud: pull historical stock-option chains (ATM+/-3) and send the zip to Telegram.

Read-only market data. No files, no env access: everything stays in memory.
Set Env Variables DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID, TG_BOT_TOKEN, TG_CHAT_ID.
"""
import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

ACCESS_TOKEN = "{{DHAN_ACCESS_TOKEN}}"
CLIENT_ID = "{{DHAN_CLIENT_ID}}"
TG_BOT_TOKEN = "{{TG_BOT_TOKEN}}"
TG_CHAT_ID = "{{TG_CHAT_ID}}"

FROM_DATE = "2025-11-01"          # inclusive
TO_DATE = "2026-11-01"            # non-inclusive
SYMBOLS = ""                      # "RELIANCE,TCS"; blank = NIFTY 50
INTERVAL = 5                      # 1, 5, 15, 25, 60
WIDTH = 3                         # strikes each side of ATM (Dhan max for stocks: 3)
EXPIRY_CODE = 1                   # 1 near month, 2 next, 3 far
ALL_BARS = False                  # False = only ~09:30 and ~15:20 bars per day
SEND_EVERY = 10                   # send a zip to Telegram after this many symbols
HOLIDAYS = []                     # NSE holidays "YYYY-MM-DD" that moved an expiry

NIFTY50 = ("ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE "
           "BAJAJFINSV BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM "
           "HCLTECH HDFCBANK HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN "
           "JSWSTEEL KOTAKBANK LT M&M MARUTI MAXHEALTH NESTLEIND NTPC ONGC POWERGRID RELIANCE "
           "SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TMPV TATASTEEL TCS TECHM TITAN TRENT "
           "ULTRACEMCO WIPRO").split()

API = "https://api.dhan.co/v2/charts/rollingoption"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
IST = timezone(timedelta(hours=5, minutes=30))
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
COLS = ["date", "time", "symbol", "expiry", "strike", "ce_oi", "pe_oi", "ce_ltp", "pe_ltp",
        "spot", "strike_offset", "ce_iv", "pe_iv", "ce_volume", "pe_volume", "lot_size"]
SNAPS = ("09:30", "15:20")
TUESDAY_FROM = date(2025, 9, 1)   # NSE monthly expiry: last Thu before, last Tue after
_last_call = [0.0]


def log(msg):
    print(msg, flush=True)


def next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def monthly_expiry(y, m):
    ny, nm = next_month(y, m)
    d = date(ny, nm, 1) - timedelta(days=1)
    wd = 3 if d - timedelta(days=(d.weekday() - 3) % 7) < TUESDAY_FROM else 1
    d -= timedelta(days=(d.weekday() - wd) % 7)
    while d.isoformat() in HOLIDAYS or d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def expiry_for(day):
    y, m = day.year, day.month
    if day > monthly_expiry(y, m):
        y, m = next_month(y, m)
    for _ in range(EXPIRY_CODE - 1):
        y, m = next_month(y, m)
    return monthly_expiry(y, m)


def load_scrip_master():
    ids, lots = {}, {}
    with urllib.request.urlopen(SCRIP_MASTER, timeout=180) as r:
        for row in csv.DictReader(io.TextIOWrapper(r, encoding="utf-8", errors="replace")):
            if row.get("SEM_EXM_EXCH_ID") != "NSE":
                continue
            sym = row.get("SEM_TRADING_SYMBOL", "")
            if row.get("SEM_SEGMENT") == "E" and row.get("SEM_SERIES") == "EQ":
                ids[sym] = row["SEM_SMST_SECURITY_ID"].strip()
            elif row.get("SEM_INSTRUMENT_NAME") == "OPTSTK":
                try:
                    lots.setdefault(sym.split("-")[0], int(float(row.get("SEM_LOT_UNITS") or 0)))
                except ValueError:
                    pass
    return ids, lots


def dhan_post(body):
    headers = {"access-token": ACCESS_TOKEN, "client-id": CLIENT_ID,
               "Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(6):
        wait = 0.25 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(API, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:200]
            if e.code in (401, 403):
                raise RuntimeError(f"auth failed (HTTP {e.code}) - refresh DHAN_ACCESS_TOKEN")
            if e.code not in (429, 500, 502, 503, 504) and "DH-904" not in txt:
                log(f"  HTTP {e.code}: {txt}")
                return {}
        except Exception as e:  # network hiccup
            log(f"  network error {type(e).__name__}")
        time.sleep(2 ** attempt)
    return {}


def pull_symbol(sym, sec_id, lot, start, stop):
    merged = {}
    d = start
    while d < stop:
        end = min(d + timedelta(days=30), stop)
        for off in range(-WIDTH, WIDTH + 1):
            for opt, side in (("CALL", "ce"), ("PUT", "pe")):
                body = {"exchangeSegment": "NSE_FNO", "interval": str(INTERVAL),
                        "securityId": sec_id, "instrument": "OPTSTK", "expiryFlag": "MONTH",
                        "expiryCode": EXPIRY_CODE, "strike": "ATM" if off == 0 else f"ATM{off:+d}",
                        "drvOptionType": opt, "requiredData": FIELDS,
                        "fromDate": d.isoformat(), "toDate": end.isoformat()}
                data = ((dhan_post(body).get("data") or {}).get(side)) or {}
                for i, ts in enumerate(data.get("timestamp") or []):
                    col = {f: (data.get(f) or [])[i] if i < len(data.get(f) or []) else None
                           for f in FIELDS}
                    if col["strike"] is None:
                        continue
                    r = merged.setdefault((int(ts), float(col["strike"])),
                                          {"ts": int(ts), "strike": float(col["strike"]),
                                           "strike_offset": off, "spot": col["spot"]})
                    r[side + "_ltp"], r[side + "_oi"] = col["close"], col["oi"]
                    r[side + "_iv"], r[side + "_volume"] = col["iv"], col["volume"]
        d = end
    rows = []
    for r in merged.values():
        dt = datetime.fromtimestamp(r["ts"], IST)
        k = r["strike"]
        r.update(date=dt.strftime("%Y-%m-%d"), time=dt.strftime("%H:%M"), symbol=sym,
                 expiry=expiry_for(dt.date()).isoformat(), lot_size=lot or "",
                 strike=int(k) if k == int(k) else k)
        rows.append(r)
    if not ALL_BARS:
        keep, by_day = set(), {}
        for r in rows:
            by_day.setdefault(r["date"], set()).add(r["time"])
        for day, times in by_day.items():
            times = sorted(times)
            for t in SNAPS:
                before = [x for x in times if x <= t]
                keep.add((day, before[-1] if before else times[0]))
        rows = [r for r in rows if (r["date"], r["time"]) in keep]
    rows.sort(key=lambda r: (r["ts"], r["strike"]))
    return rows


def to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: "" if r.get(c) is None else r.get(c) for c in COLS})
    return buf.getvalue()


def send_zip(name, text, caption):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name + ".csv", text)
    data = raw.getvalue()
    log(f"zip {name}: {len(data) // 1024} KB")
    if TG_BOT_TOKEN.startswith("{{") or TG_CHAT_ID.startswith("{{"):
        log("Telegram not set - printing CSV to log instead")
        log(text)
        return
    b = "----dhanpull%d" % int(time.time())
    head = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT_ID}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}.zip\"\r\n"
            f"Content-Type: application/zip\r\n\r\n")
    body = head.encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument",
                                 data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            log("telegram: sent" if json.loads(r.read()).get("ok") else "telegram: failed")
    except Exception as e:
        log(f"telegram: failed ({type(e).__name__})")


def run():
    if ACCESS_TOKEN.startswith("{{") or CLIENT_ID.startswith("{{"):
        log("Set DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID in Env Variables")
        return
    start, stop = date.fromisoformat(FROM_DATE), date.fromisoformat(TO_DATE)
    symbols = [s.strip().upper() for s in SYMBOLS.split(",") if s.strip()] or NIFTY50
    log(f"token ...{ACCESS_TOKEN[-4:]} | {len(symbols)} symbols | {start} -> {stop}")
    ids, lots = load_scrip_master()
    batch, batch_syms, done, total = [], [], [], 0
    for i, sym in enumerate(symbols, 1):
        if sym not in ids:
            log(f"[{i}/{len(symbols)}] {sym}: not in scrip master, skipped")
            continue
        rows = pull_symbol(sym, ids[sym], lots.get(sym), start, stop)
        log(f"[{i}/{len(symbols)}] {sym}: {len(rows)} rows")
        batch.extend(rows)
        batch_syms.append(sym)
        done.append(sym)
        total += len(rows)
        if len(batch_syms) >= SEND_EVERY or i == len(symbols):
            if batch:
                part = f"stock_options_{start}_{stop}_part{(len(done) - 1) // SEND_EVERY + 1}"
                send_zip(part, to_csv(batch), f"{len(batch)} rows: {', '.join(batch_syms)}")
            batch, batch_syms = [], []
    log(f"done: {total} rows")


if __name__ == "__main__":
    run()
