"""
NSE stock-options HISTORY collector for Dhan Cloud - SINGLE FILE.
Paste into Dhan Cloud main.py. Configure the CONFIG block below.

READ-ONLY: calls only POST /v2/charts/rollingoption (expired options data) and the
public instrument master. No order, position, login or token-renewal logic.

Output goes to the run log as compressed lines:
  BX|STKMETA|...            run settings
  BX|STKINST|...            resolved security ids / lot sizes / live expiries
  BX|STK|<id>|i/n|<hex>     one record per stock per 30-day window (14 legs)
Download the log and run on your PC:
  python -m collector.decode_cloud_log <logfile>
which rebuilds the CSVs, ZIP and validation report (see collector/README.md).
"""

# ============================== CONFIG ======================================
FROM_DATE = ""                    # "YYYY-MM-DD"; blank = 5 years back (Dhan's limit)
TO_DATE = ""                      # "YYYY-MM-DD", exclusive; blank = today
SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TMPV", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO",
    "INDUSINDBK", "HEROMOTOCO", "BANKBARODA", "PNB", "CANBK", "TATAPOWER", "DLF",
    "HAL", "VEDL", "TVSMOTOR",
]
START_AT_SYMBOL = ""              # resume: skip symbols before this one (blank = start at the first)
STRIKES = ["ATM-3", "ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2", "ATM+3"]   # Dhan max for stocks: +/-3
EXPIRY_CODE = 1                   # 1 = near month (0 is refused by Dhan)
INTERVAL = 1                      # minutes
WINDOW_DAYS = 30                  # Dhan: at most 30 days per request
KEEP = "SNAPSHOTS"                # "SNAPSHOTS": keep only bars within KEEP_TOLERANCE_MIN of 09:30/15:20
                                  # (values untouched, ~40x smaller log); "FULL": every 1-min bar
SNAPSHOT_TIMES = ["09:30", "15:20"]
KEEP_TOLERANCE_MIN = 2
REQUESTS_PER_SECOND = 4           # Dhan data APIs allow ~5/s
MAX_REQUESTS = 95000              # stop after this many calls (Dhan ~100k/day)
RECORD_CHUNK = 3000               # characters per log line

# Credentials: Dhan Cloud replaces these two placeholders at run time with the
# MANAGED credentials of your logged-in account. Add no Env Variables.
# NEVER put real values in this file.
CLIENT_ID = "{{CLIENT_ID}}"
ACCESS_TOKEN = "{{ACCESS_TOKEN}}"
# ============================================================================

import csv
import gzip
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

PROGRAM = "stock-options-history"
VERSION = "1.0"
BASE_URL = "https://api.dhan.co/v2"
ROLLING_PATH = "/charts/rollingoption"
INSTRUMENT_FILE = "https://images.dhan.co/api-data/api-scrip-master.csv"
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
IST = timezone(timedelta(hours=5, minutes=30))
AUTH_CODES = {"DH-901", "807", "808", "809", "810"}
PLAN_CODES = {"DH-902", "806"}
THROTTLE_CODES = {"DH-904", "805"}
RETRY_CODES = {"800", "DH-908", "DH-909"}
NO_DATA_CODES = {"DH-907"}
_JWT = r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"


class Stop(Exception):
    pass


class Redactor:
    def __init__(self):
        self._secrets = set()

    def register(self, *values):
        for v in values:
            if v and len(v) >= 4:
                self._secrets.add(v)

    def __call__(self, text):
        s = text if isinstance(text, str) else str(text)
        for v in sorted(self._secrets, key=len, reverse=True):
            s = s.replace(v, "[REDACTED]")
        return re.sub(_JWT, "[REDACTED_JWT]", s)


REDACT = Redactor()


def line(text):
    sys.stdout.write(REDACT(text) + "\n")
    sys.stdout.flush()


