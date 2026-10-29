"""
Fetch SENSEX history from Dhan for the backtest. Read-only market data.

  python3 -I fetch_data.py <token_file> <client_id> <out_dir> <from YYYY-MM-DD> <to YYYY-MM-DD>

Writes:
  index_1m.json            SENSEX index 1-min candles (with volume)
  opt_<code>_<type>_<off>.json  expired weekly options, ATM-relative strike
                           offsets -5..+5, expiryCode 1 (current weekly) and
                           2 (next weekly, for expiry days), CALL and PUT
Re-running skips files already present.
"""
import json, os, sys, time, datetime as dt
import urllib.request, urllib.error

TOKEN = open(sys.argv[1]).read().strip()
CLIENT = sys.argv[2]
OUT = sys.argv[3]
FROM = dt.date.fromisoformat(sys.argv[4])
TO = dt.date.fromisoformat(sys.argv[5])
os.makedirs(OUT, exist_ok=True)

def post(path, body, tries=5):
    req = urllib.request.Request(
        "https://api.dhan.co/v2" + path,
        data=json.dumps(body).encode(),
        headers={"access-token": TOKEN, "client-id": CLIENT,
                 "Content-Type": "application/json", "Accept": "application/json"},
    )
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(2 ** i); continue
            raise RuntimeError(f"{path} {e.code}: {msg}")
        except urllib.error.URLError:
            if i < tries - 1:
                time.sleep(2 ** i); continue
            raise
    return None

def chunks(a, b, days):
    s = a
    while s <= b:
        e = min(s + dt.timedelta(days=days - 1), b)
        yield s, e
        s = e + dt.timedelta(days=1)

# --- index 1-min ---------------------------------------------------------
p = os.path.join(OUT, "index_1m.json")
if not os.path.exists(p):
    cols = {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")}
    for s, e in chunks(FROM, TO, 85):
        r = post("/charts/intraday", {
            "securityId": "51", "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": "1", "oi": False,
            "fromDate": f"{s} 09:15:00", "toDate": f"{e} 15:30:00"})
        for k in cols:
            cols[k] += r.get(k, [])
        time.sleep(0.3)
    json.dump(cols, open(p, "w"))
    print("index bars", len(cols["timestamp"]))

# --- expired options -----------------------------------------------------
# The rolling-option endpoint rejects windows longer than 2 days (DH-905), so
# fetch 2-day windows, cache each response, and merge per series at the end.
# expiryCode 2 (next weekly) is only needed on expiry days: fetch it for
# windows touching a Wednesday or Thursday.
from concurrent.futures import ThreadPoolExecutor

FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
RAW = os.path.join(OUT, "raw")
os.makedirs(RAW, exist_ok=True)

windows = []
d = FROM
while d <= TO:
    e = min(d + dt.timedelta(days=1), TO)
    if any((d + dt.timedelta(days=k)).weekday() < 5 for k in range((e - d).days + 1)):
        windows.append((d, e))
    d = e + dt.timedelta(days=1)

jobs = []
for (s, e) in windows:
    span = {s + dt.timedelta(days=k) for k in range((e - s).days + 1)}
    for code in (1, 2):
        if code == 2 and not any(x.weekday() in (2, 3) for x in span):
            continue
        for typ in ("CALL", "PUT"):
            for off in range(-5, 6):
                jobs.append((code, typ, off, s, e))

def run(job):
    code, typ, off, s, e = job
    f = os.path.join(RAW, f"{code}_{typ}_{off}_{s}.json")
    if os.path.exists(f):
        return 0
    strike = "ATM" if off == 0 else f"ATM{off:+d}"
    try:
        r = post("/charts/rollingoption", {
            "exchangeSegment": "BSE_FNO", "interval": "1", "securityId": 51,
            "instrument": "OPTIDX", "expiryFlag": "WEEK", "expiryCode": code,
            "strike": strike, "drvOptionType": typ, "requiredData": FIELDS,
            "fromDate": str(s), "toDate": str(e)})
    except RuntimeError as err:
        r = {"error": str(err)}
    json.dump(r, open(f, "w"))
    return 1

print("jobs", len(jobs), flush=True)
done = 0
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, n in enumerate(ex.map(run, jobs)):
        done += n
        if i % 500 == 0:
            print("progress", i, len(jobs), flush=True)

errors = 0
for code in (1, 2):
    for typ in ("CALL", "PUT"):
        key = "ce" if typ == "CALL" else "pe"
        for off in range(-5, 6):
            cols = {k: [] for k in FIELDS + ["timestamp"]}
            for (c, t, o, s, e) in jobs:
                if (c, t, o) != (code, typ, off):
                    continue
                r = json.load(open(os.path.join(RAW, f"{c}_{t}_{o}_{s}.json")))
                if "error" in r:
                    errors += 1
                    continue
                dd = ((r or {}).get("data") or {}).get(key) or {}
                for k in cols:
                    cols[k] += dd.get(k) or []
            json.dump(cols, open(os.path.join(OUT, f"opt_{code}_{typ}_{off}.json"), "w"))
print("fetched", done, "errors", errors)
