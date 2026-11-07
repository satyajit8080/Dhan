"""Entry rules, levels filter, stops/targets, sizing and statutory charges (mirror of main.py)."""
import math

from indicators import ok

CHARGES = {
    "brokerage_per_order": 20.0,
    "fut": {"stt_sell": 0.0005, "txn_NSE": 0.0000173, "txn_BSE": 0.0, "stamp_buy": 0.00002},
    "opt": {"stt_sell": 0.0015, "txn_NSE": 0.0003503, "txn_BSE": 0.000325, "stamp_buy": 0.00003},
    "sebi": 0.000001, "gst": 0.18,
}


def charges(kind, exch, buy_value, sell_value):
    r = CHARGES[kind]
    brk = 2 * CHARGES["brokerage_per_order"]
    txn = r.get("txn_" + exch, r["txn_NSE"]) * (buy_value + sell_value)
    sebi = CHARGES["sebi"] * (buy_value + sell_value)
    tot = brk + r["stt_sell"] * sell_value + txn + sebi + r["stamp_buy"] * buy_value + CHARGES["gst"] * (brk + txn + sebi)
    return tot


def _is_squeeze(bw, j, lb):
    if j < lb:
        return False
    w = bw[j - lb + 1:j + 1]
    return all(ok(v) for v in w) and bw[j] <= min(w) + 1e-12


def signal_at(ind, i, levels, p):
    g = {k: v[i] for k, v in ind.items()}
    need = ("atr", "adx", "di_plus", "di_minus", "rsi", "er", "st_dir", "bw")
    if i < 1 or any(not ok(g[k]) for k in need):
        return {"signal": "NO_TRADE", "setup": "WARMUP", "reason": "indicators warming up"}
    c, vw, atr = g["close"], g["vwap"], g["atr"]
    rp, cp = ind["rsi"][i - 1], ind["close"][i - 1]
    trend_ok = g["adx"] > p["adx_trend"] and g["er"] > p["er_trend"]
    out = {"signal": "NO_TRADE", "setup": "CHOP",
           "reason": "chop: ADX %.1f / ER %.2f below %.0f / %s" % (g["adx"], g["er"], p["adx_trend"], p["er_trend"])}
    lb = p["squeeze_lookback"]
    sq_now = _is_squeeze(ind["bw"], i, lb)
    sq_recent = any(_is_squeeze(ind["bw"], j, lb) for j in range(max(lb, i - p["squeeze_memory"]), i))
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
        if not ok(L) or abs(c - L) >= p["level_atr"] * atr:
            continue
        broke = (cp <= L < c) if d > 0 else (cp >= L > c)
        if not broke:
            return {"signal": "NO_TRADE", "setup": "NEAR_LEVEL",
                    "reason": "%s %s skipped: close %.1f within %sxATR of %s %.1f" % (cand["setup"], cand["signal"], c, p["level_atr"], name, L)}
        cand["reason"] += "; closing break of %s %.1f" % (name, L)
    sd = p["stop_atr"] * atr
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
