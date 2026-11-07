"""Read-only Dhan v2 market-data access + Telegram. Memory only: nothing is written to disk."""
import csv
import datetime as dt
import time

import requests

import config

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_last = {}
ALLOWED = ("/charts/intraday", "/charts/historical", "/optionchain", "/optionchain/expirylist", "/marketfeed/ltp")
SPACING = {"/optionchain": 3.2, "/optionchain/expirylist": 3.2, "/marketfeed/ltp": 1.05}


def now_ist():
    return dt.datetime.now(IST)


def post(path, payload, retries=3):
    if path not in ALLOWED:
        raise ValueError("blocked non-market-data endpoint " + path)
    wait = _last.get(path, 0) + SPACING.get(path, 0.25) - time.time()
    if wait > 0:
        time.sleep(wait)
    hdr = {"access-token": config.ACCESS_TOKEN, "client-id": config.CLIENT_ID,
           "Content-Type": "application/json", "Accept": "application/json"}
    err = None
    for k in range(retries):
        _last[path] = time.time()
        try:
            r = requests.post(config.BASE + path, json=payload, headers=hdr, timeout=20)
            if r.status_code == 200:
                return r.json()
            err = "HTTP %s %s" % (r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403):
                break
        except requests.RequestException as e:
            err = str(e)[:200]
        time.sleep(2 ** k)
    raise RuntimeError(path + ": " + str(err))


def candles(payload):
    d = payload or {}
    ts, o, h, l, c, v = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close", "volume"))
    out = []
    for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
        out.append({"ts": float(ts[i]), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]),
                    "close": float(c[i]), "volume": float(v[i]) if i < len(v) else 0.0})
    return out


def front_futures():
    """Stream the instrument master; keep the nearest-expiry FUTIDX per index (sid, segment, lot, expiry)."""
    want = set(config.INDICES)
    today = now_ist().date().isoformat()
    best = {}
    with requests.get(config.SCRIP_MASTER, stream=True, timeout=120) as r:
        r.raise_for_status()
        lines = (ln.decode("utf-8", "replace") for ln in r.iter_lines() if ln)
        rd = csv.reader(lines)
        hdr = [x.strip().upper() for x in next(rd)]
        ix = {k: hdr.index(k) for k in ("EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL",
                                        "DISPLAY_NAME", "LOT_SIZE", "SM_EXPIRY_DATE")}
        for row in rd:
            if len(row) <= max(ix.values()) or row[ix["INSTRUMENT"]] != "FUTIDX":
                continue
            u = row[ix["UNDERLYING_SYMBOL"]].strip().upper()
            e = row[ix["SM_EXPIRY_DATE"]][:10]
            if u not in want or e < today:
                continue
            if u not in best or e < best[u]["expiry"]:
                seg = "NSE_FNO" if row[ix["EXCH_ID"]] == "NSE" else "BSE_FNO"
                best[u] = {"sid": int(row[ix["SECURITY_ID"]]), "segment": seg, "expiry": e,
                           "lot": float(row[ix["LOT_SIZE"]] or 1), "display": row[ix["DISPLAY_NAME"]]}
    return best


def fut_1m(fut, days=6):
    d0 = (now_ist().date() - dt.timedelta(days=days)).isoformat()
    d1 = now_ist().date().isoformat()
    return candles(post("/charts/intraday", {"securityId": str(fut["sid"]), "exchangeSegment": fut["segment"],
                                             "instrument": "FUTIDX", "interval": 1, "oi": False,
                                             "fromDate": d0 + " 09:15:00", "toDate": d1 + " 15:30:00"}))


def prev_day(index_sid):
    d0 = (now_ist().date() - dt.timedelta(days=15)).isoformat()
    rows = candles(post("/charts/historical", {"securityId": str(index_sid), "exchangeSegment": "IDX_I",
                                               "instrument": "INDEX", "expiryCode": 0, "oi": False,
                                               "fromDate": d0, "toDate": now_ist().date().isoformat()}))
    today = now_ist().date()
    rows = [x for x in rows if dt.datetime.fromtimestamp(x["ts"], IST).date() < today]
    return rows[-1] if rows else None


def option_chain(index_sid):
    exps = post("/optionchain/expirylist", {"UnderlyingScrip": index_sid, "UnderlyingSeg": "IDX_I"}).get("data") or []
    now = now_ist()
    exps = sorted(e for e in exps if dt.datetime.fromisoformat(e[:10] + "T15:30:00+05:30") > now)
    if not exps:
        return None, {}
    d = post("/optionchain", {"UnderlyingScrip": index_sid, "UnderlyingSeg": "IDX_I", "Expiry": exps[0]})
    d = d.get("data", d)
    legs = {}
    for ks, v in (d.get("oc") or {}).items():
        for side in ("ce", "pe"):
            o = v.get(side)
            if isinstance(o, dict):
                legs[(float(ks), side.upper())] = o
    return exps[0], legs


def vix():
    d = post("/marketfeed/ltp", {"IDX_I": [config.VIX_SID]})
    try:
        return float(d["data"]["IDX_I"][str(config.VIX_SID)]["last_price"])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def telegram(text):
    if config.TELEGRAM_TOKEN.startswith("{{") or config.TELEGRAM_CHAT.startswith("{{"):
        return False
    try:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % config.TELEGRAM_TOKEN,
                          json={"chat_id": config.TELEGRAM_CHAT, "text": text[:4000]}, timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False
