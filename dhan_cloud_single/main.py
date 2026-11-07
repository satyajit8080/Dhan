"""
NIFTY QUANT ENGINE - single-file Dhan Cloud build (NIFTY only, read-only, never places orders)

MODES (set below):
  RUN_AUDIT_ON_START   full institutional audit of the NIFTY strategy (data quality, reproduction of the
                       reported numbers, corrections, execution scenarios, statistics, filter attribution,
                       permutation / bootstrap / Monte Carlo / Deflated Sharpe, regimes, ML research)
  LIVE loop            closed-5m-bar signals on NIFTY front-month futures with filters F3 + F4,
                       Telegram cards, and a PAPER LEDGER that is rebuilt deterministically from Dhan data
                       (survives restarts, unique trade ids, lifecycle states, no duplicates)

DATA SETS ARE KEPT SEPARATE: NIFTY index (spot, IDX_I 13), NIFTY futures (NSE_FNO, stitched front month,
additively back-adjusted for indicators only), NIFTY options (option chain, Black-76 on the carry-adjusted
futures forward).
"""
import bisect
import csv
import datetime as dt
import hashlib
import math
import random
import time
import traceback

import requests

# =============================================================== CONFIG ==
# Dhan Cloud replaces {{NAME}} with the Variable's value (Env Variables tab).


def _var(v):
    return str(v).strip().strip('"').strip("'").strip()


def _first(*vals):
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
RUN_AUDIT_ON_START = True          # NIFTY audit report in the logs (takes ~1-3 minutes)
AUDIT_MONTHS = 6
PERMUTATIONS = 400                 # random-entry permutation test size
BOOTSTRAPS = 3000
LIVE = True
LIVE_FILTER = {"entry_end": dt.time(13, 0), "min_break_bps": 2.0}   # F3 + F4 (frozen; not re-optimised)
PAPER_TRADE = True
LEDGER_START = "2026-11-09"        # first paper-trading session; the ledger is replayed from here on every restart
EXEC_LIVE = "realistic"            # execution scenario used for the paper ledger

# research-history bookkeeping: every rule/filter variant evaluated on this data so far (for Deflated Sharpe)
VARIANTS_TESTED = 27               # 3 setups x baseline + 7 single filters + 3 combos + 4 ORB + 4 fixed-point + 5 indices + F3+F4+order flow

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
INDICES = {"NIFTY": {"sid": 13, "exch": "NSE", "step": 50}}   # NIFTY 50, IDX_I security id 13 (Dhan instrument master)
VIX_SID = 21
PARAMS = {"adx_trend": 25.0, "er_trend": 0.3, "adx_mr": 20.0, "z_mr": 2.0, "rsi_lo": 40.0, "rsi_hi": 55.0,
          "squeeze_lookback": 20, "squeeze_memory": 5, "level_atr": 0.2, "stop_atr": 2.0,
          "risk_rupees": 5000.0, "max_spread_pct": 1.0, "pcr_low": 0.7, "pcr_high": 1.3}
RISK_FREE = 0.065
LOOP_SLEEP_S = 20
ENTRY_START, ENTRY_END, EOD_EXIT = dt.time(9, 30), dt.time(15, 10), dt.time(15, 20)
SESSION_BARS_5M = 75               # 09:15 ... 15:25

# Execution scenarios. slip = adverse points per market fill (entry, stop, time exit). Targets are limit orders.
#   legacy     = engine before this audit (stop and target at exact price, no gaps, no slippage)
#   optimistic = no slippage, target assumed first when both are touched in one bar
#   realistic  = 1.0 pt slippage, gaps through the stop fill at the open, stop assumed first
#   stress     = 3.0 pt slippage, gap fills, stop first, target needs a 0.5 pt trade-through
EXEC = {
    "legacy": {"slip": 0.0, "gap": False, "both": "sl", "tg_through": 0.0},
    "optimistic": {"slip": 0.0, "gap": False, "both": "tg", "tg_through": 0.0},
    "realistic": {"slip": 1.0, "gap": True, "both": "sl", "tg_through": 0.0},
    "stress": {"slip": 3.0, "gap": True, "both": "sl", "tg_through": 0.5},
}

NAN = float("nan")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def log(*a):
    print(dt.datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def configured():
    return not CLIENT_ID.startswith("{{") and not ACCESS_TOKEN.startswith("{{")


def ok(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


def ist(ts):
    return dt.datetime.fromtimestamp(ts, IST)


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



# ===================================================== CHARGES (INDIA) ==
# Statutory schedule. STT was raised by Union Budget 2026, effective 01-Apr-2026:
#   futures 0.02% -> 0.05% of sell value; options 0.10% -> 0.15% of sell premium.
# NSE exchange transaction charge: futures 0.00173%, options 0.03503% of turnover (Oct-2024 schedule).
# SEBI fee Rs10/crore; stamp duty on buy side: futures 0.002%, options 0.003%; GST 18% on brokerage+txn+SEBI.
# Brokerage: Rs20 per executed order (Dhan flat fee).
CHARGES = {
    2026: {"fut": {"stt_sell": 0.0005, "txn_NSE": 0.0000173, "stamp_buy": 0.00002},
           "opt": {"stt_sell": 0.0015, "txn_NSE": 0.0003503, "stamp_buy": 0.00003}},
    2025: {"fut": {"stt_sell": 0.0002, "txn_NSE": 0.0000173, "stamp_buy": 0.00002},
           "opt": {"stt_sell": 0.001, "txn_NSE": 0.0003503, "stamp_buy": 0.00003}},
}
BROKERAGE_PER_ORDER, SEBI_RATE, GST = 20.0, 0.000001, 0.18
STT_CHANGE_DATE = dt.date(2026, 4, 1)


def charges(kind, exch, buy_value, sell_value, year=None, trade_date=None):
    """Round-trip statutory + brokerage charges in Rupees."""
    if year is None:
        year = 2026 if (trade_date is None or trade_date >= STT_CHANGE_DATE) else 2025
    r = CHARGES[year][kind]
    brk = 2 * BROKERAGE_PER_ORDER
    txn = r["txn_NSE"] * (buy_value + sell_value)
    sebi = SEBI_RATE * (buy_value + sell_value)
    return brk + r["stt_sell"] * sell_value + txn + sebi + r["stamp_buy"] * buy_value + GST * (brk + txn + sebi)


def lots_for(risk_rupees, stop_pts, lot):
    return int(risk_rupees // (stop_pts * lot)) if stop_pts > 0 and lot > 0 else 0


# ========================================================= OPTIONS MATH ==
def norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black76(F, K, T, r, s, call):
    df = math.exp(-r * T)
    if T <= 0 or s <= 0:
        return df * max((F - K) if call else (K - F), 0.0)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return df * (F * norm_cdf(d1) - K * norm_cdf(d2)) if call else df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def greeks76(F, K, T, r, s, call):
    """Black-76 Greeks. delta dV/dF, gamma per pt^2, vega per 1 vol pt, theta per calendar day,
    rho per 1% rate, vanna d(delta)/d(vol pt), charm d(delta)/d(calendar day)."""
    df = math.exp(-r * T)
    sq = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s * T) / (s * sq)
    d2 = d1 - s * sq
    n1 = norm_pdf(d1)
    price = black76(F, K, T, r, s, call)
    delta = df * (norm_cdf(d1) if call else norm_cdf(d1) - 1.0)
    gamma = df * n1 / (F * s * sq)
    vega = df * F * n1 * sq * 0.01
    theta = -(-r * price + df * F * n1 * s / (2 * sq)) / 365.0
    rho = -T * price * 0.01
    vanna = -df * n1 * d2 / s * 0.01
    ddelta_dT = -r * delta + df * n1 * (-d2 / (2 * T))
    charm = -ddelta_dT / 365.0
    return {"price": price, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho,
            "vanna": vanna, "charm": charm}


def implied_vol(px, F, K, T, r, call):
    """Bisection on Black-76 (monotone in vol). None if outside no-arbitrage bounds."""
    lo, hi = 1e-4, 5.0
    if px <= 0 or T <= 0 or black76(F, K, T, r, hi, call) < px or black76(F, K, T, r, lo, call) > px:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, r, mid, call) > px:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def parity_residual(C, P, F, K, T, r):
    """C - P - DF(F - K): zero under put-call parity."""
    return C - P - math.exp(-r * T) * (F - K)


def year_frac(expiry, now=None):
    now = now or dt.datetime.now(IST)
    e = dt.datetime.fromisoformat(expiry[:10] + "T15:30:00+05:30")
    return max((e - now).total_seconds() / (365.0 * 86400), 1.0 / (365 * 1440))


def option_forward(fut_px, fut_expiry, opt_expiry, r=RISK_FREE, now=None):
    """Forward for an option expiry from the listed future: F_opt = F_fut * exp(-r (T_fut - T_opt))."""
    return fut_px * math.exp(-r * (year_frac(fut_expiry, now) - year_frac(opt_expiry, now)))


def max_pain(legs):
    ks = sorted({k for k, _ in legs})
    best, bk = None, None
    for s in ks:
        pay = sum(float(o.get("oi") or 0) * max(s - k, 0) for (k, sd), o in legs.items() if sd == "CE") + \
            sum(float(o.get("oi") or 0) * max(k - s, 0) for (k, sd), o in legs.items() if sd == "PE")
        if best is None or pay < best:
            best, bk = pay, s
    return bk


def iv_rank(hist, iv):
    h = [x for x in hist if ok(x)]
    if len(h) < 20 or not ok(iv):
        return NAN, NAN
    lo, hi = min(h), max(h)
    rank = (iv - lo) / (hi - lo) * 100 if hi > lo else NAN
    pct = 100.0 * sum(1 for x in h if x < iv) / len(h)
    return rank, pct


def chain_analytics(legs, F, T, step, r=RISK_FREE):
    """ATM IV (own Black-76 on the futures forward), 2-strike skew, parity check, max pain, PCR, OI-weighted
    gamma concentration and a dealer-GEX sign convention (estimate only)."""
    if not legs or not ok(F):
        return {}
    ks = sorted({k for k, _ in legs})
    atm = min(ks, key=lambda k: abs(k - F))

    def mid(o):
        b, a = float(o.get("top_bid_price") or 0), float(o.get("top_ask_price") or 0)
        return (a + b) / 2 if a > 0 and b > 0 and (a - b) <= 0.05 * (a + b) / 2 else float(o.get("last_price") or 0)
    out = {"atm": atm}
    ivs = {}
    for side in ("CE", "PE"):
        o = legs.get((atm, side))
        if o:
            ivs[side] = implied_vol(mid(o), F, atm, T, r, side == "CE")
    if ivs.get("CE") and ivs.get("PE"):
        out["atm_iv"] = (ivs["CE"] + ivs["PE"]) / 2
    vend = [float(legs[(atm, s)].get("implied_volatility") or 0) / 100 for s in ("CE", "PE") if (atm, s) in legs]
    out["atm_iv_vendor"] = sum(vend) / len(vend) if vend else NAN
    oc, op = legs.get((atm + 2 * step, "CE")), legs.get((atm - 2 * step, "PE"))
    if oc and op:
        ic = implied_vol(mid(oc), F, atm + 2 * step, T, r, True)
        ip = implied_vol(mid(op), F, atm - 2 * step, T, r, False)
        if ic and ip:
            out["skew_2strike"] = ip - ic
    ce, pe = legs.get((atm, "CE")), legs.get((atm, "PE"))
    if ce and pe:
        out["parity_residual"] = parity_residual(mid(ce), mid(pe), F, atm, T, r)
        out["straddle"] = mid(ce) + mid(pe)
    out["max_pain"] = max_pain(legs)
    gex, conc, tot = 0.0, {}, 0.0
    for (k, sd), o in legs.items():
        if abs(k - F) > 15 * step:
            continue
        iv = float(o.get("implied_volatility") or 0) / 100
        if iv <= 0:
            continue
        g = greeks76(F, k, T, r, iv, sd == "CE")["gamma"] * float(o.get("oi") or 0)
        gex += (g if sd == "CE" else -g) * F * F * 0.01
        conc[k] = conc.get(k, 0) + g
        tot += g
    if tot > 0:
        pk = max(conc, key=conc.get)
        out.update({"gex_sign_conv": gex, "gamma_peak": pk, "gamma_conc": conc[pk] / tot})
    return out


# ============================================================ DHAN DATA ==
_last = {}
ALLOWED = ("/charts/intraday", "/charts/historical", "/optionchain", "/optionchain/expirylist", "/marketfeed/ltp",
           "/marketfeed/quote")
SPACING = {"/optionchain": 3.2, "/optionchain/expirylist": 3.2, "/marketfeed/ltp": 1.05, "/marketfeed/quote": 1.05}


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


_SCAN = {}


def all_futures():
    """Cached daily instrument scan (futures + NIFTY 50 equities in one pass over the master)."""
    d = now_ist().date().isoformat()
    if _SCAN.get("day") != d:
        _SCAN["futs"], _SCAN["eq"] = _scan_master()
        _SCAN["day"] = d
    return _SCAN["futs"]


def nifty50_ids():
    all_futures()
    return _SCAN.get("eq", {})


def _scan_master():
    """Stream the instrument master: every live NIFTY FUTIDX contract and the NSE EQ security ids of the NIFTY 50."""
    today = now_ist().date().isoformat()
    res = {n: [] for n in INDICES}  # NIFTY only
    eq = {}
    with requests.get(SCRIP_MASTER, stream=True, timeout=180) as r:
        r.raise_for_status()
        rd = csv.reader(ln.decode("utf-8", "replace") for ln in r.iter_lines() if ln)
        hdr = [x.strip().upper() for x in next(rd)]
        ix = {k: hdr.index(k) for k in ("EXCH_ID", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL",
                                        "DISPLAY_NAME", "LOT_SIZE", "SM_EXPIRY_DATE")}
        i_ser = hdr.index("SERIES") if "SERIES" in hdr else -1
        for row in rd:
            if len(row) <= max(ix.values()):
                continue
            if row[ix["INSTRUMENT"]] == "EQUITY" and row[ix["EXCH_ID"]] == "NSE" and (i_ser < 0 or row[i_ser] == "EQ"):
                sym = row[ix["UNDERLYING_SYMBOL"]].strip().upper()
                if sym in NIFTY50:
                    eq[sym] = int(row[ix["SECURITY_ID"]])
                continue
            if row[ix["INSTRUMENT"]] != "FUTIDX":
                continue
            u = row[ix["UNDERLYING_SYMBOL"]].strip().upper()
            e = row[ix["SM_EXPIRY_DATE"]][:10]
            if u in res and e >= today:
                res[u].append({"sid": int(row[ix["SECURITY_ID"]]), "expiry": e, "lot": float(row[ix["LOT_SIZE"]] or 1),
                               "segment": "NSE_FNO" if row[ix["EXCH_ID"]] == "NSE" else "BSE_FNO",
                               "display": row[ix["DISPLAY_NAME"]]})
    for u in res:
        res[u].sort(key=lambda f: f["expiry"])
    return res, eq


def quotes_snapshot(fut_sid=None):
    """One /marketfeed/quote call: NIFTY 50 constituents (breadth) + the NIFTY future (resting order book)."""
    eq = nifty50_ids()
    req = {}
    if eq:
        req["NSE_EQ"] = sorted(eq.values())
    if fut_sid:
        req["NSE_FNO"] = [fut_sid]
    if not req:
        return None, None, 0
    d = post("/marketfeed/quote", req)
    d = d.get("data", d) or {}
    inv = {v: k for k, v in eq.items()}
    qs = {}
    for sid, q in (d.get("NSE_EQ") or {}).items():
        sym = inv.get(int(sid))
        if sym and isinstance(q, dict):
            pc = float((q.get("ohlc") or {}).get("close") or 0)
            lp = float(q.get("last_price") or 0)
            if pc <= 0 and q.get("net_change") is not None:
                pc = lp - float(q["net_change"])
            qs[sym] = {"ltp": lp, "prev_close": pc}
    fq = (d.get("NSE_FNO") or {}).get(str(fut_sid)) if fut_sid else None
    return breadth_from_quotes(qs), book_from_quote(fq), len(eq)


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


def nifty_futures():
    """Live NIFTY index futures contracts from the instrument master (nearest expiry first)."""
    return all_futures().get("NIFTY", [])


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




# ================================================ FUTURES STITCH + DATA QUALITY ==
def stitch_front_month(per, expiries, adjust=True):
    """Continuous front-month NIFTY futures. Each date uses the contract with the nearest expiry >= date
    (the expiring contract is traded on its expiry day; positions are intraday so nothing is carried).
    adjust=True adds field 'adj' (additive back-adjustment): raw + adj is a gap-free series for indicators,
    removing the artificial jump at each roll. Trade P&L in points is unchanged (all trades are intraday);
    charges must use raw prices (= adjusted - adj). Returns (bars, rolls)."""
    day_bars = {}
    for sid, bars in per.items():
        for b in bars:
            day_bars.setdefault(ist(b["ts"]).date(), {}).setdefault(sid, []).append(b)
    days = sorted(day_bars)
    choice = []
    for d in days:
        c = sorted((expiries[s], s) for s in day_bars[d] if expiries[s] >= d.isoformat())
        if c:
            choice.append((d, c[0][1]))
    rolls = []
    for k in range(1, len(choice)):
        (d0, a), (d1, bsid) = choice[k - 1], choice[k]
        if a == bsid:
            continue
        diff, how = 0.0, "none"
        a_last = day_bars[d0][a][-1]
        bb = {x["ts"]: x for x in day_bars[d0].get(bsid, [])}
        if a_last["ts"] in bb:
            diff, how = bb[a_last["ts"]]["close"] - a_last["close"], "same-bar close"
        elif day_bars[d0].get(bsid):
            diff, how = day_bars[d0][bsid][-1]["close"] - a_last["close"], "last close prev day"
        rolls.append({"date": d1.isoformat(), "from": a, "to": bsid, "diff": diff, "method": how})
    out = []
    for d, sid in choice:
        adj = 0.0
        if adjust:
            adj = sum(r["diff"] for r in rolls if r["date"] > d.isoformat())
        out += [dict(b, contract=sid, adj=adj) for b in day_bars[d][sid]]
    return out, rolls


def adjusted(bars):
    """Price series with the back-adjustment applied (identity when bars carry no 'adj')."""
    out = []
    for b in bars:
        a = b.get("adj", 0.0) or 0.0
        if a:
            out.append(dict(b, open=b["open"] + a, high=b["high"] + a, low=b["low"] + a, close=b["close"] + a))
        else:
            out.append(b)
    return out


def dq_report(bars, label, minutes=5, expect_volume=True):
    days = {}
    for b in bars:
        days.setdefault(ist(b["ts"]).date(), []).append(b)
    exp = SESSION_BARS_5M if minutes == 5 else 375
    ts = [b["ts"] for b in bars]
    dup = len(ts) - len(set(ts))
    unordered = sum(1 for i in range(1, len(ts)) if ts[i] <= ts[i - 1])
    short = {d.isoformat(): len(v) for d, v in days.items() if len(v) < exp - 5}
    gaps = []
    for d, v in days.items():
        for i in range(1, len(v)):
            g = (v[i]["ts"] - v[i - 1]["ts"]) / 60
            if g > minutes:
                gaps.append(g)
    outside = sum(1 for b in bars if not (dt.time(9, 15) <= ist(b["ts"]).time() <= dt.time(15, 30)))
    zero_vol = sum(1 for b in bars if expect_volume and not b.get("volume")) if expect_volume else 0
    bad_ohlc = sum(1 for b in bars if not (b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]))
    log("  DATA %-34s days %3d bars %6d | expected/day %d | short days %d %s | missing bars %d | dup ts %d | "
        "out-of-order %d | bars outside session %d | max intraday gap %.0f min | zero-volume %d | bad OHLC %d" % (
            label, len(days), len(bars), exp, len(short), (list(short.items())[:6] if short else ""),
            sum(max(exp - len(v), 0) for v in days.values()), dup, unordered, outside, max(gaps) if gaps else 0,
            zero_vol, bad_ohlc))
    return {"days": len(days), "short_days": short, "dup": dup, "unordered": unordered, "bad_ohlc": bad_ohlc}


