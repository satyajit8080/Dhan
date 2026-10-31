"""
Fetch expiry-day data for the multi-year iron-fly test. Read-only market data.

  python3 -I fetch_expiry.py <token_file> <client_id> <out_dir> <NIFTY|SENSEX> <from> <to>

1. Index 1-min candles for the whole period.
2. ATM CE + PE (nearest weekly) for every trading day, to find expiry days from
   the data itself: an expiry day is one where the cheaper ATM leg closes at
   <= Rs 1 (the weekday changed several times, and holidays shift it).
3. ATM-10..ATM+10, CE and PE, for each expiry day only.

Writes index_1m.json, expiries.json and opt_1_<CALL|PUT>_<off>.json (same
format as fetch_data.py, expiry days only). Re-running reuses cached requests.
"""
import json, os, sys, time, datetime as dt
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

TOKEN = open(sys.argv[1]).read().strip()
CLIENT = sys.argv[2]
OUT = sys.argv[3]
INDEX = sys.argv[4]
FROM = dt.date.fromisoformat(sys.argv[5])
TO = dt.date.fromisoformat(sys.argv[6])
SEG, SID = {"NIFTY": ("NSE_FNO", 13), "SENSEX": ("BSE_FNO", 51)}[INDEX]
RAW = os.path.join(OUT, "raw")
os.makedirs(RAW, exist_ok=True)
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]


def post(path, body, tries=6):
    req = urllib.request.Request(
        "https://api.dhan.co/v2" + path, data=json.dumps(body).encode(),
        headers={"access-token": TOKEN, "client-id": CLIENT,
                 "Content-Type": "application/json", "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if (e.code in (429, 500, 502, 503) or "DH-904" in msg) and i < tries - 1:
                time.sleep(2 ** i); continue
            return {"error": f"{e.code}: {msg}"}
        except urllib.error.URLError:
            if i < tries - 1:
                time.sleep(2 ** i); continue
            return {"error": "network"}


# ---- 1. index -------------------------------------------------------------
p = os.path.join(OUT, "index_1m.json")
if not os.path.exists(p):
    cols = {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")}
    s = FROM
    while s <= TO:
        e = min(s + dt.timedelta(days=84), TO)
        r = post("/charts/intraday", {
            "securityId": str(SID), "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": "1", "oi": False,
            "fromDate": f"{s} 09:15:00", "toDate": f"{e} 15:30:00"})
        for k in cols:
            cols[k] += (r or {}).get(k, []) or []
        s = e + dt.timedelta(days=1)
        time.sleep(0.3)
    json.dump(cols, open(p, "w"))
idx = json.load(open(p))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
days = sorted({dt.datetime.fromtimestamp(t, IST).date() for t in idx["timestamp"]})
print(INDEX, "trading days", len(days), flush=True)


def fetch(job):
    typ, off, d = job
    f = os.path.join(RAW, f"{typ}_{off}_{d}.json")
    if os.path.exists(f):
        return json.load(open(f))
    strike = "ATM" if off == 0 else f"ATM{off:+d}"
    r = post("/charts/rollingoption", {
        "exchangeSegment": SEG, "interval": "1", "securityId": SID,
        "instrument": "OPTIDX", "expiryFlag": "WEEK", "expiryCode": 1,
        "strike": strike, "drvOptionType": typ, "requiredData": FIELDS,
        "fromDate": str(d), "toDate": str(d)})
    json.dump(r, open(f, "w"))
    return r


def series(r, typ):
    return ((r or {}).get("data") or {}).get("ce" if typ == "CALL" else "pe") or {}


# ---- 2. find expiry days ----------------------------------------------------
jobs = [(t, 0, d) for d in days for t in ("CALL", "PUT")]
with ThreadPoolExecutor(max_workers=4) as ex:
    res = list(ex.map(fetch, jobs))
by = {(j[0], j[2]): r for j, r in zip(jobs, res)}
expiries = []
for d in days:
    ce, pe = series(by[("CALL", d)], "CALL"), series(by[("PUT", d)], "PUT")
    if not ce.get("close") or not pe.get("close"):
        continue
    # Last bar at or after 15:25 where both legs exist, same strike.
    last_ce, last_pe = ce["close"][-1], pe["close"][-1]
    t_ce = dt.datetime.fromtimestamp(ce["timestamp"][-1], IST).strftime("%H:%M")
    if t_ce >= "15:25" and min(last_ce, last_pe) <= 1.0:
        expiries.append(str(d))
json.dump(expiries, open(os.path.join(OUT, "expiries.json"), "w"))
print(INDEX, "expiry days found", len(expiries), flush=True)

# ---- 3. full strike band on expiry days -------------------------------------
jobs = [(t, off, dt.date.fromisoformat(d)) for d in expiries
        for t in ("CALL", "PUT") for off in range(-10, 11)]
done = 0
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, _ in enumerate(ex.map(fetch, jobs)):
        if i % 1000 == 0:
            print("band progress", i, len(jobs), flush=True)

errors = 0
for typ in ("CALL", "PUT"):
    for off in range(-10, 11):
        cols = {k: [] for k in FIELDS + ["timestamp"]}
        for d in expiries:
            r = json.load(open(os.path.join(RAW, f"{typ}_{off}_{d}.json")))
            if "error" in r:
                errors += 1
                continue
            s = series(r, typ)
            for k in cols:
                cols[k] += s.get(k) or []
        json.dump(cols, open(os.path.join(OUT, f"opt_1_{typ}_{off}.json"), "w"))
print(INDEX, "done; errored requests", errors, flush=True)