def log(level, event, **fields):
    rec = {"ts": datetime.now(IST).isoformat(timespec="seconds"), "level": level, "event": event}
    rec.update(fields)
    line("BX|" + json.dumps(rec, default=str, ensure_ascii=False))


def emit(kind, rec_id, payload):
    blob = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hex()
    parts = [blob[i:i + RECORD_CHUNK] for i in range(0, len(blob), RECORD_CHUNK)] or [""]
    for i, part in enumerate(parts, 1):
        line("BX|%s|%s|%d/%d|%s" % (kind, rec_id, i, len(parts), part))


def _filled(v):
    return bool(v) and not (v.startswith("{" + "{") and v.endswith("}" + "}"))


# ------------------------------------------------------------------ instruments
def resolve_instruments(symbols):
    wanted = set(symbols)
    eq, lots = {}, {}
    req = urllib.request.Request(INSTRUMENT_FILE, method="GET", headers={"User-Agent": PROGRAM})
    with urllib.request.urlopen(req, timeout=180) as resp:
        header = None
        for raw in resp:
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if header is None:
                header = next(csv.reader([text]))
                continue
            if "NSE" not in text:
                continue
            row = dict(zip(header, next(csv.reader([text]))))
            if row.get("SEM_EXM_EXCH_ID") != "NSE":
                continue
            sym = row.get("SEM_TRADING_SYMBOL", "")
            if row.get("SEM_SEGMENT") == "E" and row.get("SEM_SERIES") == "EQ" and sym in wanted:
                eq.setdefault(sym, row.get("SEM_SMST_SECURITY_ID", "").strip())
            elif row.get("SEM_INSTRUMENT_NAME") == "OPTSTK":
                u = sym.split("-")[0]
                if u in wanted:
                    exp = (row.get("SEM_EXPIRY_DATE") or "")[:10]
                    try:
                        lot = int(float(row.get("SEM_LOT_UNITS") or 0)) or None
                    except ValueError:
                        lot = None
                    lots.setdefault(u, {}).setdefault(exp, lot)
    today = datetime.now(IST).date().isoformat()
    out = {}
    for s in symbols:
        exps = sorted(e for e in lots.get(s, {}) if e and e >= today)
        inst = {"symbol": s, "status": "OK", "security_id": eq.get(s), "equity_id": eq.get(s),
                "fno_underlying_id": None, "lot_size": None, "listed_expiries": exps, "note": ""}
        if s not in lots:
            inst["status"] = "NOT_FOUND" if not eq.get(s) else "NOT_IN_FNO"
            inst["note"] = "symbol not in instrument master" if not eq.get(s) else \
                "no live OPTSTK contracts; not currently in F&O"
            inst["security_id"] = None
        elif not eq.get(s):
            inst["status"], inst["note"] = "NOT_FOUND", "no NSE equity row for the underlying id"
        else:
            inst["lot_size"] = lots[s][exps[0]] if exps else next(iter(lots[s].values()))
            distinct = sorted({v for v in lots[s].values() if v})
            if len(distinct) > 1:
                inst["note"] = "lot sizes differ across live expiries %s; using nearest" % distinct
        out[s] = inst
    return out


