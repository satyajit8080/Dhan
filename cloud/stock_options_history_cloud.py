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
START_AT_DATE = ""                # resume: with START_AT_SYMBOL, skip that symbol's windows before this date
STRIKES = ["ATM-3", "ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2", "ATM+3"]   # Dhan max for stocks: +/-3
EXPIRY_CODE = 1                   # 1 = near month (0 is refused by Dhan)
INTERVAL = 1                      # minutes
KEEP = "SNAPSHOTS"                # "SNAPSHOTS": per leg and day keep only the LAST bar at or before each
                                  # snapshot time, at most ASOF_WINDOW_MIN old (values untouched; stock
                                  # options do not trade every minute). "FULL": every 1-min bar (huge log)
SNAPSHOT_TIMES = ["09:30", "15:20"]
ASOF_WINDOW_MIN = 15
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
VERSION = "1.4"
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
                m = re.match(r"^(.+?)-[A-Za-z]{3}\d{4}-", sym)
                u = m.group(1) if m else sym.split("-")[0]
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
        """Returns (status, http_status, error_code, error_message, data_or_None, attempts)."""
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
            ecode, emsg = "", ""
            if isinstance(parsed, dict):
                ecode = str(parsed.get("errorCode") or parsed.get("internalErrorCode") or "").upper()
                emsg = REDACT(str(parsed.get("errorMessage") or parsed.get("internalErrorMessage")
                                  or ""))[:300]
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
                return "no_data", http_status, ecode, emsg, None, attempt
            if (http_status or 0) >= 400:
                return "failed", http_status, ecode, emsg, None, attempt
            payload = parsed.get("data") if isinstance(parsed, dict) and "data" in parsed else parsed
            if not isinstance(payload, dict):
                return "failed", http_status, "BAD_ENVELOPE", "", None, attempt
            side = payload.get("ce") or payload.get("pe") or {}
            ok = isinstance(side, dict) and bool(side.get("timestamp"))
            return ("ok" if ok else "no_data"), http_status, ecode, "", payload, attempt
        return "failed", http_status, err or ecode, "", None, 6


# Start-up probe. Dhan answered HTTP 400 DH-905 ("Missing required fields, bad values for
# parameters") to 30-day windows in every field/interval format, so the probe now also
# tries shorter windows (the SENSEX history run uses 1-day windows) and both toDate
# conventions, and checks the account with the SENSEX request first.
SENSEX_FIELDS = ["open", "high", "low", "close", "oi", "spot", "strike"]   # SENSEX run's fields
PROBE_DAYS = [30, 10, 5, 2, 1]


def make_body(fmt, sec, strike, opt, a, b, segment="NSE_FNO", instrument="OPTSTK", flag="MONTH"):
    return {"securityId": int(sec) if fmt.get("secIdInt") else str(sec),
            "exchangeSegment": segment, "instrument": instrument, "expiryFlag": flag,
            "expiryCode": int(EXPIRY_CODE), "strike": strike, "drvOptionType": opt,
            "requiredData": list(fmt["fields"]), "fromDate": a.isoformat(), "toDate": b.isoformat(),
            "interval": int(INTERVAL) if fmt.get("intInterval", True) else str(INTERVAL)}


def window_end(a, days, inclusive):
    return a + timedelta(days=days - 1) if inclusive else a + timedelta(days=days)


def _try(client, label, body):
    """Returns (status, set of IST dates that came back)."""
    st, http_status, ecode, emsg, payload, att = client.rolling(body)
    bars, days = 0, set()
    for side in ("ce", "pe"):
        leg = payload.get(side) if isinstance(payload, dict) else None
        if isinstance(leg, dict):
            ts = leg.get("timestamp") or []
            bars += len(ts)
            days |= {datetime.fromtimestamp(int(t), IST).date().isoformat() for t in ts}
    log("info", "probe", test=label, fromDate=body["fromDate"], toDate=body["toDate"],
        status=st, httpStatus=http_status, errorCode=ecode, errorMessage=emsg, bars=bars,
        datesReturned=sorted(days))
    return st, days