def complete_5m(b1, now):
    """5-minute bars built ONLY from complete buckets: the bucket's last minute (start+4m) must exist and be closed.
    A forming or gappy last bucket never reaches the completed-candle strategy."""
    b1 = [b for b in b1 if b["ts"] + 60 <= now + 1]
    have = {b["ts"] for b in b1}
    out = []
    for b in resample(b1, 5):
        if (b["ts"] + 240) in have and b["ts"] + 300 <= now + 1:
            out.append(b)
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
            if b.get("oi") is not None:
                cur["oi"] = b["oi"]
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




def build_card(bars5, legs, opt_expiry, fut_expiry, vix_v, lot, flt=None, p=PARAMS, now=None):
    """Signal card for the LAST COMPLETE 5m bar, using exactly the engine's rules (same series, levels, filters).
    bars5 = complete 5m NIFTY futures bars (may carry 'adj'; the last session's adj is 0)."""
    card = {"index": "NIFTY", "signal": "NO_TRADE", "setup": "NO_DATA", "reason": "only %d complete 5m bars" % len(bars5)}
    if len(bars5) < 60:
        return card, {}
    A = adjusted(bars5)
    x = compute(A)
    i = len(A) - 1
    t = ist(A[i]["ts"])
    lv = prior_session_levels(A).get(t.date(), {})
    sig = signal_at(x, i, lv, p)
    flt = flt or {}
    if sig["signal"] in ("LONG", "SHORT") and not (ENTRY_START <= t.time() < flt.get("entry_end", ENTRY_END)):
        why = "FILTER_F3_TIME" if flt.get("entry_end") and t.time() >= flt["entry_end"] else "OUTSIDE_WINDOW"
        sig = {"signal": "NO_TRADE", "setup": why, "reason": "%s %s ignored at %s (%s)" % (
            sig["setup"], sig["signal"], t.strftime("%H:%M"), why)}
    if sig["signal"] in ("LONG", "SHORT") and sig["setup"] == "SQUEEZE_BREAKOUT" and flt.get("min_break_bps"):
        band = x["bb_up"][i] if sig["signal"] == "LONG" else x["bb_lo"][i]
        brk = abs(x["close"][i] - band) / x["close"][i] * 1e4
        if brk < flt["min_break_bps"]:
            sig = {"signal": "NO_TRADE", "setup": "FILTER_F4_WEAK_BREAK",
                   "reason": "squeeze %s filtered (F4): %.1f bps beyond band < %.1f" % (sig["signal"], brk, flt["min_break_bps"])}
    card = {"index": "NIFTY", "signal": sig["signal"], "setup": sig["setup"], "reason": sig["reason"],
            "bar": t.strftime("%Y-%m-%d %H:%M"), "bar_ts": A[i]["ts"]}
    table = {k: v[i] for k, v in x.items()}
    fut_px = bars5[i]["close"]
    T = year_frac(opt_expiry, now) if opt_expiry else NAN
    F = option_forward(fut_px, fut_expiry, opt_expiry, RISK_FREE, now) if opt_expiry and fut_expiry else fut_px
    ca = chain_analytics(legs, F, T, INDICES["NIFTY"]["step"]) if legs and ok(T) else {}
    pv, po = pcr(legs)
    flags = [lab for lab, v in (("PCR vol", pv), ("PCR oi", po)) if ok(v) and (v < p["pcr_low"] or v > p["pcr_high"])]
    card["context"] = ("fut %.1f fwd(%s) %.1f | ATM IV %.2f%% (vendor %.2f%%) skew2 %.2f%% | straddle %.1f | parity res %.2f | "
                       "maxpain %s | GEX(sign conv) %.3g peak %s conc %.2f | PCR vol %.2f oi %.2f%s | VIX %.2f | OI chg %s" % (
                           fut_px, opt_expiry, F, 100 * ca.get("atm_iv", NAN), 100 * ca.get("atm_iv_vendor", NAN),
                           100 * ca.get("skew_2strike", NAN), ca.get("straddle", NAN), ca.get("parity_residual", NAN),
                           ca.get("max_pain"), ca.get("gex_sign_conv", NAN), ca.get("gamma_peak"), ca.get("gamma_conc", NAN),
                           pv, po, (" EXTREME:" + ",".join(flags)) if flags else "", vix_v,
                           " ".join("%.0f%s %+.0f" % r for r in oi_change_top(legs))))
    card["analytics"] = ca
    if sig["signal"] not in ("LONG", "SHORT"):
        return card, table
    d = 1 if sig["signal"] == "LONG" else -1
    sd = sig["stop_dist"]
    card.update({"entry": fut_px, "sl": fut_px - d * sd, "targets": [fut_px + d * sd, fut_px + d * 2 * sd] if sig["setup"] != "MEAN_REVERSION"
                 else [sig["targets"][0] - (A[i].get("adj", 0) or 0)], "stop_dist": sd,
                 "fut_lots": lots_for(p["risk_rupees"], sd, lot)})
    best, notes = pick_strike(legs, F, INDICES["NIFTY"]["step"], d, p["max_spread_pct"])
    card["strike_notes"] = notes
    if not best:
        card["reason"] += " | no liquid ATM/ITM strike passed the spread check"
        return card, table
    _, K, side, bid, ask, sp = best
    iv = implied_vol((bid + ask) / 2, F, K, T, RISK_FREE, side == "CE")
    if iv:
        g = greeks76(F, K, T, RISK_FREE, iv, side == "CE")
        adj = ask - g["price"]
        sl = max(black76(F - d * sd, K, T, RISK_FREE, iv, side == "CE") + adj, 0.05)
        tg = [black76(F + (tt - fut_px), K, T, RISK_FREE, iv, side == "CE") + adj for tt in card["targets"]]
    else:
        g, sl, tg = {}, max(ask - 0.5 * sd, 0.05), [ask + 0.5 * abs(tt - fut_px) for tt in card["targets"]]
    lots = lots_for(p["risk_rupees"], ask - sl, lot)
    q = max(lots, 1) * lot
    card["option"] = {"strike": K, "side": side, "entry": round(ask, 2), "sl": round(sl, 2), "targets": [round(v, 2) for v in tg],
                      "spread_pct": round(sp, 2), "iv": round(iv * 100, 2) if iv else None, "lots": lots,
                      "delta": round(g.get("delta", NAN), 3), "gamma": g.get("gamma"), "theta_day": round(g.get("theta", NAN), 2),
                      "vega_pt": round(g.get("vega", NAN), 2), "vanna": g.get("vanna"), "charm_day": g.get("charm"),
                      "charges_at_sl": round(charges("opt", "NSE", ask * q, sl * q), 2)}
    return card, table


# ====================================================== EXECUTION ENGINE ==
def prior_session_levels(bars):
    """{date: pivots of the previous session} from the SAME series being traded (fixes the old basis error:
    index daily H/L/C was compared with futures prices that trade at a 50-200 pt premium)."""
    days = {}
    for b in bars:
        days.setdefault(ist(b["ts"]).date(), []).append(b)
    ds = sorted(days)
    out = {}
    for k in range(1, len(ds)):
        p = days[ds[k - 1]]
        out[ds[k]] = pivots(max(x["high"] for x in p), min(x["low"] for x in p), p[-1]["close"])
    return out