# ------------------------------------------------------------------ client
class Client:
    def __init__(self, client_id, access_token):
        self.headers = {"access-token": access_token, "client-id": client_id,
                        "Content-Type": "application/json", "Accept": "application/json"}
        self.gap = 1.0 / max(REQUESTS_PER_SECOND, 0.1)
        self.last = 0.0
        self.calls = 0

    def rolling(self, body):
        """Returns (status, http_status, error_code, data_or_None, attempts)."""
        data = json.dumps(body).encode()
        err, ecode, http_status = "", "", None
        for attempt in range(1, 7):
            if self.calls >= MAX_REQUESTS:
                raise Stop("MAX_REQUESTS reached (%d)" % MAX_REQUESTS)
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(BASE_URL + ROLLING_PATH, data=data, method="POST",
                                         headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    http_status, raw = r.status, r.read()
            except urllib.error.HTTPError as e:
                http_status, raw = e.status, e.read() or b""
            except Exception as e:
                err, ecode = "network %s" % type(e).__name__, ""
                time.sleep(min(60.0, 2.0 ** attempt) + random.random())
                continue
            try:
                parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except ValueError:
                parsed = {}
            ecode = ""
            if isinstance(parsed, dict):
                ecode = str(parsed.get("errorCode") or parsed.get("internalErrorCode") or "").upper()
            if ecode in PLAN_CODES:
                raise Stop("Data API plan missing (%s): enable the Data API in Dhan web" % ecode)
            if http_status in (401, 403) or ecode in AUTH_CODES:
                raise Stop("Dhan rejected the managed credentials (HTTP %s %s): refresh the "
                           "account connection in Dhan Cloud and run again" % (http_status, ecode))
            if http_status == 429 or ecode in THROTTLE_CODES or (http_status or 0) >= 500 or ecode in RETRY_CODES:
                err = "HTTP %s %s" % (http_status, ecode)
                time.sleep(min(60.0, 2.0 ** attempt) + random.random())
                continue
            if ecode in NO_DATA_CODES:
                return "no_data", http_status, ecode, None, attempt
            if (http_status or 0) >= 400:
                return "failed", http_status, ecode, None, attempt
            payload = parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed
            if not isinstance(payload, dict):
                return "failed", http_status, "BAD_ENVELOPE", None, attempt
            side = payload.get("ce") or payload.get("pe") or {}
            ok = isinstance(side, dict) and bool(side.get("timestamp"))
            return ("ok" if ok else "no_data"), http_status, ecode, payload, attempt
        return "failed", http_status, err or ecode, None, 6


def trim(leg, keep_minutes):
    """Keep only bars at the snapshot minutes. Values are copied unchanged."""
    ts = leg.get("timestamp") or []
    idx = [i for i, t in enumerate(ts) if (((int(t) + 19800) % 86400) // 60) in keep_minutes]
    out = {}
    for k, v in leg.items():
        if isinstance(v, list) and len(v) == len(ts):
            out[k] = [v[i] for i in idx]
        else:
            out[k] = v
    return out


def windows(frm, to):
    out, d = [], frm
    while d < to:
        e = min(d + timedelta(days=WINDOW_DAYS), to)
        out.append((d, e))
        d = e
    return out


def history():
    if not (_filled(CLIENT_ID) and _filled(ACCESS_TOKEN)):
        log("error", "credentials_missing", action="Dhan Cloud did not substitute {{CLIENT_ID}} / "
            "{{ACCESS_TOKEN}}: check the account connection on the Cloud home screen")
        return 3
    REDACT.register(CLIENT_ID.strip(), ACCESS_TOKEN.strip())
    client = Client(CLIENT_ID.strip(), ACCESS_TOKEN.strip())
    today = datetime.now(IST).date()
    to = date.fromisoformat(TO_DATE) if TO_DATE else today
    frm = date.fromisoformat(FROM_DATE) if FROM_DATE else \
        today.replace(year=today.year - 5) + timedelta(days=1)
    wins = windows(frm, to)
    symbols = list(dict.fromkeys(s.strip().upper() for s in SYMBOLS if s.strip()))
    if START_AT_SYMBOL and START_AT_SYMBOL.upper() in symbols:
        symbols = symbols[symbols.index(START_AT_SYMBOL.upper()):]
    keep = set()
    for t in SNAPSHOT_TIMES:
        h, m = (int(x) for x in t.split(":"))
        for d in range(-KEEP_TOLERANCE_MIN, KEEP_TOLERANCE_MIN + 1):
            keep.add(h * 60 + m + d)

    meta = {"program": PROGRAM, "version": VERSION, "from": frm.isoformat(), "to": to.isoformat(),
            "windowDays": WINDOW_DAYS, "interval": INTERVAL, "expiryCode": EXPIRY_CODE,
            "strikes": STRIKES, "keep": KEEP, "snapshotTimes": SNAPSHOT_TIMES,
            "keepToleranceMin": KEEP_TOLERANCE_MIN, "symbols": symbols,
            "python": "%d.%d.%d" % tuple(sys.version_info[:3]), "orders": "NONE"}
    emit("STKMETA", "meta", meta)
    log("info", "history_start", fromDate=frm.isoformat(), toDate=to.isoformat(),
        symbols=len(symbols), windows=len(wins),
        requests=len(symbols) * len(wins) * len(STRIKES) * 2)

    try:
        inst = resolve_instruments(symbols)
    except Exception as e:
        log("error", "instruments_failed", error=type(e).__name__, detail=str(e)[:200])
        return 2
    emit("STKINST", "instruments", inst)
    skipped = ["%s (%s)" % (s, i["status"]) for s, i in inst.items() if not i["security_id"]]
    if skipped:
        log("warn", "symbols_skipped", symbols=skipped)

    total = sum(1 for s in symbols if inst[s]["security_id"]) * len(wins) * len(STRIKES) * 2
    done, t0 = 0, time.monotonic()
    try:
        for si, s in enumerate(symbols, 1):
            sec = inst[s]["security_id"]
            if not sec:
                continue
            rows = 0
            for a, b in wins:
                legs = []
                for strike in STRIKES:
                    for opt in ("CALL", "PUT"):
                        body = {"exchangeSegment": "NSE_FNO", "interval": str(INTERVAL),
                                "securityId": str(sec), "instrument": "OPTSTK",
                                "expiryFlag": "MONTH", "expiryCode": EXPIRY_CODE,
                                "strike": strike, "drvOptionType": opt, "requiredData": FIELDS,
                                "fromDate": a.isoformat(), "toDate": b.isoformat()}
                        st, http_status, ecode, payload, att = client.rolling(body)
                        side = "ce" if opt == "CALL" else "pe"
                        leg = (payload or {}).get(side) if isinstance(payload, dict) else None
                        rec = {"strike": strike, "side": side, "status": st, "httpStatus": http_status,
                               "errorCode": ecode, "attempts": att, "request": body}
                        if isinstance(leg, dict):
                            n = len(leg.get("timestamp") or [])
                            kept = trim(leg, keep) if KEEP == "SNAPSHOTS" else leg
                            rec.update(barsReturned=n, barsKept=len(kept.get("timestamp") or []),
                                       data={side: kept})
                            rows += rec["barsKept"]
                        legs.append(rec)
                        done += 1
                emit("STK", "%s_%s_%s" % (s.replace("&", "_AND_"), a.isoformat(), b.isoformat()),
                     {"symbol": s, "from": a.isoformat(), "to": b.isoformat(), "legs": legs})
                rate = done / max(time.monotonic() - t0, 1e-6)
                log("info", "progress", symbol=s, n="%d/%d" % (si, len(symbols)),
                    window=a.isoformat(), pct=round(100.0 * done / max(total, 1), 1),
                    reqPerS=round(rate, 2), etaMin=round((total - done) / max(rate, 1e-6) / 60, 1),
                    failed=sum(1 for l in legs if l["status"] == "failed"))
            log("info", "symbol_done", symbol=s, barsKept=rows)
    except Stop as e:
        log("error", "history_stopped", reason=str(e), resumeWith="START_AT_SYMBOL = %r" % s)
        return 3
    log("info", "history_done", requests=client.calls, orders="NONE",
        next="download this log, then on your PC: python -m collector.decode_cloud_log <logfile>")
    return 0


def main():
    log("info", "runtime", program=PROGRAM, version=VERSION, endpoint="POST " + ROLLING_PATH,
        orders="NONE (no order functions exist in this program)")
    return history()


if __name__ == "__main__":
    main()