def probe(client, sec, sym, frm, to):
    """Find a request shape Dhan accepts for stock options. Returns a format dict or None."""
    ref = to - timedelta(days=10)
    while ref.weekday() >= 5:
        ref -= timedelta(days=1)
    if ref < frm:
        ref = frm
    base = {"intInterval": True, "fields": SENSEX_FIELDS, "secIdInt": False}
    # A. control: exactly the SENSEX history request shape (1 day, from == to)
    _try(client, "control SENSEX 51 OPTIDX WEEK 1-day",
         make_body(base, 51, "ATM", "CALL", ref, ref, "BSE_FNO", "OPTIDX", "WEEK"))
    # B. stock, window length x toDate convention (x securityId type as a last resort)
    found, empty = None, None
    for sec_int in (False, True):
        for days in (PROBE_DAYS if not sec_int else [30, 1]):
            for inclusive in (False, True):
                fmt = dict(base, secIdInt=sec_int, days=days, inclusive=inclusive)
                a = ref - timedelta(days=days - 1)
                b = window_end(a, days, inclusive)
                label = "%s %d-day toDate-%s secId-%s" % (sym, days, "inclusive" if inclusive
                                                         else "exclusive", "int" if sec_int else "str")
                st, got = _try(client, label, make_body(fmt, sec, "ATM", "CALL", a, b))
                if st == "ok":
                    # Step = days really covered. If an "inclusive" window did not return its
                    # last day, toDate is exclusive after all: overlap windows by one day so no
                    # day is skipped (overlapping bars are dropped later as duplicates).
                    step = days if (not inclusive or b.isoformat() in got) else days - 1
                    if step < 1:
                        continue
                    found = dict(fmt, step=step)
                    break
                if st == "no_data" and empty is None:
                    empty = dict(fmt, step=days - 1 if inclusive and days > 1 else 1)
            if found:
                break
        if found:
            break
    fmt = found or empty
    if fmt is None:
        return None
    # C. can we also get iv and volume?
    a = ref - timedelta(days=fmt["days"] - 1)
    b = window_end(a, fmt["days"], fmt["inclusive"])
    for fields in (FIELDS, [f for f in FIELDS if f != "iv"], [f for f in FIELDS if f != "volume"]):
        trial = dict(fmt, fields=fields)
        if _try(client, "fields " + ",".join(fields), make_body(trial, sec, "ATM", "CALL", a, b))[0] == "ok":
            fmt = trial
            break
    fmt["name"] = "%d-day/step-%d/toDate-%s/secId-%s/%s" % (
        fmt["days"], fmt["step"], "inclusive" if fmt["inclusive"] else "exclusive",
        "int" if fmt["secIdInt"] else "str", ",".join(fmt["fields"]))
    return fmt


def trim(leg, targets):
    """Per IST day and snapshot, keep the last bar in [target - ASOF_WINDOW_MIN, target].
    Values are copied unchanged."""
    ts = leg.get("timestamp") or []
    best = {}
    for i, t in enumerate(ts):
        t = int(t)
        day, minute = (t + 19800) // 86400, ((t + 19800) % 86400) // 60
        for target in targets:
            if target - ASOF_WINDOW_MIN <= minute <= target:
                if (day, target) not in best or t > int(ts[best[(day, target)]]):
                    best[(day, target)] = i
    idx = sorted(set(best.values()))
    out = {}
    for k, v in leg.items():
        if isinstance(v, list) and len(v) == len(ts):
            out[k] = [v[i] for i in idx]
        else:
            out[k] = v
    return out