def backtest(bars5, lot, exch="NSE", p=PARAMS, flt=None, ex="realistic", levels="series", dly=None,
             charge_year=None, with_open=False, of_ctx=None):
    """Event-driven NIFTY backtest on CLOSED 5m bars.
      signal on bar i (data <= close of i) -> market entry at OPEN of bar i+1 (+slippage)
      exits checked from the entry bar on: stop (market, gap-aware), target (limit), 15:20 time exit
      one position at a time; flat every day; filters F1..F6 via flt
    bars may carry 'adj' (back-adjusted futures): indicators/levels use adjusted prices, charges use raw prices.
    levels: 'series' = prior-session pivots of the traded series; 'index_daily' = legacy (needs dly).
    Returns dict with trades (and the open position when with_open=True)."""
    E = EXEC[ex] if isinstance(ex, str) else ex
    slip, gap, both, thr = E["slip"], E["gap"], E["both"], E["tg_through"]
    if len(bars5) < 60:
        return {"status": "INSUFFICIENT DATA (%d bars)" % len(bars5), "trades": [], "open": None}
    A = adjusted(bars5)
    x = compute(A)
    flt = flt or {}
    ofc = of_ctx if of_ctx is not None else (of_prepare(A) if any((b.get("volume") or 0) > 0 for b in A) else None)
    if levels == "index_daily":
        dmap = {ist(d["ts"]).date(): d for d in (dly or [])}
        ddays = sorted(dmap)
        lvl = {}
        for d in {ist(b["ts"]).date() for b in A}:
            j = bisect.bisect_left(ddays, d) - 1
            lvl[d] = pivots(dmap[ddays[j]]["high"], dmap[ddays[j]]["low"], dmap[ddays[j]]["close"]) if j >= 0 else {}
    else:
        lvl = prior_session_levels(A)
    trades, pos = [], None
    day_n, day_loss = {}, {}
    n = len(A)
    for i in range(1, n):
        b = A[i]
        t = ist(b["ts"])
        last = i == n - 1
        tn = ist(A[i + 1]["ts"]) if not last else None
        if pos is not None:
            d = pos["d"]
            fav = (b["high"] - pos["entry"]) if d > 0 else (pos["entry"] - b["low"])
            adv = (b["low"] - pos["entry"]) if d > 0 else (pos["entry"] - b["high"])
            pos["mfe"] = max(pos["mfe"], fav / pos["sd"])
            pos["mae"] = min(pos["mae"], adv / pos["sd"])
            stop, tgt = pos["stop"], pos["target"]
            hit_sl = (b["low"] <= stop) if d > 0 else (b["high"] >= stop)
            hit_tg = (b["high"] >= tgt + thr) if d > 0 else (b["low"] <= tgt - thr)
            exit_px = why = None
            if hit_sl and (both == "sl" or not hit_tg):
                gapped = gap and ((b["open"] <= stop) if d > 0 else (b["open"] >= stop)) and b["ts"] != pos["entry_bar_ts"]
                exit_px = (b["open"] if gapped else stop) - d * slip
                why = "BE" if stop == pos["entry"] else ("SL_GAP" if gapped else "SL")
            elif hit_tg:
                exit_px, why = tgt, "TARGET"
            elif (t + dt.timedelta(minutes=5)).time() >= EOD_EXIT or (tn is not None and tn.date() != t.date()):
                exit_px, why = b["close"] - d * slip, "EOD"
            if exit_px is not None:
                adj = b.get("adj", 0.0) or 0.0
                q = pos["qty"]
                e_raw, x_raw = pos["entry"] - pos["adj"], exit_px - adj
                buy, sell = (e_raw, x_raw) if d > 0 else (x_raw, e_raw)
                ch = charges("fut", exch, buy * q, sell * q, charge_year, t.date())
                pts = d * (exit_px - pos["entry"])
                trades.append(dict(pos["feat"], id=trade_id("NIFTY", pos["ts"], pos["setup"], d), entry_ts=pos["ts"],
                                   exit_ts=b["ts"], setup=pos["setup"], dir=d, entry=e_raw, exit=x_raw, why=why, pts=pts,
                                   gross=pts * q, charges=ch, net=pts * q - ch, r=pts / pos["sd"], mfe_r=pos["mfe"],
                                   mae_r=pos["mae"], entry_reason=pos["reason"], sd=pos["sd"], date=t.date().isoformat(),
                                   bars_held=int(round((b["ts"] - pos["ts"]) / 300)) + 1))
                if pts * q - ch <= 0:
                    day_loss[ist(pos["ts"]).date()] = True
                pos = None
            elif flt.get("breakeven_r") and pos["mfe"] >= flt["breakeven_r"]:
                pos["stop"] = max(pos["stop"], pos["entry"]) if d > 0 else min(pos["stop"], pos["entry"])
            continue
        if last or not (ENTRY_START <= t.time() < flt.get("entry_end", ENTRY_END)) or tn.date() != t.date():
            continue
        if flt.get("max_trades_per_day") and day_n.get(t.date(), 0) >= flt["max_trades_per_day"]:
            continue
        if flt.get("stop_after_loss") and day_loss.get(t.date()):
            continue
        sig = signal_at(x, i, lvl.get(t.date(), {}), p)
        if sig["signal"] not in ("LONG", "SHORT"):
            continue
        if flt.get("setups") and sig["setup"] not in flt["setups"]:
            continue
        brk = NAN
        if sig["setup"] == "SQUEEZE_BREAKOUT":
            band = x["bb_up"][i] if sig["signal"] == "LONG" else x["bb_lo"][i]
            brk = abs(x["close"][i] - band) / x["close"][i] * 1e4
            if flt.get("min_break_bps") and brk < flt["min_break_bps"]:
                continue
        d = 1 if sig["signal"] == "LONG" else -1
        of_st = orderflow_state(A, ofc, i) if ofc else None
        of_agree = bool(of_st and of_st["signal"] == ("OF_BULLISH" if d > 0 else "OF_BEARISH"))
        if flt.get("of_confirm") and not of_agree:
            continue
        nb = A[i + 1]
        entry, sd = nb["open"] + d * slip, sig["stop_dist"]
        mr = sig["setup"] == "MEAN_REVERSION"
        tgt = sig["targets"][0] if mr else entry + d * 2 * sd
        if (tgt - entry) * d <= 0:
            continue
        day_n[t.date()] = day_n.get(t.date(), 0) + 1
        feat = {"f_adx": x["adx"][i], "f_er": x["er"][i], "f_rsi": x["rsi"][i], "f_bw": x["bw"][i], "f_z": x["z"][i],
                "f_brk": brk if ok(brk) else 0.0, "f_hour": t.hour + t.minute / 60.0,
                "f_atr_bps": x["atr"][i] / x["close"][i] * 1e4,
                "f_vwap_bps": ((x["close"][i] / x["vwap"][i] - 1) * 1e4 * d) if ok(x["vwap"][i]) else 0.0,
                "f_di": (x["di_plus"][i] - x["di_minus"][i]) * d, "f_dir": d,
                "of_agree": of_agree if of_st else None,
                "of_score": of_st["score"] if of_st and ok(of_st.get("score", NAN)) else None,
                "of_side": of_st["side"] if of_st else None}
        pos = {"ts": nb["ts"], "entry_bar_ts": nb["ts"], "signal_ts": b["ts"], "d": d, "entry": entry,
               "stop": entry - d * sd, "target": tgt, "sd": sd, "setup": sig["setup"], "qty": lot, "mfe": 0.0, "mae": 0.0,
               "reason": sig["reason"], "adj": nb.get("adj", 0.0) or 0.0, "feat": feat}
    res = {"status": "OK" if trades else "NO TRADES", "trades": trades, "bars": n}
    if with_open:
        res["open"] = pos
    return res


def trade_id(sym, entry_ts, setup, d):
    return hashlib.sha1(("%s|%d|%s|%d" % (sym, int(entry_ts), setup, d)).encode()).hexdigest()[:12]


# ============================================================ STATISTICS ==
def wilson(k, n, z=1.959964):
    if n == 0:
        return NAN, NAN, NAN
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return p, max(0.0, c - h), min(1.0, c + h)


def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1) < 3e-14:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta I_x(a, b) (Numerical Recipes continued fraction)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1 - math.exp(lb) * _betacf(b, a, 1 - x) / b


def t_sf2(t, df):
    """Two-sided p-value of Student's t."""
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def mean_sd(x):
    n = len(x)
    if n < 2:
        return (x[0] if x else NAN), NAN
    m = sum(x) / n
    return m, math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))


def skew_kurt(x):
    n = len(x)
    if n < 4:
        return 0.0, 3.0
    m = sum(x) / n
    s2 = sum((v - m) ** 2 for v in x) / n
    if s2 <= 0:
        return 0.0, 3.0
    return sum((v - m) ** 3 for v in x) / n / s2 ** 1.5, sum((v - m) ** 4 for v in x) / n / s2 ** 2


def max_dd(seq):
    eq = pk = dd = 0.0
    for v in seq:
        eq += v
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def max_consec_losses(net):
    best = cur = 0
    for v in net:
        cur = cur + 1 if v <= 0 else 0
        best = max(best, cur)
    return best


def daily_pnl(tr):
    d = {}
    for t in tr:
        d[t["date"]] = d.get(t["date"], 0.0) + t["net"]
    return [d[k] for k in sorted(d)]


def summarize(tr, all_days=None):
    """Trade-level and daily statistics. all_days = number of sessions in the sample (zero-P&L days count)."""
    net = [t["net"] for t in tr]
    n = len(net)
    if n == 0:
        return {"n": 0}
    wins = [v for v in net if v > 0]
    loss = [v for v in net if v <= 0]
    m, sd = mean_sd(net)
    tstat = m / (sd / math.sqrt(n)) if n > 1 and sd > 0 else NAN
    dp = daily_pnl(tr)
    if all_days and all_days > len(dp):
        dp = dp + [0.0] * (all_days - len(dp))
    dm, dsd = mean_sd(dp)
    down = [min(v, 0.0) for v in dp]
    dd_dev = math.sqrt(sum(v * v for v in down) / len(down)) if down else NAN
    sk, ku = skew_kurt(net)
    gp, gl = sum(wins), -sum(loss)
    return {"n": n, "wins": len(wins), "win": wilson(len(wins), n), "net": sum(net), "gross": sum(t["gross"] for t in tr),
            "charges": sum(t["charges"] for t in tr), "expectancy": m, "avg_win": (gp / len(wins)) if wins else 0.0,
            "avg_loss": (-gl / len(loss)) if loss else 0.0, "payoff": (gp / len(wins)) / (gl / len(loss)) if wins and loss and gl > 0 else NAN,
            "pf": gp / gl if gl > 0 else float("inf"), "max_dd": max_dd(net), "max_consec_losses": max_consec_losses(net),
            "sharpe_trade": m / sd if sd and sd > 0 else NAN, "t": tstat, "p_t": t_sf2(tstat, n - 1) if ok(tstat) else NAN,
            "sharpe_daily_ann": dm / dsd * math.sqrt(252) if dsd and dsd > 0 else NAN,
            "sortino_daily_ann": dm / dd_dev * math.sqrt(252) if dd_dev and dd_dev > 0 else NAN,
            "skew": sk, "kurt": ku, "avg_pts": sum(t["pts"] for t in tr) / n}


def fmt_sum(s):
    if not s or s.get("n", 0) == 0:
        return "n=0"
    w = s["win"]
    return ("n=%3d win=%4.1f%% [%2.0f-%2.0f] net=Rs%+9.0f exp/trade=Rs%+7.0f avgW=%6.0f avgL=%7.0f PF=%4.2f maxDD=Rs%8.0f "
            "maxL-run=%d Sharpe(d,ann)=%5.2f Sortino=%5.2f t=%5.2f p=%.3f" % (
                s["n"], 100 * w[0], 100 * w[1], 100 * w[2], s["net"], s["expectancy"], s["avg_win"], s["avg_loss"],
                s["pf"], s["max_dd"], s["max_consec_losses"], s["sharpe_daily_ann"], s["sortino_daily_ann"],
                s["t"], s["p_t"]))


def bootstrap(net, B=BOOTSTRAPS, seed=11):
    rnd = random.Random(seed)
    n = len(net)
    if n < 5:
        return {}
    tots = sorted(sum(net[rnd.randrange(n)] for _ in range(n)) for _ in range(B))
    return {"total_ci95": (tots[int(0.025 * B)], tots[int(0.975 * B) - 1]),
            "p_total_le_0": sum(1 for v in tots if v <= 0) / B}


