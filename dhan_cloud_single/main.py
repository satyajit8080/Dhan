"""
DHAN QUANT - single-file Dhan Cloud strategy (read-only, never places orders)

On start:  1) offline self-test of every formula
           2) pulls BACKTEST_MONTHS of 5m data from Dhan (in memory) and backtests the rules
           3) live loop: on every closed 5m bar computes indicators + rules for each index,
              prints the signal card, and sends Telegram on a new LONG/SHORT
Nothing is written to disk; no ports. Secrets come from the Env Variables tab ({{NAME}}).

Indicators: VWAP (typical price, session reset), ATR(14) Wilder, Supertrend(10,3), ADX/DI(14),
RSI(14) Wilder, Bollinger(20,2) bandwidth, Efficiency Ratio(10), Z-score(20), floor pivots +
previous-day H/L/C, PCR (volume and OI).
Rules: trend filter ADX>25 and ER>0.3; LONG = close>VWAP, Supertrend long, DI+>DI-, RSI pullback
40-55 turning up (SHORT mirror); mean reversion only if ADX<20 (|z|>2 fade to VWAP); squeeze
(bandwidth at 20-bar low) -> breakout in Supertrend direction; skip entries within 0.2*ATR of
R1/R2/S1/S2/PDH/PDL unless closing through; stop 2*ATR; lots = risk / (stop * lot size);
P&L includes brokerage, STT, exchange txn, SEBI, stamp, GST. Entries 09:30-15:10 IST only.
"""
import bisect
import csv
import datetime as dt
import math
import time
import traceback

import requests

# =============================================================== CONFIG ==
# Dhan Cloud replaces {{NAME}} with the Variable's value. Single quotes here mean the value
# works whether it is entered in the Variables page with or without double quotes.


def _var(v):
    return str(v).strip().strip('"').strip("'").strip()


def _first(*vals):
    """First placeholder Dhan Cloud actually substituted (the automatic Global Variable name is not
    published; common names are tried in order)."""
    for v in vals:
        v = _var(v)
        if v and not v.startswith("{{"):
            return v
    return _var(vals[0])


CLIENT_ID = _first('{{DHAN_CLIENT_ID}}', '{{CLIENT_ID}}', '{{CLIENT_CODE}}', '{{DHAN_CLIENT_CODE}}', '{{client_id}}')
ACCESS_TOKEN = _first('{{DHAN_ACCESS_TOKEN}}', '{{ACCESS_TOKEN}}', '{{TOKEN}}', '{{DHAN_TOKEN}}', '{{access_token}}')
TELEGRAM_TOKEN = _var('{{TELEGRAM_BOT_TOKEN}}')
TELEGRAM_CHAT = _var('{{TELEGRAM_CHAT_ID}}')

RUN_SELFTEST_ON_START = True
RUN_BACKTEST_ON_START = True
BACKTEST_MONTHS = 6
FIXED_POINT_TESTS = [(10, 10), (15, 15), (10, 20), (15, 30)]   # (stop pts, target pts) compared vs 2xATR
PRINT_ALL_TRADES = True         # print every backtest trade as WIN / LOSS with entry + exit reason

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
INDICES = {   # index security ids verified against the Dhan instrument master (IDX_I)
    "NIFTY": {"sid": 13, "exch": "NSE", "step": 50},
    "BANKNIFTY": {"sid": 25, "exch": "NSE", "step": 100},
    "FINNIFTY": {"sid": 27, "exch": "NSE", "step": 50},
    "SENSEX": {"sid": 51, "exch": "BSE", "step": 100},
    "BANKEX": {"sid": 69, "exch": "BSE", "step": 100},
}
VIX_SID = 21
PARAMS = {"adx_trend": 25.0, "er_trend": 0.3, "adx_mr": 20.0, "z_mr": 2.0, "rsi_lo": 40.0, "rsi_hi": 55.0,
          "squeeze_lookback": 20, "squeeze_memory": 5, "level_atr": 0.2, "stop_atr": 2.0,
          "risk_rupees": 5000.0, "max_spread_pct": 1.0, "pcr_low": 0.7, "pcr_high": 1.3}
RISK_FREE = 0.065
LOOP_SLEEP_S = 20
ENTRY_START, ENTRY_END, EOD_EXIT = dt.time(9, 30), dt.time(15, 10), dt.time(15, 20)

NAN = float("nan")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def configured():
    return not CLIENT_ID.startswith("{{") and not ACCESS_TOKEN.startswith("{{")