def windows(frm, to, days, inclusive, step):
    out, d = [], frm
    while d < to:
        # toDate really inclusive -> last window ends the day before `to` (to is exclusive);
        # really exclusive (exclusive format, or inclusive-shaped with step < days) -> at `to`.
        really_inclusive = inclusive and step >= days
        e = min(window_end(d, days, inclusive), to - timedelta(days=1) if really_inclusive else to)
        if not (days <= 2 and all((d + timedelta(days=k)).weekday() >= 5
                                  for k in range((e - d).days + (1 if inclusive else 0)))):
            out.append((d, e))
        d = d + timedelta(days=step)
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
    symbols = list(dict.fromkeys(s.strip().upper() for s in SYMBOLS if s.strip()))
    if START_AT_SYMBOL and START_AT_SYMBOL.upper() in symbols:
        symbols = symbols[symbols.index(START_AT_SYMBOL.upper()):]
    keep = []
    for t in SNAPSHOT_TIMES:
        h, m = (int(x) for x in t.split(":"))
        keep.append(h * 60 + m)

    try:
        inst = resolve_instruments(symbols)
    except Exception as e:
        log("error", "instruments_failed", error=type(e).__name__, detail=str(e)[:200])
        return 2
    emit("STKINST", "instruments", inst)
    skipped = ["%s (%s)" % (s, i["status"]) for s, i in inst.items() if not i["security_id"]]
    if skipped:
        log("warn", "symbols_skipped", symbols=skipped)

    s = next((x for x in symbols if inst[x]["security_id"]), None)
    if s is None:
        log("error", "history_stopped", reason="no symbol resolved to a security id")
        return 2
    try:
        fmt = probe(client, inst[s]["security_id"], s, frm, to)
    except Stop as e:
        log("error", "history_stopped", reason=str(e))
        return 3
    if fmt is None:
        log("error", "history_stopped", reason="Dhan refused every request shape in the probe "
            "(see the probe lines above). Nothing else was requested. If the SENSEX control "
            "line also failed, check the Data API plan / account, not this script.")
        return 2
    wins = windows(frm, to, fmt["days"], fmt["inclusive"], fmt["step"])
    missing = [f for f in FIELDS if f not in fmt["fields"]]
    total = sum(1 for x in symbols if inst[x]["security_id"]) * len(wins) * len(STRIKES) * 2
    log("info", "probe_selected", format=fmt["name"], fieldsNotRequested=missing,
        windows=len(wins), requests=total,
        hoursAt4PerS=round(total / 4.0 / 3600, 1), daysAt95kPerDay=round(total / 95000.0, 1),
        note="fields not requested stay blank in the CSV" if missing else "all fields requested")
    meta = {"program": PROGRAM, "version": VERSION, "from": frm.isoformat(), "to": to.isoformat(),
            "windowDays": fmt["days"], "windowStep": fmt["step"], "inclusiveToDate": fmt["inclusive"],
            "windows": [[a.isoformat(), b.isoformat()] for a, b in wins],
            "interval": INTERVAL, "expiryCode": EXPIRY_CODE, "strikes": STRIKES, "keep": KEEP,
            "snapshotTimes": SNAPSHOT_TIMES, "keepToleranceMin": ASOF_WINDOW_MIN, "selection": "asof",
            "symbols": symbols, "format": fmt, "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
            "orders": "NONE"}
    emit("STKMETA", "meta", meta)
    log("info", "history_start", fromDate=frm.isoformat(), toDate=to.isoformat(),
        symbols=len(symbols), windows=len(wins), requests=total)
    done, t0, cur = 0, time.monotonic(), None
    bad_windows = 0
    try:
        for si, s in enumerate(symbols, 1):
            sec = inst[s]["security_id"]
            if not sec:
                continue
            rows = 0
            for a, b in wins:
                if START_AT_DATE and s == START_AT_SYMBOL.upper() and a < date.fromisoformat(START_AT_DATE):
                    continue
                cur = a
                legs = []
                for strike in STRIKES:
                    for opt in ("CALL", "PUT"):
                        body = make_body(fmt, sec, strike, opt, a, b)
                        st, http_status, ecode, emsg, payload, att = client.rolling(body)
                        side = "ce" if opt == "CALL" else "pe"
                        leg = (payload or {}).get(side) if isinstance(payload, dict) else None
                        rec = {"strike": strike, "side": side, "status": st, "httpStatus": http_status,
                               "errorCode": ecode, "errorMessage": emsg, "attempts": att,
                               "request": body}
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
                rejected = [l for l in legs if l["status"] == "failed" and (l["httpStatus"] or 0) < 500]
                bad_windows = bad_windows + 1 if len(rejected) == len(legs) else 0
                if bad_windows >= 3:
                    raise Stop("Dhan rejected every request in 3 windows in a row (%s %s: %s)"
                               % (rejected[0]["httpStatus"], rejected[0]["errorCode"],
                                  rejected[0]["errorMessage"]))
            log("info", "symbol_done", symbol=s, barsKept=rows)
    except Stop as e:
        log("error", "history_stopped", reason=str(e),
            resumeWith="START_AT_SYMBOL = %r and START_AT_DATE = %r" % (s, cur.isoformat() if cur else ""))
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