def mc_drawdown(net, B=2000, seed=5):
    rnd = random.Random(seed)
    if len(net) < 5:
        return {}
    dds = []
    v = list(net)
    for _ in range(B):
        rnd.shuffle(v)
        dds.append(max_dd(v))
    dds.sort()
    return {"dd_median": dds[B // 2], "dd_p95": dds[int(0.05 * B)], "dd_p99": dds[int(0.01 * B)]}


def deflated_sharpe(sr, n_obs, n_trials, sk, ku, var_trials):
    """Bailey & Lopez de Prado (2014). sr per observation. Returns P(true SR > 0) after selection bias."""
    if n_obs < 3 or not ok(sr):
        return NAN
    emc = 0.5772156649

    def inv(p):
        lo, hi = -10.0, 10.0
        for _ in range(100):
            mid = (lo + hi) / 2
            if norm_cdf(mid) < p:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2
    sd = math.sqrt(max(var_trials, 1e-12))
    sr0 = sd * ((1 - emc) * inv(1 - 1.0 / n_trials) + emc * inv(1 - 1.0 / (n_trials * math.e))) if n_trials > 1 else 0.0
    den = math.sqrt(max(1 - sk * sr + (ku - 1) / 4.0 * sr * sr, 1e-12))
    return norm_cdf((sr - sr0) * math.sqrt(n_obs - 1) / den)


def bh_adjust(pvals):
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        prev = min(prev, pvals[i] * m / (rank + 1))
        adj[i] = prev
    return adj


# ----------------------------------------------------------- time series --
def autocorr(x, lag):
    n = len(x)
    if n <= lag + 2:
        return NAN
    m = sum(x) / n
    den = sum((v - m) ** 2 for v in x)
    return sum((x[i] - m) * (x[i - lag] - m) for i in range(lag, n)) / den if den > 0 else NAN


def hurst(prices, lags=(2, 4, 8, 16, 32, 64)):
    """H from the scaling of std(log P[t+tau] - log P[t]) ~ tau^H (0.5 random walk, <0.5 mean-reverting)."""
    lp = [math.log(p) for p in prices if p > 0]
    pts = []
    for tau in lags:
        if len(lp) <= tau + 10:
            continue
        d = [lp[i + tau] - lp[i] for i in range(len(lp) - tau)]
        m, s = mean_sd(d)
        if s > 0:
            pts.append((math.log(tau), math.log(s)))
    if len(pts) < 3:
        return NAN
    mx = sum(a for a, _ in pts) / len(pts)
    my = sum(b for _, b in pts) / len(pts)
    return sum((a - mx) * (b - my) for a, b in pts) / sum((a - mx) ** 2 for a, _ in pts)


def variance_ratio(r, q=5):
    n = len(r)
    if n < 10 * q:
        return NAN, NAN
    m, s = mean_sd(r)
    v1 = s * s
    rq = [sum(r[i:i + q]) for i in range(n - q + 1)]
    _, sq = mean_sd(rq)
    vr = sq * sq / q / v1 if v1 > 0 else NAN
    z = (vr - 1) / math.sqrt(2 * (2 * q - 1) * (q - 1) / (3 * q * n))
    return vr, z


def cusum_change_point(x, B=500, seed=3):
    """Single change point in the mean: argmax |S_k| of the centred cumulative sum; permutation p-value."""
    n = len(x)
    if n < 20:
        return None
    def stat(v):
        m = sum(v) / len(v)
        s, best, bk = 0.0, 0.0, 0
        for k, a in enumerate(v):
            s += a - m
            if abs(s) > best:
                best, bk = abs(s), k
        return best, bk
    st, k = stat(x)
    rnd = random.Random(seed)
    v = list(x)
    cnt = 0
    for _ in range(B):
        rnd.shuffle(v)
        if stat(v)[0] >= st:
            cnt += 1
    return {"index": k, "stat": st, "p": (cnt + 1) / (B + 1),
            "mean_before": sum(x[:k + 1]) / (k + 1), "mean_after": sum(x[k + 1:]) / max(n - k - 1, 1)}


# ----------------------------------------------------- logistic regression --
def logit_fit(X, y, l2=1.0, iters=400, lr=0.1):
    k = len(X[0])
    w = [0.0] * (k + 1)
    n = len(X)
    for _ in range(iters):
        g = [0.0] * (k + 1)
        for xi, yi in zip(X, y):
            z = w[0] + sum(a * b for a, b in zip(w[1:], xi))
            p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            e = p - yi
            g[0] += e
            for j in range(k):
                g[j + 1] += e * xi[j]
        for j in range(k + 1):
            w[j] -= lr * (g[j] / n + (l2 * w[j] / n if j else 0))
    return w


def logit_pred(w, xi):
    z = w[0] + sum(a * b for a, b in zip(w[1:], xi))
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return NAN
    c = sum(1.0 if a > b else (0.5 if a == b else 0.0) for a in pos for b in neg)
    return c / (len(pos) * len(neg))



# ============================================================ ORDER FLOW ==
# Concepts adapted (re-implemented, no code copied) from github.com/MrChartist/order-flow
# (PolyForm Noncommercial 1.0.0; Required Notice: Copyright (c) 2026 Mr. Chartist, https://mrchartist.com):
#   * futures build-up = price change vs open-interest change of the future over a window
#   * open interest summed over ALL listed months so a roll between months cancels out
#   * the "zone": the 15-minute stretch that added the most OI while price moved with the side
#   * side = futures move beyond a floor %, sector lean by majority, explainable pass/fail checks
# Added here (not in that repository): estimated volume delta / CVD, relative volume, order-book imbalance,
# NIFTY 50 breadth, historical validation and paper-ledger integration.
#
# DATA HONESTY: Dhan provides NO trade-side (aggressor) flag. Every delta / CVD figure below is ESTIMATED:
#   * from bars: Bulk Volume Classification (Easley, Lopez de Prado & O'Hara 2012): buy share = Phi(dP / sigma_dP)
#   * from live ticks (VPS WebSocket): quote rule vs the previous best bid/ask, tick rule as fallback
# Book imbalance uses RESTING orders (5-level depth, total buy/sell quantity) - intentions, not executions.

NIFTY50_AS_OF = "NSE NIFTY 50 constituents, September-2025 rebalance - re-check after every March/September review"
NIFTY50 = {
    "AXISBANK": "Financials", "HDFCBANK": "Financials", "ICICIBANK": "Financials", "KOTAKBANK": "Financials", "SBIN": "Financials",
    "BAJFINANCE": "Financials", "BAJAJFINSV": "Financials", "HDFCLIFE": "Financials", "SBILIFE": "Financials",
    "SHRIRAMFIN": "Financials", "JIOFIN": "Financials",
    "HCLTECH": "IT", "INFY": "IT", "TCS": "IT", "TECHM": "IT", "WIPRO": "IT",
    "RELIANCE": "Energy", "ONGC": "Energy", "NTPC": "Energy", "POWERGRID": "Energy", "COALINDIA": "Energy",
    "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "M&M": "Auto", "MARUTI": "Auto", "TMPV": "Auto",
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG", "TATACONSUM": "FMCG",
    "CIPLA": "Healthcare", "DRREDDY": "Healthcare", "SUNPHARMA": "Healthcare", "APOLLOHOSP": "Healthcare", "MAXHEALTH": "Healthcare",
    "HINDALCO": "Metals", "JSWSTEEL": "Metals", "TATASTEEL": "Metals", "ADANIENT": "Metals",
    "ASIANPAINT": "Consumer", "TITAN": "Consumer", "TRENT": "Consumer", "ETERNAL": "Consumer",
    "LT": "Industrials", "BEL": "Industrials", "ULTRACEMCO": "Industrials", "GRASIM": "Industrials", "ADANIPORTS": "Industrials",
    "BHARTIARTL": "Telecom & Services", "INDIGO": "Telecom & Services",
}
OF_FLOOR_PCT = {"15m": 0.15, "60m": 0.30, "day": 0.50}
OF_ZONE_BARS = 3          # 15 minutes of 5m bars
OF_MIN_SCORE = 0.6        # share of counted checks that must hold for an order-flow side


def buildup(dp, doi):
    if not ok(dp) or not ok(doi) or abs(dp) <= 1e-9 or doi == 0:
        return "NEUTRAL"
    if dp > 0:
        return "LONG_BUILD" if doi > 0 else "SHORT_COVER"
    return "SHORT_BUILD" if doi > 0 else "LONG_UNWIND"


BUILDUP_SIDE = {"LONG_BUILD": 1, "SHORT_COVER": 1, "SHORT_BUILD": -1, "LONG_UNWIND": -1, "NEUTRAL": 0}


def combine_oi(near, others):
    """All-months open interest on the near month's bars: each other month contributes its last known OI
    at or before the bar (OI holds until it changes)."""
    out = []
    cur = [-1] * len(others)
    for b in near:
        tot = b.get("oi") or 0.0
        for k, o in enumerate(others):
            while cur[k] + 1 < len(o) and o[cur[k] + 1]["ts"] <= b["ts"]:
                cur[k] += 1
            if cur[k] >= 0:
                tot += o[cur[k]].get("oi") or 0.0
        out.append(tot)
    return out


def bvc_delta(bars, lookback=50):
    """ESTIMATED signed volume per bar by Bulk Volume Classification; sigma = stdev of the previous `lookback`
    bar-to-bar close changes (causal). Returns (delta list, buy-share list)."""
    deltas, shares = [], []
    ch = []
    for i, b in enumerate(bars):
        dp = b["close"] - bars[i - 1]["close"] if i else b["close"] - b["open"]
        w = ch[-lookback:]
        if len(w) >= 10:
            m = sum(w) / len(w)
            sd = math.sqrt(sum((v - m) ** 2 for v in w) / (len(w) - 1))
        else:
            sd = 0.0
        share = norm_cdf(dp / sd) if sd > 0 else 0.5
        v = b.get("volume") or 0.0
        deltas.append(v * (2 * share - 1))
        shares.append(share)
        ch.append(dp)
    return deltas, shares


def session_cvd(bars, deltas):
    out, s, day = [], 0.0, None
    for b, d in zip(bars, deltas):
        dd = ist(b["ts"]).date()
        if dd != day:
            s, day = 0.0, dd
        s += d
        out.append(s)
    return out


def relative_volume(bars, sessions=10):
    """Volume / mean volume of the same time-of-day bar over the previous `sessions` sessions (causal)."""
    hist = {}
    out = []
    for b in bars:
        t = ist(b["ts"])
        key = (t.hour, t.minute)
        h = hist.setdefault(key, [])
        base = sum(h[-sessions:]) / len(h[-sessions:]) if len(h) >= 3 else NAN
        v = b.get("volume") or 0.0
        out.append(v / base if ok(base) and base > 0 else NAN)
        h.append(v)
    return out


def of_prepare(bars, oi_total=None):
    """Per-bar order-flow series for a futures 5m series (adjusted prices, real volume and OI)."""
    d, _ = bvc_delta(bars)
    oi = oi_total if oi_total is not None else [b.get("oi") for b in bars]
    day_open_idx, prev_close, prev_oi = {}, {}, {}
    last_day, last_c, last_oi = None, None, None
    for i, b in enumerate(bars):
        dd = ist(b["ts"]).date()
        if dd != last_day:
            day_open_idx[dd] = i
            prev_close[dd], prev_oi[dd] = last_c, last_oi
            last_day = dd
        last_c, last_oi = b["close"], oi[i]
    return {"delta": d, "cvd": session_cvd(bars, d), "relvol": relative_volume(bars), "oi": oi, "vwap": vwap(bars),
            "prev_close": prev_close, "prev_oi": prev_oi, "has_volume": any((b.get("volume") or 0) > 0 for b in bars),
            "has_oi": any(ok(v) and v > 0 for v in oi if v is not None)}


def of_window(bars, ctx, i, nbars):
    """Futures reading over the last `nbars` bars (or the day vs previous close when nbars is None)."""
    if nbars is None:
        dd = ist(bars[i]["ts"]).date()
        p0, o0 = ctx["prev_close"].get(dd), ctx["prev_oi"].get(dd)
    else:
        j = i - nbars
        if j < 0 or ist(bars[j]["ts"]).date() != ist(bars[i]["ts"]).date():
            return None
        p0, o0 = bars[j]["close"], ctx["oi"][j]
    p1, o1 = bars[i]["close"], ctx["oi"][i]
    if not ok(p0) or p0 <= 0:
        return None
    doi = (o1 - o0) if ok(o1) and ok(o0) else NAN
    return {"dp": p1 - p0, "dp_pct": (p1 / p0 - 1) * 100, "doi": doi,
            "doi_pct": (doi / o0 * 100) if ok(doi) and o0 else NAN, "buildup": buildup(p1 - p0, doi)}


def of_zone(bars, ctx, i, side):
    """Repository idea: the 15-minute stretch today that added the most OI while price moved with the side."""
    dd = ist(bars[i]["ts"]).date()
    best = None
    for e in range(i, OF_ZONE_BARS - 1, -1):
        s = e - OF_ZONE_BARS + 1
        if ist(bars[s]["ts"]).date() != dd:
            break
        o0, o1 = ctx["oi"][s], ctx["oi"][e]
        if not (ok(o0) and ok(o1)) or o1 - o0 <= 0:
            continue
        if (bars[e]["close"] - bars[s]["close"]) * side <= 0:
            continue
        if best is None or o1 - o0 > best["oi_added"]:
            best = {"low": min(bars[k]["low"] for k in range(s, e + 1)), "high": max(bars[k]["high"] for k in range(s, e + 1)),
                    "from": bars[s]["ts"], "oi_added": o1 - o0}
    return best


def breadth_from_quotes(quotes):
    """quotes: {symbol: {"ltp":..., "prev_close":...}} for NIFTY 50 constituents. Equal-weight (no index weights)."""
    rows = []
    for sym, q in quotes.items():
        lp, pc = q.get("ltp"), q.get("prev_close")
        if ok(lp) and ok(pc) and pc > 0:
            rows.append((sym, (lp / pc - 1) * 100, NIFTY50.get(sym, "Other")))
    if not rows:
        return None
    adv = sum(1 for _, c, _ in rows if c > 0.05)
    dec = sum(1 for _, c, _ in rows if c < -0.05)
    sectors = {}
    for sym, c, sec in rows:
        sectors.setdefault(sec, []).append((sym, c))
    sec_out = {}
    for sec, v in sectors.items():
        cs = sorted(c for _, c in v)
        med = cs[len(cs) // 2] if len(cs) % 2 else (cs[len(cs) // 2 - 1] + cs[len(cs) // 2]) / 2
        up = sum(1 for c in cs if c > 0.05)
        dn = sum(1 for c in cs if c < -0.05)
        lean = "mixed"
        if up > dn and up * 2 >= len(cs):
            lean = "bullish"
        elif dn > up and dn * 2 >= len(cs):
            lean = "bearish"
        sec_out[sec] = {"n": len(cs), "median_pct": med, "up": up, "down": dn, "lean": lean}
    rows.sort(key=lambda r: r[1])
    return {"n": len(rows), "advances": adv, "declines": dec, "unchanged": len(rows) - adv - dec,
            "ad_ratio": adv / dec if dec else float("inf") if adv else NAN,
            "mean_pct": sum(c for _, c, _ in rows) / len(rows), "sectors": sec_out,
            "leaders": [(s, round(c, 2)) for s, c, _ in rows[-3:][::-1]], "laggards": [(s, round(c, 2)) for s, c, _ in rows[:3]]}


def book_from_quote(q):
    """Resting-order state of the NIFTY future from a /marketfeed/quote record."""
    if not q:
        return None
    dep = q.get("depth") or {}
    bids = [(float(x.get("price") or 0), float(x.get("quantity") or 0)) for x in dep.get("buy", [])]
    asks = [(float(x.get("price") or 0), float(x.get("quantity") or 0)) for x in dep.get("sell", [])]
    tb, ts_ = float(q.get("buy_quantity") or 0), float(q.get("sell_quantity") or 0)
    wb = sum(qq * 0.5 ** k for k, (_, qq) in enumerate(bids))
    wa = sum(qq * 0.5 ** k for k, (_, qq) in enumerate(asks))
    bb = bids[0][0] if bids else NAN
    ba = asks[0][0] if asks else NAN
    return {"total_imb": (tb - ts_) / (tb + ts_) if tb + ts_ > 0 else NAN,
            "depth_imb": (wb - wa) / (wb + wa) if wb + wa > 0 else NAN,
            "spread": ba - bb if ok(ba) and ok(bb) and ba >= bb else NAN, "bid": bb, "ask": ba,
            "total_buy_qty": tb, "total_sell_qty": ts_}


def orderflow_state(bars, ctx, i, breadth=None, book=None):
    """Explainable order-flow reading at CLOSED bar i (uses data <= i only).
    side: bullish / bearish from the 15-minute futures move beyond its floor, else rangebound.
    Each check is pass / fail / None (data not available -> not counted)."""
    w15, w60, wday = of_window(bars, ctx, i, 3), of_window(bars, ctx, i, 12), of_window(bars, ctx, i, None)
    if w15 is None:
        return {"side": "unknown", "score": NAN, "checks": [], "signal": "NONE"}
    side = 1 if w15["dp_pct"] >= OF_FLOOR_PCT["15m"] else (-1 if w15["dp_pct"] <= -OF_FLOOR_PCT["15m"] else 0)
    out = {"side": {1: "bullish", -1: "bearish", 0: "rangebound"}[side], "w15": w15, "w60": w60, "day": wday,
           "cvd": ctx["cvd"][i], "relvol": ctx["relvol"][i], "vwap": ctx["vwap"][i], "checks": [], "signal": "NONE",
           "delta_label": "ESTIMATED (bulk volume classification; Dhan has no trade-side flag)"}
    if side == 0:
        out["score"] = NAN
        return out
    ck = out["checks"]
    opened = "LONG_BUILD" if side > 0 else "SHORT_BUILD"
    bu = w15["buildup"]
    ck.append(("fresh positions (OI up with the move)", (bu == opened) if ok(w15["doi"]) else None,
               "15m: price %+.2f%%, OI %s -> %s" % (w15["dp_pct"], "%+.2f%%" % w15["doi_pct"] if ok(w15["doi_pct"]) else "n/a", bu)))
    ck.append(("last hour agrees", (w60["dp"] * side > 0) if w60 else None,
               "60m price %+.2f%% (%s)" % (w60["dp_pct"], w60["buildup"]) if w60 else "60m not measurable yet"))
    ck.append(("day agrees", (wday["dp"] * side > 0) if wday else None,
               "since prev close %+.2f%% (%s)" % (wday["dp_pct"], wday["buildup"]) if wday else "no previous close"))
    vw = ctx["vwap"][i]
    ck.append(("price on the right side of VWAP", ((bars[i]["close"] - vw) * side > 0) if ok(vw) else None,
               "close %.1f vs VWAP %.1f" % (bars[i]["close"], vw) if ok(vw) else "no volume -> no VWAP"))
    j = i - 6
    if ctx["has_volume"] and j >= 0 and ist(bars[j]["ts"]).date() == ist(bars[i]["ts"]).date():
        dc = ctx["cvd"][i] - ctx["cvd"][j]
        ck.append(("estimated CVD rising with the move (30m)", dc * side > 0, "est. CVD change 30m %+.0f (ESTIMATED)" % dc))
    else:
        ck.append(("estimated CVD rising with the move (30m)", None, "not enough volume history"))
    rv = ctx["relvol"][i]
    ck.append(("volume above normal for this time (>=1.2x)", (rv >= 1.2) if ok(rv) else None,
               "relative volume %.2fx" % rv if ok(rv) else "needs 3 prior sessions"))
    if breadth:
        adr = breadth["ad_ratio"]
        ck.append(("NIFTY 50 breadth agrees", (adr >= 1.5) if side > 0 else (ok(adr) and adr <= 1 / 1.5),
                   "adv %d / dec %d (A/D %.2f), equal-weight %+.2f%%" % (breadth["advances"], breadth["declines"], adr, breadth["mean_pct"])))
        fin = breadth["sectors"].get("Financials")
        if fin:
            ck.append(("Financials (heaviest NIFTY sector) lean agrees", fin["lean"] == out["side"],
                       "Financials %s, median %+.2f%% (%d up / %d down)" % (fin["lean"], fin["median_pct"], fin["up"], fin["down"])))
    else:
        ck.append(("NIFTY 50 breadth agrees", None, "breadth not available (historical bars carry none)"))
    if book and ok(book.get("total_imb")):
        ck.append(("resting order book leans the same way", book["total_imb"] * side > 0.10,
                   "total buy/sell qty imbalance %+.2f, 5-level %+.2f (resting orders, not trades)" % (
                       book["total_imb"], book.get("depth_imb", NAN))))
    else:
        ck.append(("resting order book leans the same way", None, "no live depth (historical bars carry none)"))
    counted = [c for c in ck if c[1] is not None]
    out["score"] = sum(1 for c in counted if c[1]) / len(counted) if counted else NAN
    out["counted"] = len(counted)
    zone = of_zone(bars, ctx, i, side) if ctx["has_oi"] else None
    out["zone"] = zone
    if ok(out["score"]) and out["score"] >= OF_MIN_SCORE and ck[0][1] is not False:
        out["signal"] = "OF_BULLISH" if side > 0 else "OF_BEARISH"
    return out


def of_text(st):
    if not st or not st.get("checks"):
        return "ORDER FLOW: %s" % (st or {}).get("side", "n/a")
    lines = ["ORDER FLOW: %s | side %s | %d/%d checks (score %.2f)" % (
        st["signal"], st["side"], sum(1 for c in st["checks"] if c[1]), st.get("counted", 0), st["score"] if ok(st["score"]) else NAN)]
    for name, p, ev in st["checks"]:
        lines.append("  [%s] %s - %s" % ("PASS" if p else ("FAIL" if p is False else " n/a"), name, ev))
    if st.get("zone"):
        z = st["zone"]
        lines.append("  zone %.1f-%.1f from %s (+%.0f OI)" % (z["low"], z["high"], ist(z["from"]).strftime("%H:%M"), z["oi_added"]))
    lines.append("  " + st["delta_label"])
    return "\n".join(lines)


# ============================================================ NIFTY AUDIT ==
VARIANTS = {
    "BASELINE": None,
    "F3 09:30-13:00": {"entry_end": dt.time(13, 0)},
    "F4 break>=2bps": {"min_break_bps": 2.0},
    "F3+F4": {"entry_end": dt.time(13, 0), "min_break_bps": 2.0},
}
REPORTED = {  # numbers reported in the earlier runs (index proxy, legacy engine, 2025 STT) -> must be reproduced
    "BASELINE": 25595, "F3 09:30-13:00": 54773, "F4 break>=2bps": 43748, "F3+F4": 41518,
}


def simulate_exit(A, i, d, sd, E, lot, exch, detail=False):
    """Exit of a position entered at the open of bar i (same rules as the engine, stop/target 2x/4x ATR)."""
    slip = E["slip"]
    entry = A[i]["open"] + d * slip
    stop, tgt = entry - d * sd, entry + d * 2 * sd
    day = ist(A[i]["ts"]).date()
    for j in range(i, len(A)):
        b = A[j]
        t = ist(b["ts"])
        if t.date() != day:
            exit_px = A[j - 1]["close"] - d * slip
            break
        hit_sl = (b["low"] <= stop) if d > 0 else (b["high"] >= stop)
        hit_tg = (b["high"] >= tgt + E["tg_through"]) if d > 0 else (b["low"] <= tgt - E["tg_through"])
        if hit_sl and (E["both"] == "sl" or not hit_tg):
            gapped = E["gap"] and j != i and ((b["open"] <= stop) if d > 0 else (b["open"] >= stop))
            exit_px = (b["open"] if gapped else stop) - d * slip
            break
        if hit_tg:
            exit_px = tgt
            break
        if (t + dt.timedelta(minutes=5)).time() >= EOD_EXIT:
            exit_px = b["close"] - d * slip
            break
    else:
        exit_px = A[-1]["close"]
    raw_e = entry - (A[i].get("adj", 0) or 0)
    raw_x = exit_px - (A[i].get("adj", 0) or 0)
    buy, sell = (raw_e, raw_x) if d > 0 else (raw_x, raw_e)
    ch = charges("fut", exch, buy * lot, sell * lot, None, day)
    pts = d * (exit_px - entry)
    if detail:
        return {"pts": pts, "gross": pts * lot, "charges": ch, "net": pts * lot - ch, "date": day.isoformat(), "dir": d,
                "entry_ts": A[i]["ts"]}
    return pts * lot - ch


def permutation_random_entries(bars, trades, lot, exch, E, entry_end, P=PERMUTATIONS, seed=21):
    """H0: the strategy's entries are no better than random entries with the SAME count, the same entry window,
    the same long/short mix, the same 2xATR stop / 4xATR target / 15:20 exit and the same costs."""
    if len(trades) < 10:
        return {}
    A = adjusted(bars)
    x = compute(A)
    cand = [i for i in range(1, len(A) - 1) if ok(x["atr"][i]) and ENTRY_START <= ist(A[i]["ts"]).time() < entry_end
            and ist(A[i + 1]["ts"]).date() == ist(A[i]["ts"]).date()]
    dirs = [t["dir"] for t in trades]
    actual = sum(t["net"] for t in trades)
    rnd = random.Random(seed)
    dist = []
    for _ in range(P):
        tot = 0.0
        for d in dirs:
            i = rnd.choice(cand)
            tot += simulate_exit(A, i + 1, d, PARAMS["stop_atr"] * x["atr"][i], E, lot, exch)
        dist.append(tot)
    dist.sort()
    return {"actual": actual, "random_median": dist[P // 2], "random_p95": dist[int(0.95 * P)],
            "p_value": (sum(1 for v in dist if v >= actual) + 1) / (P + 1)}


def filter_attribution(base, filt, label, B=2000, seed=9):
    """Trades removed by a filter: if they have negative expectancy the filter adds value. Random-removal test:
    remove the same number of random baseline trades; p = P(random keeps >= the filter's remaining net)."""
    ids_f = {t["id"] for t in filt}
    ids_b = {t["id"] for t in base}
    removed = [t for t in base if t["id"] not in ids_f]
    added = [t for t in filt if t["id"] not in ids_b]
    kept = [t for t in base if t["id"] in ids_f]
    rnd = random.Random(seed)
    k = len(removed)
    nets = [t["net"] for t in base]
    target = sum(t["net"] for t in kept)
    cnt = 0
    for _ in range(B):
        idx = set(rnd.sample(range(len(nets)), k)) if k <= len(nets) else set()
        if sum(v for j, v in enumerate(nets) if j not in idx) >= target:
            cnt += 1
    rm = summarize(removed) if removed else {"n": 0}
    log("  %-14s removed %3d trades: %s" % (label, k, fmt_sum(rm) if removed else "-"))
    log("  %-14s added %3d trades (path effect) net Rs%.0f | random-removal p = %.3f (low = filter better than chance)" % (
        "", len(added), sum(t["net"] for t in added), (cnt + 1) / (B + 1)))
    return {"removed_net": sum(t["net"] for t in removed), "p_random_removal": (cnt + 1) / (B + 1)}


def regime_table(tr, label, key_fn, title):
    groups = {}
    for t in tr:
        groups.setdefault(key_fn(t), []).append(t)
    log("  %s - %s" % (label, title))
    for k in sorted(groups):
        s = summarize(groups[k])
        log("    %-16s n=%3d win=%4.1f%% net=Rs%+8.0f exp=Rs%+6.0f PF=%.2f" % (
            k, s["n"], 100 * s["win"][0], s["net"], s["expectancy"], s["pf"]))


def ml_research(tr, label):
    """Chronological walk-forward logistic regression on entry-time features -> P(win). Research only."""
    feats = ["f_adx", "f_er", "f_rsi", "f_bw", "f_z", "f_brk", "f_hour", "f_atr_bps", "f_vwap_bps", "f_di", "f_dir"]
    rows = sorted(tr, key=lambda t: t["entry_ts"])
    n = len(rows)
    if n < 80:
        log("  ML %s: %d trades < 80 -> not enough data for a leakage-free walk-forward; skipped" % (label, n))
        return
    X = [[float(t.get(f) or 0.0) for f in feats] for t in rows]
    y = [1 if t["net"] > 0 else 0 for t in rows]
    folds = 5
    size = n // folds
    P, Yt, Nt = [], [], []
    for k in range(2, folds):
        tr_end = k * size
        te = range(tr_end, n if k == folds - 1 else tr_end + size)
        Xtr, ytr = X[:tr_end], y[:tr_end]
        mu = [sum(c) / len(c) for c in zip(*Xtr)]
        sd = [max(math.sqrt(sum((v - m) ** 2 for v in c) / len(c)), 1e-9) for c, m in zip(zip(*Xtr), mu)]
        Z = [[(v - m) / s for v, m, s in zip(r, mu, sd)] for r in Xtr]
        w = logit_fit(Z, ytr, l2=5.0)
        base = sum(ytr) / len(ytr)
        for j in te:
            p = logit_pred(w, [(v - m) / s for v, m, s in zip(X[j], mu, sd)])
            P.append(p)
            Yt.append(y[j])
            Nt.append((rows[j]["net"], p > base))
    if not P:
        return
    base_all = sum(Yt) / len(Yt)
    brier = sum((p - a) ** 2 for p, a in zip(P, Yt)) / len(P)
    brier0 = sum((base_all - a) ** 2 for a in Yt) / len(Yt)
    sel = [v for v, s in Nt if s]
    tp = sum(1 for p, a in zip(P, Yt) if p > 0.5 and a)
    fp = sum(1 for p, a in zip(P, Yt) if p > 0.5 and not a)
    fn = sum(1 for p, a in zip(P, Yt) if p <= 0.5 and a)
    log("  ML %s logistic walk-forward (train on past folds only): OOS n=%d | base win %.1f%% | AUC %.3f | Brier %.4f vs "
        "naive %.4f | precision %.2f recall %.2f | expectancy all OOS Rs%.0f vs model-selected Rs%.0f (n=%d)" % (
            label, len(P), 100 * base_all, auc(P, Yt), brier, brier0, tp / (tp + fp) if tp + fp else NAN,
            tp / (tp + fn) if tp + fn else NAN, sum(v for v, _ in Nt) / len(Nt), (sum(sel) / len(sel)) if sel else NAN,
            len(sel)))


def of_audit(fut_adj, per, lot, f34_fut):
    """Historical validation of the order-flow layer on NIFTY futures 5m bars (the only series with volume + OI).
    Breadth and order-book checks cannot be back-tested (Dhan history has no constituent snapshots or depth)."""
    log("-" * 100)
    log("PHASE OF  ORDER-FLOW VALIDATION (futures 5m, all-months OI, ESTIMATED delta; breadth/book not back-testable)")
    A = adjusted(fut_adj)
    oi_tot = oi_all_months(fut_adj, per, 5)
    ctx = of_prepare(A, oi_tot)
    n_oi = sum(1 for v in oi_tot if ok(v))
    log("  coverage: bars %d | bars with all-months OI %d (%.0f%%) | bars with volume %d" % (
        len(A), n_oi, 100.0 * n_oi / max(len(A), 1), sum(1 for b in A if (b.get("volume") or 0) > 0)))
    x = compute(A)
    sigs, last = [], "NONE"
    for i in range(30, len(A) - 2):
        t = ist(A[i]["ts"])
        st = orderflow_state(A, ctx, i)
        sgn = st["signal"]
        if sgn != "NONE" and sgn != last and ENTRY_START <= t.time() < ENTRY_END and ist(A[i + 1]["ts"]).date() == t.date() \
                and ok(x["atr"][i]):
            d = 1 if sgn == "OF_BULLISH" else -1
            tr = simulate_exit(A, i + 1, d, PARAMS["stop_atr"] * x["atr"][i], EXEC["realistic"], lot, "NSE", detail=True)
            fw = {}
            for h in (3, 6):
                if i + h < len(A) and ist(A[i + h]["ts"]).date() == t.date():
                    fw[h] = d * (A[i + h]["close"] / A[i]["close"] - 1) * 1e4
            tr.update({"signal": sgn, "fwd": fw, "score": st["score"]})
            sigs.append(tr)
        last = sgn
    log("  ORDER-FLOW SIGNALS traded alone (next-bar entry, 2xATR stop / 4xATR target / 15:20, realistic, 2026 costs):")
    for lab, S in (("ALL", sigs), ("OF_BULLISH", [q for q in sigs if q["signal"] == "OF_BULLISH"]),
                   ("OF_BEARISH", [q for q in sigs if q["signal"] == "OF_BEARISH"])):
        log("    %-11s %s" % (lab, fmt_sum(summarize(S)) if S else "n=0"))
    for h in (3, 6):
        v = [q["fwd"][h] for q in sigs if h in q["fwd"]]
        if v:
            k = sum(1 for a in v if a > 0)
            w = wilson(k, len(v))
            base = []
            for i in range(30, len(A) - h):
                if ist(A[i + h]["ts"]).date() == ist(A[i]["ts"]).date():
                    base.append(A[i + h]["close"] > A[i]["close"])
            log("    forward +%dm in signal direction: hit %.1f%% [%.0f-%.0f] (n=%d), mean %+.1f bps | unconditional up-share %.1f%%" % (
                h * 5, 100 * w[0], 100 * w[1], 100 * w[2], len(v), sum(v) / len(v), 100.0 * sum(base) / max(len(base), 1)))
    if len(sigs) >= 10:
        perm = permutation_random_entries(fut_adj, sigs, lot, "NSE", EXEC["realistic"], ENTRY_END)
        log("    permutation vs random entries: actual Rs%.0f | random median Rs%.0f | p = %.3f" % (
            perm["actual"], perm["random_median"], perm["p_value"]))
    log("  F3+F4 futures trades split by order-flow agreement at the signal bar:")
    for lab, S in (("OF agrees", [t for t in f34_fut if t.get("of_agree") is True]),
                   ("OF does not agree", [t for t in f34_fut if t.get("of_agree") is False])):
        log("    %-18s %s" % (lab, fmt_sum(summarize(S)) if S else "n=0"))
    v = backtest(fut_adj, lot, "NSE", PARAMS, dict(VARIANTS["F3+F4"], of_confirm=True), "realistic", of_ctx=ctx)["trades"]
    log("    F3+F4 + order-flow confirmation (F7, rule fixed before testing): %s" % (fmt_sum(summarize(v)) if v else "n=0"))
    log("  CAVEAT: one 2.5-month futures period only (the period F3/F4 were chosen on); no out-of-sample order-flow test "
        "is possible until more futures history is collected. Delta/CVD are ESTIMATES (no trade-side data from Dhan).")


def run_audit(months=AUDIT_MONTHS):
    log("=" * 100)
    log("NIFTY QUANT AUDIT  (in-sample = data used to choose rules/filters; OOS = data not used)")
    log("=" * 100)
    today = now_ist().date()
    start = today - dt.timedelta(days=int(months * 30.5))
    lot_info = nifty_futures()
    futs = lot_info
    lot = futs[0]["lot"] if futs else 65.0
    sid = INDICES["NIFTY"]["sid"]
    dly = daily(sid, int(months * 31) + 30)
    idx5 = intraday(sid, "IDX_I", "INDEX", start, today, 5)
    per, exp = {}, {}
    for f in futs:
        per[f["sid"]] = intraday(f["sid"], f["segment"], "FUTIDX", start, today, 5, oi=True)
        exp[f["sid"]] = f["expiry"]
    fut_adj, rolls = stitch_front_month(per, exp, adjust=True)
    fut_raw, _ = stitch_front_month(per, exp, adjust=False)
    ib = [dict(b, volume=0.0) for b in idx5]
    split = fut_raw[0]["ts"] if fut_raw else None
    log("PHASE 1  DATA: lot %s | contracts %s | futures from %s | index from %s | rolls %s" % (
        lot, [(f["display"], f["expiry"]) for f in futs], ist(fut_raw[0]["ts"]).date() if fut_raw else None,
        ist(idx5[0]["ts"]).date() if idx5 else None, [(r["date"], round(r["diff"], 1), r["method"]) for r in rolls]))
    dq_report(fut_raw, "NIFTY futures 5m (stitched)")
    dq_report(idx5, "NIFTY index 5m", expect_volume=False)
    for f in futs:
        dq_report(per[f["sid"]], "contract %s" % f["display"])
    if not fut_raw or not idx5:
        log("AUDIT ABORTED: missing data")
        return
    oos_days = len({ist(b["ts"]).date() for b in idx5 if b["ts"] < split})

    log("-" * 100)
    log("PHASE 3a REPRODUCTION of reported numbers (legacy engine: index-daily pivots, no gaps/slippage, 2025 STT)")
    for name, flt in VARIANTS.items():
        tr = backtest(ib, lot, "NSE", PARAMS, flt, "legacy", "index_daily", dly, 2025)["trades"]
        s = summarize(tr)
        rep = REPORTED.get(name)
        log("  %-16s INDEX %s | reported Rs%s -> %s" % (name, fmt_sum(s), rep,
                                                       "MATCH" if rep is not None and abs(s.get("net", 0) - rep) < 2 else "DIFFERENT"))
    trl = backtest(fut_raw, lot, "NSE", PARAMS, None, "legacy", "index_daily", dly, 2025)["trades"]
    log("  %-16s FUT   %s | reported Rs46912" % ("BASELINE", fmt_sum(summarize(trl))))

    log("-" * 100)
    log("PHASE 3b CORRECTIONS applied one at a time to F3+F4 (attributes the P&L impact of each fix)")
    f34 = VARIANTS["F3+F4"]
    steps = [
        ("legacy", dict(ex="legacy", levels="index_daily", charge_year=2025)),
        ("+2026 STT (0.05% fut)", dict(ex="legacy", levels="index_daily", charge_year=2026)),
        ("+pivots from same series", dict(ex="legacy", levels="series", charge_year=2026)),
        ("+realistic execution", dict(ex="realistic", levels="series", charge_year=2026)),
    ]
    for lab, kw in steps:
        si = summarize(backtest(ib, lot, "NSE", PARAMS, f34, kw["ex"], kw["levels"], dly, kw["charge_year"])["trades"])
        fr = fut_raw if lab != "+realistic execution" else fut_adj
        sf = summarize(backtest(fr, lot, "NSE", PARAMS, f34, kw["ex"], kw["levels"], dly, kw["charge_year"])["trades"])
        log("  %-26s INDEX net Rs%+8.0f PF %.2f n=%d | FUT net Rs%+8.0f PF %.2f n=%d" % (
            lab, si.get("net", 0), si.get("pf", 0), si.get("n", 0), sf.get("net", 0), sf.get("pf", 0), sf.get("n", 0)))
    sf_noadj = summarize(backtest(fut_raw, lot, "NSE", PARAMS, f34, "realistic")["trades"])
    log("  %-26s FUT net Rs%+8.0f (roll back-adjustment OFF, for comparison)" % ("", sf_noadj.get("net", 0)))

    log("-" * 100)
    log("PHASE 4/5 CORRECTED ENGINE, realistic execution, 2026 costs")
    res = {}
    for name, flt in VARIANTS.items():
        ti = backtest(ib, lot, "NSE", PARAMS, flt, "realistic")["trades"]
        tf = backtest(fut_adj, lot, "NSE", PARAMS, flt, "realistic")["trades"]
        res[name] = (ti, tf)
        log("  %-16s OOS   May-Aug INDEX %s" % (name, fmt_sum(summarize([t for t in ti if t["entry_ts"] < split], oos_days))))
        log("  %-16s IS    Aug-Nov INDEX %s" % ("", fmt_sum(summarize([t for t in ti if t["entry_ts"] >= split]))))
        log("  %-16s IS    Aug-Nov FUT   %s" % ("", fmt_sum(summarize(tf))))
    ti, tf = res["F3+F4"]
    oos = [t for t in ti if t["entry_ts"] < split]

    log("-" * 100)
    log("PHASE 4  SIGNIFICANCE of F3+F4 (corrected, realistic)")
    for lab, tr in (("INDEX 6m", ti), ("INDEX OOS", oos), ("FUT IS", tf)):
        if len(tr) < 5:
            continue
        net = [t["net"] for t in tr]
        bs = bootstrap(net)
        mc = mc_drawdown(net)
        s = summarize(tr)
        log("  %-10s bootstrap total 95%% CI Rs%.0f..%.0f  P(total<=0)=%.3f | MC maxDD median Rs%.0f p95 Rs%.0f p99 Rs%.0f" % (
            lab, bs["total_ci95"][0], bs["total_ci95"][1], bs["p_total_le_0"], mc["dd_median"], mc["dd_p95"], mc["dd_p99"]))
        srs = [summarize(res[v][0]).get("sharpe_trade", NAN) for v in VARIANTS]
        srs = [v for v in srs if ok(v)]
        m, sdv = mean_sd(srs) if len(srs) > 1 else (NAN, NAN)
        dsr = deflated_sharpe(s["sharpe_trade"], s["n"], VARIANTS_TESTED, s["skew"], s["kurt"],
                              (sdv ** 2) if ok(sdv) else 0.0)
        log("  %-10s per-trade Sharpe %.3f | Deflated Sharpe P(SR>0 | %d variants tried) = %.3f" % (
            lab, s["sharpe_trade"], VARIANTS_TESTED, dsr))
    perm = permutation_random_entries(ib, ti, lot, "NSE", EXEC["realistic"], dt.time(13, 0))
    if perm:
        log("  PERMUTATION (INDEX 6m, %d random-entry strategies, same count/window/direction mix/exits/costs): "
            "actual Rs%.0f | random median Rs%.0f | random 95th pct Rs%.0f | p = %.3f" % (
                PERMUTATIONS, perm["actual"], perm["random_median"], perm["random_p95"], perm["p_value"]))
    perm_o = permutation_random_entries([b for b in ib if b["ts"] < split], oos, lot, "NSE", EXEC["realistic"], dt.time(13, 0))
    if perm_o:
        log("  PERMUTATION (OOS May-Aug only): actual Rs%.0f | random median Rs%.0f | p = %.3f" % (
            perm_o["actual"], perm_o["random_median"], perm_o["p_value"]))
    pv = []
    for name in VARIANTS:
        s = summarize([t for t in res[name][0] if t["entry_ts"] < split])
        pv.append(s.get("p_t", 1.0) if ok(s.get("p_t", NAN)) else 1.0)
    log("  Benjamini-Hochberg adjusted OOS p-values: %s" % dict(zip(VARIANTS, [round(v, 3) for v in bh_adjust(pv)])))

    log("-" * 100)
    log("PHASE 5  FILTER ATTRIBUTION (does each filter add independent value?) - INDEX 6m, realistic")
    b, f3, f4, f34t = res["BASELINE"][0], res["F3 09:30-13:00"][0], res["F4 break>=2bps"][0], res["F3+F4"][0]
    filter_attribution(b, f3, "F3 vs base")
    filter_attribution(b, f4, "F4 vs base")
    filter_attribution(f3, f34t, "F4 given F3")
    filter_attribution(f4, f34t, "F3 given F4")
    filter_attribution([t for t in b if t["entry_ts"] < split], [t for t in f34t if t["entry_ts"] < split], "F3+F4 OOS")

    log("-" * 100)
    log("PHASE 6  EXECUTION SCENARIOS and SLIPPAGE SENSITIVITY (F3+F4)")
    for sc in ("optimistic", "legacy", "realistic", "stress"):
        log("  %-10s INDEX %s" % (sc, fmt_sum(summarize(backtest(ib, lot, "NSE", PARAMS, f34, sc)["trades"]))))
        log("  %-10s FUT   %s" % ("", fmt_sum(summarize(backtest(fut_adj, lot, "NSE", PARAMS, f34, sc)["trades"]))))
    for sl in (0.0, 0.5, 1.0, 2.0, 3.0, 5.0):
        e = dict(EXEC["realistic"], slip=sl)
        si = summarize(backtest(ib, lot, "NSE", PARAMS, f34, e)["trades"])
        sf = summarize(backtest(fut_adj, lot, "NSE", PARAMS, f34, e)["trades"])
        log("  slippage %.1f pt/fill: INDEX net Rs%+8.0f PF %.2f | FUT net Rs%+8.0f PF %.2f" % (
            sl, si.get("net", 0), si.get("pf", 0), sf.get("net", 0), sf.get("pf", 0)))
    s = summarize(ti)
    if s.get("n"):
        log("  cost per trade: Rs%.0f (= %.1f NIFTY points on 1 lot); breakeven slippage = point edge / 2 fills = %.1f pts" % (
            s["charges"] / s["n"], s["charges"] / s["n"] / lot, (s["gross"] / s["n"] / lot - s["charges"] / s["n"] / lot) / 2))

    try:
        f34_fut = backtest(fut_adj, lot, "NSE", PARAMS, f34, "realistic", of_ctx=of_prepare(adjusted(fut_adj), oi_all_months(fut_adj, per, 5)))["trades"]
        of_audit(fut_adj, per, lot, f34_fut)
    except Exception:
        log("order-flow audit failed:", traceback.format_exc()[-800:])
    log("-" * 100)
    log("PHASE 5b REGIME STABILITY (F3+F4, INDEX 6m, realistic)")
    dret = {}
    dl = sorted(dly, key=lambda d: d["ts"])
    for k in range(21, len(dl)):
        r = [math.log(dl[j]["close"] / dl[j - 1]["close"]) for j in range(k - 20, k)]
        dret[ist(dl[k]["ts"]).date().isoformat()] = mean_sd(r)[1] * math.sqrt(252)
    vols = sorted(v for v in dret.values() if ok(v))
    q1, q2 = (vols[len(vols) // 3], vols[2 * len(vols) // 3]) if len(vols) > 6 else (NAN, NAN)

    def vol_bucket(t):
        v = dret.get(t["date"])
        return "n/a" if not ok(v) else ("1 low RV" if v < q1 else ("2 mid RV" if v < q2 else "3 high RV"))
    regime_table(ti, "F3+F4", vol_bucket, "prior 20-day realized vol tercile")
    regime_table(ti, "F3+F4", lambda t: t["date"][:7], "month")
    regime_table(ti, "F3+F4", lambda t: ("LONG" if t["dir"] > 0 else "SHORT") + " " + t["setup"][:8], "direction x setup")
    regime_table(ti, "F3+F4", lambda t: ["Mon", "Tue (approx expiry)", "Wed", "Thu", "Fri"][dt.date.fromisoformat(t["date"]).weekday()],
                 "weekday (NIFTY weekly expiry is Tuesday; holiday shifts not modelled)")
    gaps = {}
    days = {}
    for bb in ib:
        days.setdefault(ist(bb["ts"]).date(), []).append(bb)
    ds = sorted(days)
    for k in range(1, len(ds)):
        gaps[ds[k].isoformat()] = abs(days[ds[k]][0]["open"] / days[ds[k - 1]][-1]["close"] - 1) * 100
    regime_table(ti, "F3+F4", lambda t: "gap>0.5%" if gaps.get(t["date"], 0) > 0.5 else "gap<=0.5%", "opening gap")
    regime_table(ti, "F3+F4", lambda t: "ADX<20" if t["f_adx"] < 20 else ("ADX20-30" if t["f_adx"] < 30 else "ADX>=30"),
                 "ADX at entry")
    regime_table(ti, "F3+F4", lambda t: t["why"], "exit type")

    log("-" * 100)
    log("PHASE 8  ML / regime models (research only, never traded)")
    ml_research(res["BASELINE"][0], "INDEX baseline")
    ml_research(ti, "INDEX F3+F4")
    log("  Kronos / XGBoost / RandomForest / HMM: not runnable inside Dhan Cloud (pure Python, 1 vCPU, no PyTorch/"
        "sklearn); evaluate on a VPS with the same chronological split before considering them.")

    log("-" * 100)
    log("PHASE 2b MARKET STATISTICS (NIFTY index)")
    cl = [bb["close"] for bb in idx5]
    r5 = [math.log(cl[k] / cl[k - 1]) for k in range(1, len(cl)) if ist(idx5[k]["ts"]).date() == ist(idx5[k - 1]["ts"]).date()]
    rd = [math.log(dl[k]["close"] / dl[k - 1]["close"]) for k in range(1, len(dl))]
    vr, z = variance_ratio(r5, 5)
    log("  5m returns: autocorr lag1..3 %s | variance ratio(5) %.3f z %.2f | Hurst(5m log price) %.3f" % (
        [round(autocorr(r5, k), 4) for k in (1, 2, 3)], vr, z, hurst(cl)))
    cp = cusum_change_point(rd)
    if cp:
        log("  daily returns CUSUM change point at %s (p=%.3f): mean %.3f%% -> %.3f%%" % (
            ist(dl[cp["index"] + 1]["ts"]).date(), cp["p"], 100 * cp["mean_before"], 100 * cp["mean_after"]))
    cpv = cusum_change_point([abs(v) for v in rd])
    if cpv:
        log("  daily |returns| (volatility) change point at %s (p=%.3f): %.3f%% -> %.3f%%" % (
            ist(dl[cpv["index"] + 1]["ts"]).date(), cpv["p"], 100 * cpv["mean_before"], 100 * cpv["mean_after"]))

    log("-" * 100)
    s_oos = summarize(oos, oos_days)
    bs = bootstrap([t["net"] for t in oos]) if len(oos) >= 5 else {}
    checks = [
        ("OOS net > 0 after realistic costs", s_oos.get("net", -1) > 0),
        ("OOS bootstrap P(total<=0) < 0.05", bs.get("p_total_le_0", 1) < 0.05),
        ("permutation p < 0.05 (6m)", perm.get("p_value", 1) < 0.05 if perm else False),
        ("OOS trades >= 100", s_oos.get("n", 0) >= 100),
        ("stress scenario net > 0", summarize(backtest(ib, lot, "NSE", PARAMS, f34, "stress")["trades"]).get("net", -1) > 0),
    ]
    for lab, okk in checks:
        log("  [%s] %s" % ("PASS" if okk else "FAIL", lab))
    log("VERDICT: %s" % ("EDGE SUPPORTED (still requires paper-trading confirmation)" if all(o for _, o in checks)
                         else "EDGE NOT ESTABLISHED - keep paper trading only; do not trade real money"))
    log("=" * 100)


# ========================================================== PAPER LEDGER ==
# Dhan Cloud has no persistent storage, so the ledger is a deterministic REPLAY of the frozen rules over Dhan's own
# historical data from LEDGER_START. Every restart rebuilds the identical ledger:
#   * positions survive restarts (an open trade is re-derived from the data)
#   * every trade has a stable unique id = sha1(symbol | entry time | setup | side) -> no duplicates
#   * lifecycle: SIGNAL -> PENDING_FILL -> OPEN -> CLOSED(SL / SL_GAP / TARGET / EOD)
#   * events are notified once per process; events older than the process start are not re-sent
#   * every execution line is logged with id, timestamps, prices, reason, charges and net P&L (audit trail)

def fetch_series(futs, frm, to, interval=1):
    per, exp = {}, {}
    for f in futs:
        per[f["sid"]] = intraday(f["sid"], f["segment"], "FUTIDX", frm, to, interval, oi=True)
        exp[f["sid"]] = f["expiry"]
    bars, rolls = stitch_front_month(per, exp, adjust=True)
    return bars, rolls, per


def oi_all_months(stitched, per, bar_minutes=5):
    """Open interest summed over every listed NIFTY futures month at each stitched bar (rolls cancel out)."""
    series = {sid: sorted(v, key=lambda b: b["ts"]) for sid, v in per.items()}
    ts_lists = {sid: [b["ts"] for b in v] for sid, v in series.items()}
    out = []
    for b in stitched:
        tot = 0.0
        for sid, v in series.items():
            k = bisect.bisect_right(ts_lists[sid], b["ts"] + bar_minutes * 60 - 1) - 1
            if k >= 0 and v[k].get("oi") is not None:
                tot += v[k]["oi"]
        out.append(tot if tot > 0 else NAN)
    return out


def ledger_build(bars5, lot, start_date, of_ctx=None):
    res = backtest(bars5, lot, "NSE", PARAMS, LIVE_FILTER, EXEC_LIVE, with_open=True, of_ctx=of_ctx)
    closed_tr = [t for t in res["trades"] if t["date"] >= start_date]
    events = []
    for t in closed_tr:
        events.append((t["entry_ts"], t["id"], "OPEN", "[PAPER %s] OPEN %s %s @ %.1f (stop dist %.1f) | %s | order flow %s" % (
            t["id"], "LONG" if t["dir"] > 0 else "SHORT", t["setup"], t["entry"], t["sd"], t["entry_reason"],
            {True: "AGREES", False: "does not agree", None: "n/a"}[t.get("of_agree")])))
        events.append((t["exit_ts"] + 300, t["id"], "CLOSED", "[PAPER %s] CLOSED %s @ %.1f (%s) | %+.1f pts | gross Rs%.0f | "
                       "charges Rs%.0f | net Rs%.0f" % (t["id"], "LONG" if t["dir"] > 0 else "SHORT", t["exit"], t["why"],
                                                       t["pts"], t["gross"], t["charges"], t["net"])))
    op = res.get("open")
    open_pos = None
    if op and ist(op["ts"]).date().isoformat() >= start_date:
        oid = trade_id("NIFTY", op["ts"], op["setup"], op["d"])
        raw = op["entry"] - op["adj"]
        state = "OPEN" if op["ts"] <= bars5[-1]["ts"] else "PENDING_FILL"
        open_pos = {"id": oid, "state": state, "dir": op["d"], "setup": op["setup"], "entry": raw,
                    "stop": op["stop"] - op["adj"], "target": op["target"] - op["adj"], "entry_ts": op["ts"]}
        msg = ("[PAPER %s] %s %s %s @ %.1f | SL %.1f | T %.1f" % (
            oid, state, "LONG" if op["d"] > 0 else "SHORT", op["setup"], raw, open_pos["stop"], open_pos["target"]))
        events.append((op["ts"], oid, state, msg))
    return closed_tr, open_pos, sorted(events)


def ledger_summary(closed_tr, day):
    def line(S):
        if not S:
            return "0 trades"
        w = sum(t["net"] > 0 for t in S)
        return "%d trades, %d wins (%.0f%%), net Rs%.0f, charges Rs%.0f" % (
            len(S), w, 100.0 * w / len(S), sum(t["net"] for t in S), sum(t["charges"] for t in S))
    td = [t for t in closed_tr if t["date"] == day]
    s = summarize(closed_tr) if closed_tr else {"n": 0}
    agree = [t for t in closed_tr if t.get("of_agree") is True]
    other = [t for t in closed_tr if t.get("of_agree") is False]
    return "[PAPER] %s today: %s | since %s: %s | maxDD Rs%.0f | order-flow agreed: %s | did not: %s" % (
        day, line(td), LEDGER_START, line(closed_tr), s.get("max_dd", 0.0), line(agree), line(other))


# ============================================================= SELF-TEST ==
def synthetic_bars(n=3000, seed=7, t0=None, vol=0.0012):
    rnd = random.Random(seed)
    t0 = t0 or dt.datetime(2026, 5, 4, 9, 15, tzinfo=IST).timestamp()
    px, bars, day, k = 24000.0, [], 0, 0
    for i in range(n):
        if k == SESSION_BARS_5M:
            day, k = day + 1, 0
            while ist(t0 + day * 86400).weekday() >= 5:
                day += 1
        o = bars[-1]["close"] if bars and k else px
        px *= math.exp(rnd.gauss(0, vol))
        bars.append({"ts": t0 + day * 86400 + k * 300, "open": o, "high": max(o, px) + abs(rnd.gauss(0, 8)),
                     "low": min(o, px) - abs(rnd.gauss(0, 8)), "close": px, "volume": float(rnd.randint(1000, 50000))})
        k += 1
    return bars


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
    check("True range", true_range([{"high": 10, "low": 8, "close": 9}, {"high": 12, "low": 11, "close": 11.5},
                                    {"high": 11, "low": 7, "close": 8}]) == [2, 3, 4.5])
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
    worst = 0.0
    for call in (True, False):
        for K in (23000, 24000, 25000):
            for T in (2 / 365, 30 / 365):
                s0 = 0.14
                g = greeks76(24000, K, T, 0.065, s0, call)
                h = 1.0
                fd_d = (black76(24000 + h, K, T, 0.065, s0, call) - black76(24000 - h, K, T, 0.065, s0, call)) / (2 * h)
                fd_g = (greeks76(24000 + h, K, T, 0.065, s0, call)["delta"] - greeks76(24000 - h, K, T, 0.065, s0, call)["delta"]) / (2 * h)
                fd_v = (black76(24000, K, T, 0.065, s0 + 1e-4, call) - black76(24000, K, T, 0.065, s0 - 1e-4, call)) / 2e-4 * 0.01
                fd_va = (greeks76(24000, K, T, 0.065, s0 + 1e-4, call)["delta"] - greeks76(24000, K, T, 0.065, s0 - 1e-4, call)["delta"]) / 2e-4 * 0.01
                e = 1e-5
                fd_ch = -(greeks76(24000, K, T + e, 0.065, s0, call)["delta"] - greeks76(24000, K, T - e, 0.065, s0, call)["delta"]) / (2 * e) / 365
                fd_th = -(black76(24000, K, T + e, 0.065, s0, call) - black76(24000, K, T - e, 0.065, s0, call)) / (2 * e) / 365
                for an, fd, sc in ((g["delta"], fd_d, 1), (g["gamma"], fd_g, 1e-3), (g["vega"], fd_v, 1),
                                   (g["vanna"], fd_va, 1e-2), (g["charm"], fd_ch, 1e-2), (g["theta"], fd_th, 1)):
                    worst = max(worst, abs(an - fd) / max(abs(fd), sc))
    check("Greeks incl. vanna & charm vs finite differences (rel err %.1e)" % worst, worst < 1e-3)
    iv = implied_vol(black76(24000, 24100, 0.02, 0.065, 0.14, True), 24000, 24100, 0.02, 0.065, True)
    check("Implied vol round trip", iv is not None and abs(iv - 0.14) < 1e-6)
    C, P = black76(24100, 24000, 0.05, 0.065, 0.15, True), black76(24100, 24000, 0.05, 0.065, 0.15, False)
    check("Put-call parity residual = 0", abs(parity_residual(C, P, 24100, 24000, 0.05, 0.065)) < 1e-8)
    ch26 = charges("fut", "NSE", 24000 * 65, 24100 * 65, 2026)
    stt = 0.0005 * 24100 * 65
    txn = 0.0000173 * (24000 + 24100) * 65
    sebi = 0.000001 * (24000 + 24100) * 65
    ref = 40 + stt + txn + sebi + 0.00002 * 24000 * 65 + 0.18 * (40 + txn + sebi)
    check("Futures charges 2026 (STT 0.05%%) = Rs%.2f" % ch26, abs(ch26 - ref) < 1e-6 and ch26 > charges("fut", "NSE", 24000 * 65, 24100 * 65, 2025))
    blocked = False
    try:
        post("/orders", {})
    except ValueError:
        blocked = True
    check("Order endpoints blocked", blocked)
    bars = synthetic_bars()
    r1 = backtest(bars, 65, "NSE", PARAMS, None, "realistic")["trades"]
    check("Engine: trades generated, net = pts*lot - charges", bool(r1) and all(abs(t["net"] - (t["pts"] * 65 - t["charges"])) < 1e-6 for t in r1))
    b2 = [dict(q) for q in bars]
    for q in b2[2000:]:
        for f in ("open", "high", "low", "close"):
            q[f] *= 1.05
    r2 = backtest(b2, 65, "NSE", PARAMS, None, "realistic")["trades"]
    cut = bars[1990]["ts"]
    check("No look-ahead: changing future bars leaves earlier trades identical",
          [t["id"] for t in r1 if t["exit_ts"] < cut] == [t["id"] for t in r2 if t["exit_ts"] < cut])
    check("Unique trade ids", len({t["id"] for t in r1}) == len(r1))
    ro = backtest(bars, 65, "NSE", PARAMS, None, "optimistic")["trades"]
    rs = backtest(bars, 65, "NSE", PARAMS, None, "stress")["trades"]
    check("Scenario ordering optimistic >= realistic >= stress",
          sum(t["net"] for t in ro) >= sum(t["net"] for t in r1) >= sum(t["net"] for t in rs))
    adj_bars = [dict(q, adj=123.0) for q in bars]
    ra = backtest(adj_bars, 65, "NSE", PARAMS, None, "realistic")["trades"]
    check("Back-adjustment: identical signals and points, charges on raw prices",
          [t["id"] for t in ra] == [t["id"] for t in r1] and all(abs(a1["pts"] - b1["pts"]) < 1e-9 for a1, b1 in zip(ra, r1)))
    day = dt.datetime(2026, 11, 9, 9, 15, tzinfo=IST).timestamp()
    m1 = [{"ts": day + k * 60, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1} for k in range(12)]
    cb = complete_5m(m1, day + 12 * 60)
    check("Incomplete 5m candle excluded (2 complete of 3 started)", len(cb) == 2)
    per = {1: [{"ts": day - 86400 + 22200, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
           2: [{"ts": day - 86400 + 22200, "open": 110, "high": 111, "low": 109, "close": 110, "volume": 1},
               {"ts": day, "open": 110, "high": 112, "low": 109, "close": 111, "volume": 1}]}
    st, rl = stitch_front_month(per, {1: (dt.date.fromtimestamp(day) - dt.timedelta(days=1)).isoformat(), 2: "2099-01-01"})
    check("Roll back-adjustment = +10 on pre-roll bars", len(rl) == 1 and abs(rl[0]["diff"] - 10) < 1e-9 and st[0]["adj"] == 10 and st[-1]["adj"] == 0)
    g1 = [{"ts": t0, "open": 100, "high": 100, "low": 100, "close": 100}]
    check("Hurst of random walk ~0.5", 0.4 < hurst([q["close"] for q in synthetic_bars(4000, 3, vol=0.001)]) < 0.6 and g1)
    check("t-test p-value (t=2.0, df=30) = 0.0546", abs(t_sf2(2.0, 30) - 0.0546) < 5e-4)
    check("OI build-up quadrants", [buildup(1, 1), buildup(1, -1), buildup(-1, 1), buildup(-1, -1), buildup(0, 5)]
          == ["LONG_BUILD", "SHORT_COVER", "SHORT_BUILD", "LONG_UNWIND", "NEUTRAL"])
    nr = [{"ts": t0 + k * 300, "oi": 100.0} for k in range(4)]
    check("All-months OI carries forward", combine_oi(nr, [[{"ts": t0 + 300, "oi": 50.0}]]) == [100.0, 150.0, 150.0, 150.0])
    fl = mk([100.0 + (k % 2) for k in range(40)]) + mk([200.0] * 1)
    dl, sh = bvc_delta(fl[:40])
    check("BVC estimated delta: |delta| <= volume, no lookahead", all(abs(x) <= 100.0 + 1e-9 for x in dl)
          and bvc_delta(fl[:20])[0] == dl[:20])
    br = breadth_from_quotes({"HDFCBANK": {"ltp": 101, "prev_close": 100}, "ICICIBANK": {"ltp": 99, "prev_close": 100},
                              "SBIN": {"ltp": 102, "prev_close": 100}, "TCS": {"ltp": 100, "prev_close": 100}})
    check("Breadth A/D + sector lean", br["advances"] == 2 and br["declines"] == 1 and br["unchanged"] == 1
          and br["sectors"]["Financials"]["lean"] == "bullish")
    bk = book_from_quote({"buy_quantity": 300, "sell_quantity": 100, "depth": {"buy": [{"price": 10, "quantity": 30}],
                                                                               "sell": [{"price": 10.5, "quantity": 10}]}})
    check("Book imbalance + spread", abs(bk["total_imb"] - 0.5) < 1e-12 and abs(bk["depth_imb"] - 0.5) < 1e-12 and bk["spread"] == 0.5)
    log("SELF-TEST: %d/%d passed" % (sum(res), len(res)))
    return all(res)


# ============================================================== LIVE LOOP ==
def in_session(t):
    return t.weekday() < 5 and dt.time(9, 15) <= t.time() <= dt.time(15, 30)


def cycle(state):
    now = time.time()
    today = now_ist().date()
    if state.get("day") != today:
        state["futs"] = nifty_futures()
        state["day"] = today
        log("instruments:", [(f["display"], f["sid"], f["lot"], f["expiry"]) for f in state["futs"]])
    futs = state["futs"]
    if not futs:
        log("no NIFTY futures found")
        return
    lot = futs[0]["lot"]
    frm = min(dt.date.fromisoformat(LEDGER_START), today) - dt.timedelta(days=8)
    b1, rolls, per = fetch_series(futs, frm, today, 1)
    bars5 = complete_5m(b1, now)
    of_ctx = of_prepare(adjusted(bars5), oi_all_months(bars5, per, 5)) if bars5 else None
    if bars5 and now - (bars5[-1]["ts"] + 300) > 900 and in_session(now_ist()):
        log("DATA STALE: last complete 5m bar %s" % ist(bars5[-1]["ts"]).strftime("%H:%M"))
    vix_v = vix()
    expiry, legs = option_chain(INDICES["NIFTY"]["sid"])
    card, table = build_card(bars5, legs, expiry, futs[0]["expiry"], vix_v, lot, LIVE_FILTER)
    breadth = book = None
    try:
        breadth, book, n_eq = quotes_snapshot(futs[0]["sid"])
        if breadth and breadth["n"] < n_eq:
            log("breadth: %d of %d NIFTY 50 quotes usable" % (breadth["n"], n_eq))
    except Exception as e:
        log("breadth/order-book snapshot failed:", str(e)[:160])
    of_st = orderflow_state(adjusted(bars5), of_ctx, len(bars5) - 1, breadth, book) if of_ctx and len(bars5) > 20 else None
    state["orderflow"] = {"state": of_st, "breadth": breadth, "book": book, "bar": card.get("bar")}
    text = card_text(card, table) + "\n" + of_text(of_st)
    if breadth:
        text += "\nBREADTH (equal-weight, %s): adv %d dec %d unch %d | A/D %.2f | avg %+.2f%% | sectors %s | leaders %s | laggards %s" % (
            NIFTY50_AS_OF.split(",")[0], breadth["advances"], breadth["declines"], breadth["unchanged"], breadth["ad_ratio"],
            breadth["mean_pct"], ", ".join("%s %s %+.2f%%" % (k, v["lean"], v["median_pct"]) for k, v in sorted(
                breadth["sectors"].items(), key=lambda kv: -kv[1]["median_pct"])), breadth["leaders"], breadth["laggards"])
    if book:
        text += "\nBOOK (resting orders): total buy/sell imbalance %+.2f | 5-level %+.2f | spread %.2f" % (
            book["total_imb"], book["depth_imb"], book["spread"])
    log(text.replace("\n", " | "))
    key = ("card", card.get("bar"), card["signal"])
    if card["signal"] in ("LONG", "SHORT") and key not in state["sent"]:
        state["sent"].add(key)
        telegram(text)
    if of_st and of_st["signal"] != "NONE":
        k2 = ("of", card.get("bar"), of_st["signal"])
        if k2 not in state["sent"] and state.get("last_of_signal") != of_st["signal"]:
            state["sent"].add(k2)
            telegram("[NIFTY] %s (research signal, not a trade) @%s\n%s" % (of_st["signal"], card.get("bar"), of_text(of_st)))
    state["last_of_signal"] = of_st["signal"] if of_st else None
    if PAPER_TRADE:
        closed_tr, open_pos, events = ledger_build(bars5, lot, LEDGER_START, of_ctx)
        for ts, tid, st_, msg in events:
            k = (tid, st_)
            if k in state["sent"]:
                continue
            state["sent"].add(k)
            if ts >= state["start_ts"] - 600:
                log(msg)
                telegram(msg)
        state["ledger"] = closed_tr
        state["open"] = open_pos


def run():
    log("NIFTY QUANT ENGINE (Dhan Cloud, read-only) starting")
    if RUN_SELFTEST_ON_START:
        selftest()
    if not configured():
        log("Credentials missing: Env Variables tab -> DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (optional TELEGRAM_BOT_TOKEN,"
            " TELEGRAM_CHAT_ID), then Deploy again.")
    elif RUN_AUDIT_ON_START:
        try:
            run_audit(AUDIT_MONTHS)
        except Exception:
            log("audit failed:", traceback.format_exc()[-1500:])
    state = {"sent": set(), "last_bar": None, "start_ts": time.time()}
    log("LIVE %s | filter F3+F4 %s | paper %s (%s execution, ledger from %s)" % (LIVE, LIVE_FILTER, PAPER_TRADE, EXEC_LIVE, LEDGER_START))
    while LIVE:
        t = now_ist()
        if configured() and PAPER_TRADE and t.weekday() < 5 and t.time() >= dt.time(15, 25) \
                and state.get("summary_day") != t.date().isoformat() and state.get("day") == t.date():
            state["summary_day"] = t.date().isoformat()
            msg = ledger_summary(state.get("ledger", []), t.date().isoformat())
            log(msg)
            telegram(msg)
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