def ok(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


# =========================================================== INDICATORS ==
def wilder(x, n):
    """Seed = SMA of first n valid values, then A_t = (A_{t-1}(n-1) + x_t)/n."""
    out = [NAN] * len(x)
    s = next((i for i, v in enumerate(x) if ok(v)), None)
    if s is None or s + n > len(x) or not all(ok(v) for v in x[s:s + n]):
        return out
    out[s + n - 1] = sum(x[s:s + n]) / n
    for i in range(s + n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n if ok(x[i]) else out[i - 1]
    return out


def true_range(b):
    tr = []
    for i, x in enumerate(b):
        if i == 0:
            tr.append(x["high"] - x["low"])
        else:
            pc = b[i - 1]["close"]
            tr.append(max(x["high"] - x["low"], abs(x["high"] - pc), abs(x["low"] - pc)))
    return tr


def vwap(b):
    out, pv, vv, day = [], 0.0, 0.0, None
    for x in b:
        d = dt.datetime.fromtimestamp(x["ts"], IST).date()
        if d != day:
            pv, vv, day = 0.0, 0.0, d
        p = (x["high"] + x["low"] + x["close"]) / 3.0
        v = x.get("volume") or 0.0
        pv += p * v
        vv += v
        out.append(pv / vv if vv > 0 else NAN)
    return out


def atr(b, n=14):
    return wilder(true_range(b), n)


def supertrend(b, n=10, m=3.0):
    a = wilder(true_range(b), n)
    line, d = [NAN] * len(b), [NAN] * len(b)
    fu = fl = None
    for i, x in enumerate(b):
        if not ok(a[i]):
            continue
        hl2 = (x["high"] + x["low"]) / 2
        bu, bl = hl2 + m * a[i], hl2 - m * a[i]
        if fu is None:
            fu, fl = bu, bl
            d[i] = 1.0 if x["close"] >= hl2 else -1.0
        else:
            pc = b[i - 1]["close"]
            fu = bu if (bu < fu or pc > fu) else fu
            fl = bl if (bl > fl or pc < fl) else fl
            if d[i - 1] < 0:
                d[i] = 1.0 if x["close"] > fu else -1.0
            else:
                d[i] = -1.0 if x["close"] < fl else 1.0
        line[i] = fl if d[i] > 0 else fu
    return line, d


def adx(b, n=14):
    tr = true_range(b)
    tr[0] = NAN
    pdm, ndm = [NAN], [NAN]
    for i in range(1, len(b)):
        up = b[i]["high"] - b[i - 1]["high"]
        dn = b[i - 1]["low"] - b[i]["low"]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    a, sp, sn = wilder(tr, n), wilder(pdm, n), wilder(ndm, n)
    dip, dim, dx = [], [], []
    for i in range(len(b)):
        if ok(a[i]) and ok(sp[i]) and a[i] > 0:
            p_, m_ = 100 * sp[i] / a[i], 100 * sn[i] / a[i]
            dip.append(p_)
            dim.append(m_)
            dx.append(100 * abs(p_ - m_) / (p_ + m_) if p_ + m_ > 0 else NAN)
        else:
            dip.append(NAN)
            dim.append(NAN)
            dx.append(NAN)
    return wilder(dx, n), dip, dim


def rsi(b, n=14):
    g, l_ = [NAN], [NAN]
    for i in range(1, len(b)):
        ch = b[i]["close"] - b[i - 1]["close"]
        g.append(max(ch, 0.0))
        l_.append(max(-ch, 0.0))
    ag, al = wilder(g, n), wilder(l_, n)
    out = []
    for x, y in zip(ag, al):
        if not ok(x):
            out.append(NAN)
        elif y == 0:
            out.append(100.0 if x > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + x / y))
    return out


def bollinger(b, n=20, k=2.0):
    c = [x["close"] for x in b]
    mid, up, lo, sd, bw = ([NAN] * len(c) for _ in range(5))
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        s = math.sqrt(sum((v - m) ** 2 for v in w) / n)
        mid[i], sd[i], up[i], lo[i] = m, s, m + k * s, m - k * s
        bw[i] = (up[i] - lo[i]) / m
    return {"mid": mid, "upper": up, "lower": lo, "sd": sd, "bandwidth": bw}


def efficiency_ratio(b, n=10):
    c = [x["close"] for x in b]
    out = [NAN] * len(c)
    for i in range(n, len(c)):
        den = sum(abs(c[j] - c[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(c[i] - c[i - n]) / den if den > 0 else 0.0
    return out


def zscore(b, n=20):
    bb = bollinger(b, n)
    return [((x["close"] - m) / s if s > 0 else 0.0) if ok(s) else NAN for x, m, s in zip(b, bb["mid"], bb["sd"])]


def pivots(h, l, c):
    p = (h + l + c) / 3
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l), "PDH": h, "PDL": l, "PDC": c}


def compute(b):
    st, sd = supertrend(b, 10, 3.0)
    a, dp, dm = adx(b, 14)
    bb = bollinger(b, 20, 2.0)
    return {"close": [x["close"] for x in b], "vwap": vwap(b), "atr": atr(b, 14), "st": st, "st_dir": sd,
            "adx": a, "di_plus": dp, "di_minus": dm, "rsi": rsi(b, 14), "bb_mid": bb["mid"], "bb_up": bb["upper"],
            "bb_lo": bb["lower"], "bw": bb["bandwidth"], "er": efficiency_ratio(b, 10), "z": zscore(b, 20)}


# ================================================================ RULES ==
CHARGES = {"brokerage_per_order": 20.0,
           "fut": {"stt_sell": 0.0002, "txn_NSE": 0.0000173, "txn_BSE": 0.0, "stamp_buy": 0.00002},
           "opt": {"stt_sell": 0.001, "txn_NSE": 0.0003503, "txn_BSE": 0.000325, "stamp_buy": 0.00003},
           "sebi": 0.000001, "gst": 0.18}


def charges(kind, exch, buy_value, sell_value):
    """Round trip in Rupees: brokerage x2, STT on sell, exchange txn, SEBI, stamp on buy, GST 18%."""
    r = CHARGES[kind]
    brk = 2 * CHARGES["brokerage_per_order"]
    txn = r.get("txn_" + exch, r["txn_NSE"]) * (buy_value + sell_value)
    sebi = CHARGES["sebi"] * (buy_value + sell_value)
    return brk + r["stt_sell"] * sell_value + txn + sebi + r["stamp_buy"] * buy_value + CHARGES["gst"] * (brk + txn + sebi)


def _is_squeeze(bw, j, lb):
    if j < lb:
        return False
    w = bw[j - lb + 1:j + 1]
    return all(ok(v) for v in w) and bw[j] <= min(w) + 1e-12


def signal_at(x, i, levels, p=PARAMS):
    """Rules on CLOSED bar i (uses bars <= i only)."""
    g = {k: v[i] for k, v in x.items()}
    need = ("atr", "adx", "di_plus", "di_minus", "rsi", "er", "st_dir", "bw")
    if i < 1 or any(not ok(g[k]) for k in need):
        return {"signal": "NO_TRADE", "setup": "WARMUP", "reason": "indicators warming up"}
    c, vw, a = g["close"], g["vwap"], g["atr"]
    rp, cp = x["rsi"][i - 1], x["close"][i - 1]
    trend_ok = g["adx"] > p["adx_trend"] and g["er"] > p["er_trend"]
    out = {"signal": "NO_TRADE", "setup": "CHOP",
           "reason": "chop: ADX %.1f / ER %.2f below %.0f / %s" % (g["adx"], g["er"], p["adx_trend"], p["er_trend"])}
    lb = p["squeeze_lookback"]
    sq_now = _is_squeeze(x["bw"], i, lb)
    sq_recent = any(_is_squeeze(x["bw"], j, lb) for j in range(max(lb, i - p["squeeze_memory"]), i))
    cand = None
    if sq_recent and ok(g["bb_up"]) and c > g["bb_up"] and g["st_dir"] > 0:
        cand = {"signal": "LONG", "setup": "SQUEEZE_BREAKOUT", "reason": "squeeze breakout above BB upper %.1f, Supertrend long" % g["bb_up"]}
    elif sq_recent and ok(g["bb_lo"]) and c < g["bb_lo"] and g["st_dir"] < 0:
        cand = {"signal": "SHORT", "setup": "SQUEEZE_BREAKOUT", "reason": "squeeze breakout below BB lower %.1f, Supertrend short" % g["bb_lo"]}
    elif sq_now:
        out = {"signal": "WAIT", "setup": "SQUEEZE", "reason": "Bollinger bandwidth %.4f at %d-bar low: wait for breakout" % (g["bw"], lb)}
    if cand is None and trend_ok and ok(vw):
        if c > vw and g["st_dir"] > 0 and g["di_plus"] > g["di_minus"] and p["rsi_lo"] <= rp <= p["rsi_hi"] and g["rsi"] > rp:
            cand = {"signal": "LONG", "setup": "TREND_PULLBACK",
                    "reason": "ADX %.0f ER %.2f; above VWAP, ST long, DI+>DI-, RSI %.0f->%.0f turning up" % (g["adx"], g["er"], rp, g["rsi"])}
        elif c < vw and g["st_dir"] < 0 and g["di_minus"] > g["di_plus"] and (100 - p["rsi_hi"]) <= rp <= (100 - p["rsi_lo"]) and g["rsi"] < rp:
            cand = {"signal": "SHORT", "setup": "TREND_PULLBACK",
                    "reason": "ADX %.0f ER %.2f; below VWAP, ST short, DI->DI+, RSI %.0f->%.0f turning down" % (g["adx"], g["er"], rp, g["rsi"])}
        elif out["setup"] == "CHOP":
            out = {"signal": "NO_TRADE", "setup": "TREND_NO_TRIGGER", "reason": "trend regime (ADX %.0f) but no pullback trigger" % g["adx"]}
    elif cand is None and trend_ok:
        out = {"signal": "NO_TRADE", "setup": "NO_VWAP", "reason": "VWAP unavailable (series carries no volume)"}
    if cand is None and g["adx"] < p["adx_mr"] and ok(g["z"]) and ok(vw):
        if g["z"] > p["z_mr"] and c > vw:
            cand = {"signal": "SHORT", "setup": "MEAN_REVERSION", "reason": "ADX %.0f<%.0f, z %+.2f: fade toward VWAP %.1f" % (g["adx"], p["adx_mr"], g["z"], vw)}
        elif g["z"] < -p["z_mr"] and c < vw:
            cand = {"signal": "LONG", "setup": "MEAN_REVERSION", "reason": "ADX %.0f<%.0f, z %+.2f: fade toward VWAP %.1f" % (g["adx"], p["adx_mr"], g["z"], vw)}
    if cand is None:
        return out
    d = 1 if cand["signal"] == "LONG" else -1
    for name in ("R1", "R2", "S1", "S2", "PDH", "PDL"):
        L = levels.get(name)
        if not ok(L) or abs(c - L) >= p["level_atr"] * a:
            continue
        broke = (cp <= L < c) if d > 0 else (cp >= L > c)
        if not broke:
            return {"signal": "NO_TRADE", "setup": "NEAR_LEVEL",
                    "reason": "%s %s skipped: close %.1f within %sxATR of %s %.1f" % (cand["setup"], cand["signal"], c, p["level_atr"], name, L)}
        cand["reason"] += "; closing break of %s %.1f" % (name, L)
    sd = p["stop_atr"] * a
    cand.update({"stop_dist": sd, "entry_ref": c, "stop": c - d * sd})
    cand["targets"] = [vw] if cand["setup"] == "MEAN_REVERSION" else [c + d * sd, c + d * 2 * sd]
    return cand


def lots_for(risk_rupees, stop_pts, lot):
    return int(risk_rupees // (stop_pts * lot)) if stop_pts > 0 and lot > 0 else 0


def norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black76(F, K, T, r, s, call):
    df = math.exp(-r * T)
    if T <= 0 or s <= 0:
        return df * max((F - K) if call else (K - F), 0.0)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return df * (F * norm_cdf(d1) - K * norm_cdf(d2)) if call else df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def implied_vol(px, F, K, T, r, call):
    lo, hi = 1e-4, 5.0
    if px <= 0 or black76(F, K, T, r, hi, call) < px or black76(F, K, T, r, lo, call) > px:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, r, mid, call) > px:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# ============================================================ DHAN DATA ==
_last = {}
ALLOWED = ("/charts/intraday", "/charts/historical", "/optionchain", "/optionchain/expirylist", "/marketfeed/ltp")
SPACING = {"/optionchain": 3.2, "/optionchain/expirylist": 3.2, "/marketfeed/ltp": 1.05}


def now_ist():
    return dt.datetime.now(IST)


def post(path, payload, retries=3):
    """Read-only market data only; any other endpoint is refused."""
    if path not in ALLOWED:
        raise ValueError("blocked non-market-data endpoint " + path)
    err = None
    for k in range(retries):
        wait = _last.get(path, 0) + SPACING.get(path, 0.25) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[path] = time.time()
        try:
            r = requests.post(BASE + path, json=payload, timeout=30,
                              headers={"access-token": ACCESS_TOKEN, "client-id": CLIENT_ID,
                                       "Content-Type": "application/json", "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            err = "HTTP %s %s" % (r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403):
                break
        except requests.RequestException as e:
            err = str(e)[:200]
        time.sleep(2 ** k)
    raise RuntimeError(path + ": " + str(err))


def candles(d):
    d = d or {}
    ts, o, h, l, c, v = (d.get(k) or [] for k in ("timestamp", "open", "high", "low", "close", "volume"))
    oi = d.get("open_interest") or []
    out = []
    for i in range(min(len(ts), len(o), len(h), len(l), len(c))):
        out.append({"ts": float(ts[i]), "open": float(o[i]), "high": float(h[i]), "low": float(l[i]),
                    "close": float(c[i]), "volume": float(v[i]) if i < len(v) else 0.0,
                    "oi": float(oi[i]) if i < len(oi) else None})
    return out


def all_futures():
    """Stream the instrument master; every live FUTIDX contract per index (sid, segment, lot, expiry)."""
    today = now_ist().date().isoformat()
    res = {n: [] for n in INDICES}
    with requests.get(SCRIP_MASTER, stream=True, timeout=180) as r:
        r.raise_for_status()
        rd = csv.reader(ln.decode("utf-8", "replace") for ln in r.iter_lines() if ln)
        hdr = [x.strip().upper() for x in next(rd)]
        ix = {k: hdr.index(k) for k in ("EXCH_ID", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL",
                                        "DISPLAY_NAME", "LOT_SIZE", "SM_EXPIRY_DATE")}
        for row in rd:
            if len(row) <= max(ix.values()) or row[ix["INSTRUMENT"]] != "FUTIDX":
                continue
            u = row[ix["UNDERLYING_SYMBOL"]].strip().upper()
            e = row[ix["SM_EXPIRY_DATE"]][:10]
            if u in res and e >= today:
                res[u].append({"sid": int(row[ix["SECURITY_ID"]]), "expiry": e, "lot": float(row[ix["LOT_SIZE"]] or 1),
                               "segment": "NSE_FNO" if row[ix["EXCH_ID"]] == "NSE" else "BSE_FNO",
                               "display": row[ix["DISPLAY_NAME"]]})
    for u in res:
        res[u].sort(key=lambda f: f["expiry"])
    return res


def intraday(sid, seg, instrument, frm, to, interval=5, oi=False):
    """Any length: split into 89-day chunks (Dhan limit 90 days per request)."""
    out, a = [], frm
    while a <= to:
        b = min(a + dt.timedelta(days=89), to)
        try:
            out += candles(post("/charts/intraday", {"securityId": str(sid), "exchangeSegment": seg, "instrument": instrument,
                                                     "interval": interval, "oi": oi, "fromDate": a.isoformat() + " 09:15:00",
                                                     "toDate": b.isoformat() + " 15:30:00"}))
        except RuntimeError as e:
            log("  chunk", a, "->", b, "failed:", str(e)[:120])
        a = b + dt.timedelta(days=1)
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["ts"]):
        if x["ts"] not in seen:
            seen.add(x["ts"])
            uniq.append(x)
    return uniq


def daily(index_sid, days):
    d0 = (now_ist().date() - dt.timedelta(days=days)).isoformat()
    return candles(post("/charts/historical", {"securityId": str(index_sid), "exchangeSegment": "IDX_I", "instrument": "INDEX",
                                               "expiryCode": 0, "oi": False, "fromDate": d0, "toDate": now_ist().date().isoformat()}))


def prev_day(rows, day):
    rows = [x for x in rows if dt.datetime.fromtimestamp(x["ts"], IST).date() < day]
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
    try:
        return float(post("/marketfeed/ltp", {"IDX_I": [VIX_SID]})["data"]["IDX_I"][str(VIX_SID)]["last_price"])
    except (KeyError, TypeError, ValueError, RuntimeError):
        return NAN


def telegram(text):
    if TELEGRAM_TOKEN.startswith("{{") or TELEGRAM_CHAT.startswith("{{"):
        return False
    try:
        r = requests.post("https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN,
                          json={"chat_id": TELEGRAM_CHAT, "text": text[:4000]}, timeout=15)
        return r.status_code == 200
    except requests.RequestException:
        return False


def stitch_front_month(per, expiries):
    """Continuous front month: each date uses the contract with the nearest expiry >= that date."""
    by_date = {}
    for sid, bars in per.items():
        for b in bars:
            by_date.setdefault(dt.datetime.fromtimestamp(b["ts"], IST).date(), set()).add((expiries[sid], sid))
    out = []
    for d in sorted(by_date):
        c = sorted(x for x in by_date[d] if x[0] >= d.isoformat())
        if c:
            out += [dict(b, contract=c[0][1]) for b in per[c[0][1]] if dt.datetime.fromtimestamp(b["ts"], IST).date() == d]
    return out


# =========================================================== SIGNAL CARD ==
def resample(b1, minutes=5):
    out, cur, key = [], None, None
    for b in b1:
        t = dt.datetime.fromtimestamp(b["ts"], IST)
        m = (t.hour * 60 + t.minute) - 555
        k = (t.date(), m // minutes)
        if k != key:
            if cur:
                out.append(cur)
            start = dt.datetime.combine(t.date(), dt.time(9, 15), IST) + dt.timedelta(minutes=(m // minutes) * minutes)
            cur = dict(b, ts=start.timestamp())
            key = k
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] = cur.get("volume", 0) + b.get("volume", 0)
    if cur:
        out.append(cur)
    return out


def closed(bars, minutes, now):
    return [b for b in bars if b["ts"] + minutes * 60 <= now + 1]


def pcr(legs):
    s = {"CE": [0.0, 0.0], "PE": [0.0, 0.0]}
    for (_k, side), o in legs.items():
        s[side][0] += float(o.get("volume") or 0)
        s[side][1] += float(o.get("oi") or 0)
    return (s["PE"][0] / s["CE"][0] if s["CE"][0] else NAN), (s["PE"][1] / s["CE"][1] if s["CE"][1] else NAN)


def oi_change_top(legs, n=3):
    rows = [(k, s, float(o.get("oi") or 0) - float(o.get("previous_oi") or 0)) for (k, s), o in legs.items()]
    return sorted(rows, key=lambda r: -abs(r[2]))[:n]


def pick_strike(legs, fwd, step, d, max_spread):
    """Liquid ATM or 1-strike ITM (CE for LONG, PE for SHORT); reject spread > max_spread %."""
    side = "CE" if d > 0 else "PE"
    ks = sorted({k for (k, s) in legs if s == side})
    if not ks:
        return None, ["no chain"]
    atm = min(ks, key=lambda k: abs(k - fwd))
    notes, best = [], None
    for K in (atm, atm - step if d > 0 else atm + step):
        o = legs.get((K, side))
        bid, ask = float((o or {}).get("top_bid_price") or 0), float((o or {}).get("top_ask_price") or 0)
        if not o or bid <= 0 or ask <= 0:
            notes.append("%.0f%s: no two-sided quote" % (K, side))
            continue
        sp = (ask - bid) / ((ask + bid) / 2) * 100
        if sp > max_spread:
            notes.append("%.0f%s: spread %.2f%% > %s%%" % (K, side, sp, max_spread))
            continue
        score = float(o.get("volume") or 0) + 0.1 * float(o.get("oi") or 0)
        if best is None or score > best[0]:
            best = (score, K, side, bid, ask, sp)
    return best, notes


def build_card(name, b1, prev, legs, expiry, vix_v, lot, now, p=PARAMS):
    bars5 = closed(resample(closed(b1, 1, now)), 5, now)
    card = {"index": name, "signal": "NO_TRADE", "setup": "NO_DATA", "reason": "only %d closed 5m bars" % len(bars5)}
    if len(bars5) < 40:
        return card, {}
    x = compute(bars5)
    i = len(bars5) - 1
    lv = pivots(prev["high"], prev["low"], prev["close"]) if prev else {}
    sig = signal_at(x, i, lv, p)
    bt = dt.datetime.fromtimestamp(bars5[i]["ts"], IST).time()
    if sig["signal"] in ("LONG", "SHORT") and not (ENTRY_START <= bt < ENTRY_END):
        sig = {"signal": "NO_TRADE", "setup": "OUTSIDE_WINDOW",
               "reason": "%s %s ignored: bar %s outside entry window 09:30-15:10" % (sig["setup"], sig["signal"], bt.strftime("%H:%M"))}
    card = {"index": name, "signal": sig["signal"], "setup": sig["setup"], "reason": sig["reason"], "bar": bt.strftime("%H:%M")}
    table = {k: v[i] for k, v in x.items()}
    pv, po = pcr(legs)
    flags = [lab for lab, v in (("PCR vol", pv), ("PCR oi", po)) if ok(v) and (v < p["pcr_low"] or v > p["pcr_high"])]
    card["context"] = "PCR vol %.2f oi %.2f%s | VIX %.2f | OI chg %s" % (
        pv, po, (" EXTREME:" + ",".join(flags)) if flags else "", vix_v,
        " ".join("%.0f%s %+.0f" % r for r in oi_change_top(legs)))
    card["levels"] = lv
    if sig["signal"] not in ("LONG", "SHORT"):
        return card, table
    d = 1 if sig["signal"] == "LONG" else -1
    card.update({"entry": sig["entry_ref"], "sl": sig["stop"], "targets": sig["targets"],
                 "fut_lots": lots_for(p["risk_rupees"], sig["stop_dist"], lot)})
    best, notes = pick_strike(legs, sig["entry_ref"], INDICES[name]["step"], d, p["max_spread_pct"])
    card["strike_notes"] = notes
    if not best:
        card["reason"] += " | no liquid ATM/ITM strike passed the spread check"
        return card, table
    _, K, side, bid, ask, sp = best
    T = max((dt.datetime.fromisoformat(expiry + "T15:30:00+05:30") - dt.datetime.now(IST)).total_seconds() / 31536000, 1e-5)
    F = sig["entry_ref"]
    iv = implied_vol((bid + ask) / 2, F, K, T, RISK_FREE, side == "CE")
    if iv:
        adj = ask - black76(F, K, T, RISK_FREE, iv, side == "CE")
        sl = max(black76(F - d * sig["stop_dist"], K, T, RISK_FREE, iv, side == "CE") + adj, 0.05)
        tg = [black76(t, K, T, RISK_FREE, iv, side == "CE") + adj for t in sig["targets"]]
    else:
        sl, tg = max(ask - 0.5 * sig["stop_dist"], 0.05), [ask + 0.5 * abs(t - F) for t in sig["targets"]]
    lots = lots_for(p["risk_rupees"], ask - sl, lot)
    q = max(lots, 1) * lot
    card["option"] = {"strike": K, "side": side, "entry": round(ask, 2), "sl": round(sl, 2), "targets": [round(t, 2) for t in tg],
                      "spread_pct": round(sp, 2), "iv": round(iv * 100, 2) if iv else None, "lots": lots,
                      "charges_at_sl": round(charges("opt", INDICES[name]["exch"], ask * q, sl * q), 2)}
    return card, table


def card_text(card, table=None):
    s = "[%s] %s %s @%s\n%s" % (card["index"], card["signal"], card.get("setup"), card.get("bar", ""), card.get("reason"))
    if table:
        s += "\nVWAP %.1f ATR %.1f ST %s ADX %.1f DI+ %.1f DI- %.1f RSI %.1f BW %.4f ER %.2f Z %.2f" % (
            table["vwap"], table["atr"], "L" if table["st_dir"] > 0 else "S", table["adx"], table["di_plus"],
            table["di_minus"], table["rsi"], table["bw"], table["er"], table["z"])
    if "entry" in card:
        s += "\nFut entry %.1f SL %.1f T %s lots %d" % (card["entry"], card["sl"], ", ".join("%.1f" % t for t in card["targets"]), card["fut_lots"])
    o = card.get("option")
    if o:
        s += "\nOption %.0f%s entry %.2f SL %.2f T %s lots %d charges@SL Rs%.0f" % (
            o["strike"], o["side"], o["entry"], o["sl"], o["targets"], o["lots"], o["charges_at_sl"])
    if card.get("context"):
        s += "\n" + card["context"]
    return s


# ============================================================== BACKTEST ==
def wilson(k, n, z=1.959964):
    if n == 0:
        return NAN, NAN, NAN
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, c - h), min(1.0, c + h)


def backtest(bars5, dly, lot, exch, p=PARAMS):
    """Signal on CLOSED bar i -> fill at OPEN of bar i+1. Exit at 2xATR stop / target / 15:20.
    Stop assumed first if both are hit inside one bar. Net = gross - full charges."""
    if len(bars5) < 60:
        return {"status": "INSUFFICIENT DATA (%d bars)" % len(bars5)}
    x = compute(bars5)
    dmap = {dt.datetime.fromtimestamp(d["ts"], IST).date(): d for d in dly}
    ddays = sorted(dmap)
    trades, pos = [], None
    for i in range(1, len(bars5) - 1):
        b, nb = bars5[i], bars5[i + 1]
        t = dt.datetime.fromtimestamp(b["ts"], IST)
        tn = dt.datetime.fromtimestamp(nb["ts"], IST)
        if pos is not None:
            fav = (b["high"] - pos["entry"]) if pos["d"] > 0 else (pos["entry"] - b["low"])
            adv = (b["low"] - pos["entry"]) if pos["d"] > 0 else (pos["entry"] - b["high"])
            pos["mfe"] = max(pos["mfe"], fav / pos["sd"])
            pos["mae"] = min(pos["mae"], adv / pos["sd"])
            hit_sl = (b["low"] <= pos["stop"]) if pos["d"] > 0 else (b["high"] >= pos["stop"])
            hit_tg = (b["high"] >= pos["target"]) if pos["d"] > 0 else (b["low"] <= pos["target"])
            exit_px = why = None
            if hit_sl:
                exit_px, why = pos["stop"], "SL"
            elif hit_tg:
                exit_px, why = pos["target"], "TARGET"
            elif (t + dt.timedelta(minutes=5)).time() >= EOD_EXIT or tn.date() != t.date():
                exit_px, why = b["close"], "EOD"
            if exit_px is not None:
                q = pos["qty"]
                buy, sell = (pos["entry"], exit_px) if pos["d"] > 0 else (exit_px, pos["entry"])
                ch = charges("fut", exch, buy * q, sell * q)
                gross = pos["d"] * (exit_px - pos["entry"]) * q
                trades.append({"entry_ts": pos["ts"], "exit_ts": b["ts"], "setup": pos["setup"], "dir": pos["d"],
                               "entry": pos["entry"], "exit": exit_px, "why": why, "gross": gross, "charges": ch,
                               "net": gross - ch, "r": pos["d"] * (exit_px - pos["entry"]) / pos["sd"],
                               "mfe_r": pos["mfe"], "mae_r": pos["mae"], "entry_reason": pos["reason"],
                               "bars_held": int(round((b["ts"] - pos["ts"]) / 300)) + 1})
                pos = None
            continue
        if not (ENTRY_START <= t.time() < ENTRY_END) or tn.date() != t.date():
            continue
        j = bisect.bisect_left(ddays, t.date()) - 1
        lv = pivots(dmap[ddays[j]]["high"], dmap[ddays[j]]["low"], dmap[ddays[j]]["close"]) if j >= 0 else {}
        sig = signal_at(x, i, lv, p)
        if sig["signal"] in ("LONG", "SHORT"):
            d = 1 if sig["signal"] == "LONG" else -1
            entry, sd = nb["open"], sig["stop_dist"]
            mr = sig["setup"] == "MEAN_REVERSION"
            tgt = sig["targets"][0] if mr else sig["targets"][-1]
            if (tgt - entry) * d <= 0:
                continue
            pos = {"ts": nb["ts"], "d": d, "entry": entry, "stop": entry - d * sd,
                   "target": tgt if mr else entry + d * 2 * sd, "sd": sd, "setup": sig["setup"], "qty": lot,
                   "mfe": 0.0, "mae": 0.0, "reason": sig["reason"]}
    res = {"status": "OK" if trades else "NO TRADES", "bars": len(bars5), "trades": len(trades), "lot": lot,
           "from": dt.datetime.fromtimestamp(bars5[0]["ts"], IST).strftime("%Y-%m-%d"),
           "to": dt.datetime.fromtimestamp(bars5[-1]["ts"], IST).strftime("%Y-%m-%d"), "trades_list": trades}
    if not trades:
        return res
    net = [t["net"] for t in trades]
    wins = sum(1 for v in net if v > 0)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for v in net:
        eq += v
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    gp, gl = sum(v for v in net if v > 0), -sum(v for v in net if v < 0)
    by = {}
    for t in trades:
        s = by.setdefault(t["setup"], {"n": 0, "net": 0.0, "wins": 0})
        s["n"] += 1
        s["net"] += t["net"]
        s["wins"] += t["net"] > 0
    exits = {}
    for t in trades:
        exits[t["why"]] = exits.get(t["why"], 0) + 1
    res.update({"gross_rupees": sum(t["gross"] for t in trades), "charges_rupees": sum(t["charges"] for t in trades),
                "net_rupees": sum(net), "win_rate_ci95": wilson(wins, len(net)), "avg_net": sum(net) / len(net),
                "avg_r": sum(t["r"] for t in trades) / len(trades), "profit_factor": gp / gl if gl > 0 else float("inf"),
                "max_drawdown_rupees": mdd, "by_setup": by, "exits": exits,
                "sample_status": "OK" if len(trades) >= 30 else "INSUFFICIENT (<30 trades): not evidence"})
    return res


def backtest_fixed(bars5, bars1, dly, lot, exch, sl_pts, tg_pts, p=PARAMS):
    """Same entries as backtest() (signal on closed 5m bar, fill at next 5m open), but a FIXED stop and
    target in points. Exits are walked on 1-minute bars; if stop and target fall in the same minute the
    stop is assumed first. One position at a time; flat at 15:20."""
    x = compute(bars5)
    dmap = {dt.datetime.fromtimestamp(d["ts"], IST).date(): d for d in dly}
    ddays = sorted(dmap)
    ts1 = [b["ts"] for b in bars1]
    trades, busy = [], 0.0
    for i in range(1, len(bars5) - 1):
        b, nb = bars5[i], bars5[i + 1]
        if nb["ts"] < busy:
            continue
        t = dt.datetime.fromtimestamp(b["ts"], IST)
        tn = dt.datetime.fromtimestamp(nb["ts"], IST)
        if not (ENTRY_START <= t.time() < ENTRY_END) or tn.date() != t.date():
            continue
        j = bisect.bisect_left(ddays, t.date()) - 1
        lv = pivots(dmap[ddays[j]]["high"], dmap[ddays[j]]["low"], dmap[ddays[j]]["close"]) if j >= 0 else {}
        sig = signal_at(x, i, lv, p)
        if sig["signal"] not in ("LONG", "SHORT"):
            continue
        k = bisect.bisect_left(ts1, nb["ts"])
        if k >= len(bars1) or bars1[k]["ts"] - nb["ts"] > 120:
            continue
        d = 1 if sig["signal"] == "LONG" else -1
        entry = bars1[k]["open"]
        stop, tgt = entry - d * sl_pts, entry + d * tg_pts
        exit_px = why = None
        ex_ts = nb["ts"]
        while k < len(bars1):
            m = bars1[k]
            mt = dt.datetime.fromtimestamp(m["ts"], IST)
            if mt.date() != tn.date():
                exit_px, why, ex_ts = bars1[k - 1]["close"], "EOD", bars1[k - 1]["ts"]
                break
            if (m["low"] <= stop) if d > 0 else (m["high"] >= stop):
                exit_px, why, ex_ts = stop, "SL", m["ts"]
                break
            if (m["high"] >= tgt) if d > 0 else (m["low"] <= tgt):
                exit_px, why, ex_ts = tgt, "TARGET", m["ts"]
                break
            if (mt + dt.timedelta(minutes=1)).time() >= EOD_EXIT:
                exit_px, why, ex_ts = m["close"], "EOD", m["ts"]
                break
            k += 1
        if exit_px is None:
            break
        buy, sell = (entry, exit_px) if d > 0 else (exit_px, entry)
        ch = charges("fut", exch, buy * lot, sell * lot)
        pts = d * (exit_px - entry)
        trades.append({"ts": nb["ts"], "setup": sig["setup"], "dir": d, "pts": pts, "why": why,
                       "gross": pts * lot, "charges": ch, "net": pts * lot - ch})
        busy = ex_ts + 60
    return trades


def summarize_fixed(label, tr, lot):
    if not tr:
        return "%-14s no trades" % label
    n = len(tr)
    w = sum(1 for t in tr if t["net"] > 0)
    pts = sum(t["pts"] for t in tr)
    net = sum(t["net"] for t in tr)
    ch = sum(t["charges"] for t in tr)
    gp = sum(t["net"] for t in tr if t["net"] > 0)
    gl = -sum(t["net"] for t in tr if t["net"] < 0)
    ex = {}
    for t in tr:
        ex[t["why"]] = ex.get(t["why"], 0) + 1
    return ("%-14s trades %3d | wins %3d (%4.1f%%) | pts/trade %+6.1f | total pts %+7.0f | charges/trade %4.1f pts | "
            "net Rs%+8.0f | PF %4.2f | exits %s" % (label, n, w, 100.0 * w / n, pts / n, pts, ch / n / lot, net,
                                                    gp / gl if gl > 0 else float("inf"), ex))


def outcome_reason(t):
    """Plain-language reason a trade won or lost (from exit type, best/worst excursion in R, charges)."""
    if t["net"] > 0:
        if t["why"] == "TARGET":
            return "target hit (+%.1fR) - move continued as signalled" % t["r"]
        return "time exit 15:20 in profit (+%.2fR, best +%.1fR)" % (t["r"], t["mfe_r"])
    if t["gross"] > 0:
        return "small gross gain Rs%.0f eaten by charges Rs%.0f" % (t["gross"], t["charges"])
    if t["why"] == "SL":
        if t["mfe_r"] >= 1.0:
            return "was +%.1fR in profit, then reversed to stop (no trailing/breakeven stop)" % t["mfe_r"]
        if t["mfe_r"] < 0.3:
            return "went against immediately (best only +%.1fR) - false signal" % t["mfe_r"]
        return "small favourable move (+%.1fR) then stopped out at 2xATR" % t["mfe_r"]
    if t["why"] == "EOD":
        return "no follow-through; closed at 15:20 at %.2fR (best +%.1fR)" % (t["r"], t["mfe_r"])
    return "target reached but net negative after charges"


def print_trade_lists(name, tl):
    wins = [t for t in tl if t["net"] > 0]
    losses = [t for t in tl if t["net"] <= 0]
    for title, rows in (("WIN", wins), ("LOSS", losses)):
        log("----- %s %s TRADES (%d) -----" % (name, title, len(rows)))
        for t in rows:
            log("  %s %s %-16s %-5s entry %.1f exit %.1f %-6s %dbars net Rs%.0f (chg %.0f) | ENTRY: %s | RESULT: %s" % (
                title, dt.datetime.fromtimestamp(t["entry_ts"], IST).strftime("%Y-%m-%d %H:%M"), t["setup"],
                "LONG" if t["dir"] > 0 else "SHORT", t["entry"], t["exit"], t["why"], t["bars_held"], t["net"],
                t["charges"], t["entry_reason"], outcome_reason(t)))
    cats = {}
    for t in losses:
        r = outcome_reason(t)
        k = ("GAVE BACK PROFIT (was >= +1R, hit stop)" if r.startswith("was +") else
             "FALSE SIGNAL (went against immediately)" if r.startswith("went against") else
             "WEAK MOVE then stopped at 2xATR" if r.startswith("small favourable") else
             "CHARGES turned small gain into loss" if r.startswith("small gross") else
             "NO FOLLOW-THROUGH (closed 15:20)" if r.startswith("no follow") else r)
        c = cats.setdefault(k, [0, 0.0])
        c[0] += 1
        c[1] += t["net"]
    log("----- %s LOSS REASONS SUMMARY -----" % name)
    for k, (n, v) in sorted(cats.items(), key=lambda kv: kv[1][1]):
        log("  %-75s n=%-3d net Rs%.0f" % (k, n, v))
    for side in (1, -1):
        rows = [t for t in tl if t["dir"] == side]
        if rows:
            log("  %s: %d trades, wins %d, net Rs%.0f" % ("LONG " if side > 0 else "SHORT", len(rows),
                                                          sum(t["net"] > 0 for t in rows), sum(t["net"] for t in rows)))
    hours = {}
    for t in tl:
        h = dt.datetime.fromtimestamp(t["entry_ts"], IST).hour
        e = hours.setdefault(h, [0, 0, 0.0])
        e[0] += 1
        e[1] += t["net"] > 0
        e[2] += t["net"]
    log("  by entry hour: " + "  ".join("%02d:00 n=%d w=%d Rs%.0f" % (h, *hours[h]) for h in sorted(hours)))


def pull_and_backtest(months):
    log("=== DATA PULL + BACKTEST: last %s months of 5m data ===" % months)
    futs = all_futures()
    today = now_ist().date()
    start = today - dt.timedelta(days=int(months * 30.5))
    summary = {}
    for name, spec in INDICES.items():
        try:
            dly = daily(spec["sid"], int(months * 31) + 15)
            idx5 = intraday(spec["sid"], "IDX_I", "INDEX", start, today, 5)
            per, exp = {}, {}
            for f in futs.get(name, []):
                per[f["sid"]] = intraday(f["sid"], f["segment"], "FUTIDX", start, today, 5, oi=True)
                exp[f["sid"]] = f["expiry"]
            fut5 = stitch_front_month(per, exp)
            per1 = {sid: intraday(sid, futs[name][[f["sid"] for f in futs[name]].index(sid)]["segment"], "FUTIDX",
                                  start, today, 1) for sid in exp} if FIXED_POINT_TESTS else {}
            fut1 = stitch_front_month(per1, exp) if per1 else []
            lot = futs[name][0]["lot"] if futs.get(name) else 1.0
            log("%s: daily %d, index 5m %d (%s..), front-month futures 5m %d (%s..), lot %s" % (
                name, len(dly), len(idx5), _d(idx5), len(fut5), _d(fut5), lot))
            r = backtest(fut5, dly, lot, spec["exch"])
            tl = r.pop("trades_list", [])
            summary[name] = r
            log("%s RESULT: %s" % (name, {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "by_setup"}))
            log("%s by setup: %s" % (name, r.get("by_setup")))
            if PRINT_ALL_TRADES:
                print_trade_lists(name, tl)
            if FIXED_POINT_TESTS and fut1:
                log("----- %s STOP/TARGET COMPARISON (same entries; exits on 1m bars, %d bars) -----" % (name, len(fut1)))
                log("  " + summarize_fixed("2xATR / 4xATR", [{"pts": t["net"] / lot + t["charges"] / lot, "net": t["net"],
                                                              "charges": t["charges"], "why": t["why"]} for t in tl], lot))
                for slp, tgp in FIXED_POINT_TESTS:
                    log("  " + summarize_fixed("SL %d / TGT %d" % (slp, tgp),
                                               backtest_fixed(fut5, fut1, dly, lot, spec["exch"], slp, tgp), lot))
        except Exception:
            log(name, "backtest failed:", traceback.format_exc()[-600:])
    tot = sum(v.get("net_rupees", 0) for v in summary.values())
    log("=== TOTAL net across indices: Rs%.0f ===" % tot)
    log("NOTE: Dhan lists only live futures contracts, so futures history starts at the oldest live contract's listing "
        "(~3 months). Index bars carry no volume (no VWAP), so they are reported but not traded.")
    return summary


def _d(rows):
    return dt.datetime.fromtimestamp(rows[0]["ts"], IST).strftime("%Y-%m-%d") if rows else "none"


# ============================================================= SELF-TEST ==
def selftest():
    res = []

    def check(name, cond):
        res.append(bool(cond))
        log("[%s] %s" % ("PASS" if cond else "FAIL", name))
    t0 = dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp()

    def mk(cl, sp=2.0):
        return [{"ts": t0 + i * 300, "open": cl[i - 1] if i else c, "high": max(cl[i - 1] if i else c, c) + sp,
                 "low": min(cl[i - 1] if i else c, c) - sp, "close": c, "volume": 100.0} for i, c in enumerate(cl)]
    b = [{"ts": t0, "high": 12, "low": 6, "close": 9, "volume": 10}, {"ts": t0 + 300, "high": 15, "low": 9, "close": 12, "volume": 30},
         {"ts": t0 + 86400, "high": 30, "low": 30, "close": 30, "volume": 5}]
    v = vwap(b)
    check("VWAP typical price + session reset", v[0] == 9 and abs(v[1] - 11.25) < 1e-12 and v[2] == 30)
    tb = [{"high": 10, "low": 8, "close": 9}, {"high": 12, "low": 11, "close": 11.5}, {"high": 11, "low": 7, "close": 8}]
    check("True range", true_range(tb) == [2, 3, 4.5])
    w = wilder([1, 2, 3, 4, 5, 6.0], 3)
    check("Wilder smoothing", w[2] == 2 and abs(w[3] - 8 / 3) < 1e-12)
    up = mk([100 + i for i in range(60)])
    a, dp, dm = adx(up)
    check("RSI = 100 on rising series", abs(rsi(up)[-1] - 100) < 1e-9)
    check("ADX = 100, DI- = 0 on rising series", abs(a[-1] - 100) < 1e-6 and dm[-1] == 0)
    cl = [float(i) for i in range(1, 31)]
    bb = bollinger(mk(cl))
    m = sum(cl[-20:]) / 20
    sd = math.sqrt(sum((q - m) ** 2 for q in cl[-20:]) / 20)
    check("Bollinger bandwidth = 4*sd/mid", abs(bb["bandwidth"][-1] - 4 * sd / m) < 1e-12)
    check("Efficiency ratio = 1 on straight line", efficiency_ratio(mk(cl))[-1] == 1.0)
    check("Z-score(20)", abs(zscore(mk(cl))[-1] - (30 - m) / sd) < 1e-12)
    check("Pivots", pivots(110, 90, 100) == {"P": 100, "R1": 110, "S1": 90, "R2": 120, "S2": 80, "PDH": 110, "PDL": 90, "PDC": 100})
    ln, dr = supertrend(mk([100 + i for i in range(40)] + [140 - 3 * i for i in range(40)], 0.5))
    check("Supertrend flips long -> short on close cross", dr[38] == 1 and dr[-1] == -1)
    F = 100 * math.exp(0.05)
    check("Black-76 vs Hull reference 10.4506", abs(black76(F, 100, 1, 0.05, 0.2, True) - 10.4506) < 1e-3)
    iv = implied_vol(black76(24000, 24100, 0.02, 0.065, 0.14, True), 24000, 24100, 0.02, 0.065, True)
    check("Implied vol round trip", iv is not None and abs(iv - 0.14) < 1e-6)
    check("Option charges: STT 0.1% of sell + Rs40 brokerage", charges("opt", "NSE", 100000, 110000) > 150)
    blocked = False
    try:
        post("/orders", {})
    except ValueError:
        blocked = True
    check("Order endpoints blocked", blocked)
    import random
    rnd = random.Random(7)
    px, bars, day, k = 24000.0, [], 0, 0
    for i in range(3000):
        if k == 75:
            day, k = day + 1, 0
        px *= math.exp(rnd.gauss(0, 0.0012))
        o = bars[-1]["close"] if bars else px
        bars.append({"ts": t0 + day * 86400 + k * 300, "open": o, "high": max(o, px) + abs(rnd.gauss(0, 8)),
                     "low": min(o, px) - abs(rnd.gauss(0, 8)), "close": px, "volume": float(rnd.randint(1000, 50000))})
        k += 1
    dl = [{"ts": t0 + d * 86400 - 86400, "high": 24300, "low": 23700, "close": 24000} for d in range(60)]
    r1 = backtest(bars, dl, 65, "NSE")
    ok_cost = r1["status"] == "OK" and all(abs(t["net"] - (t["gross"] - t["charges"])) < 1e-9 for t in r1["trades_list"])
    check("Backtest: trades generated, net = gross - charges", ok_cost)
    b2 = [dict(x) for x in bars]
    for x in b2[2000:]:
        for f in ("open", "high", "low", "close"):
            x[f] *= 1.05
    r2 = backtest(b2, dl, 65, "NSE")
    cut = bars[1990]["ts"]
    check("Backtest has no look-ahead (changing future bars leaves past trades unchanged)",
          [t for t in r1["trades_list"] if t["exit_ts"] < cut] == [t for t in r2["trades_list"] if t["exit_ts"] < cut])
    log("SELF-TEST: %d/%d passed" % (sum(res), len(res)))
    return all(res)


# ============================================================== LIVE LOOP ==
def in_session(t):
    return t.weekday() < 5 and dt.time(9, 15) <= t.time() <= dt.time(15, 30)


def cycle(state):
    now = time.time()
    today = now_ist().date()
    if state.get("day") != today:
        futs = all_futures()
        state["futs"] = {n: (v[0] if v else None) for n, v in futs.items()}
        state["prev"] = {n: prev_day(daily(s["sid"], 15), today) for n, s in INDICES.items()}
        state["day"] = today
        log("instruments:", {n: (f["display"], f["sid"], f["lot"], f["expiry"]) for n, f in state["futs"].items() if f})
    vix_v = vix()
    for name, spec in INDICES.items():
        fut = state["futs"].get(name)
        if not fut:
            continue
        try:
            b1 = intraday(fut["sid"], fut["segment"], "FUTIDX", today - dt.timedelta(days=6), today, 1)
            expiry, legs = option_chain(spec["sid"])
            card, table = build_card(name, b1, state["prev"].get(name), legs, expiry or fut["expiry"], vix_v, fut["lot"], now)
            msg = card_text(card, table)
            log(msg.replace("\n", " | "))
            key = (name, card.get("bar"), card["signal"])
            if card["signal"] in ("LONG", "SHORT") and key not in state["sent"]:
                state["sent"].add(key)
                telegram(msg)
        except Exception:
            log(name, "error:", traceback.format_exc()[-500:])


def run():
    log("DHAN QUANT single-file strategy starting (read-only)")
    if RUN_SELFTEST_ON_START:
        selftest()
    if not configured():
        log("Credentials missing: Env Variables tab -> add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN "
            "(optional TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), then Deploy again. client_id set=%s token set=%s"
            % (not CLIENT_ID.startswith("{{"), not ACCESS_TOKEN.startswith("{{")))
    elif RUN_BACKTEST_ON_START:
        try:
            pull_and_backtest(BACKTEST_MONTHS)
        except Exception:
            log("data pull failed:", traceback.format_exc()[-800:])
    state = {"sent": set(), "last_bar": None}
    while True:
        t = now_ist()
        bar = (t.hour * 60 + t.minute) // 5
        if configured() and in_session(t) and bar != state["last_bar"] and t.second >= 5:
            state["last_bar"] = bar
            try:
                cycle(state)
            except Exception:
                log("cycle error:", traceback.format_exc()[-800:])
        time.sleep(LOOP_SLEEP_S)


if __name__ == "__main__":
    run()
