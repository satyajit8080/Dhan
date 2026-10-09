"""
Breakout / breakdown levels from underlying price action.

Port of server/src/levels.ts plus the helpers it uses from indicators.ts
(trueRanges, wilderSmooth, atr, vwap, sessionVwap, sessions, openingRange,
istDateOf, istTimeOf) and structure.ts (findSwingPoints). PURE.

Candles are dicts: timestampMs, open, high, low, close, volume, openInterest.
Option-chain data is never an input: a trigger must come from price action.
"""

from __future__ import annotations

from .jscompat import is_finite, iso_of, js_max, js_round, js_str, js_sum, to_fixed

IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000

STRUCTURAL = {"previous_session_high", "previous_session_low", "opening_range_high",
              "opening_range_low", "vwap"}

# ------------------------------------------------------------- indicators.ts


def ist_date_of(epoch_ms):
    return iso_of(epoch_ms + IST_OFFSET_MS)[:10]


def ist_time_of(epoch_ms):
    return iso_of(epoch_ms + IST_OFFSET_MS)[11:16]


def true_ranges(candles):
    tr = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c["high"] - c["low"])
            continue
        pc = candles[i - 1]["close"]
        tr.append(js_max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    return tr


def wilder_smooth(values, period):
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = 0
    for i in range(period):
        s += values[i]
    prev = s / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev + (values[i] - prev) / period
        out[i] = prev
    return out


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    s = wilder_smooth(true_ranges(candles), period)
    return s[-1] if s else None


def vwap(candles):
    pv = 0
    vol = 0
    for c in candles:
        v = c["volume"] if c["volume"] is not None else 0
        if v <= 0:
            continue
        pv += ((c["high"] + c["low"] + c["close"]) / 3) * v
        vol += v
    return pv / vol if vol > 0 else None


def session_vwap(candles):
    if not candles:
        return None
    last = ist_date_of(candles[-1]["timestampMs"])
    return vwap([c for c in candles if ist_date_of(c["timestampMs"]) == last])


def sessions(candles):
    by_date = {}
    for c in candles:
        by_date.setdefault(ist_date_of(c["timestampMs"]), []).append(c)
    out = []
    for date, bars in by_date.items():
        high, low, volume, has_vol = float("-inf"), float("inf"), 0, False
        for b in bars:
            if b["high"] > high:
                high = b["high"]
            if b["low"] < low:
                low = b["low"]
            if b["volume"] is not None:
                volume += b["volume"]
                has_vol = True
        out.append({"date": date, "open": bars[0]["open"], "high": high, "low": low,
                    "close": bars[-1]["close"], "volume": volume if has_vol else None, "bars": len(bars)})
    out.sort(key=lambda s: s["date"])
    return out


def opening_range(candles, minutes=15):
    if not candles:
        return None
    last = ist_date_of(candles[-1]["timestampMs"])
    day = [c for c in candles if ist_date_of(c["timestampMs"]) == last]
    if not day:
        return None
    start = day[0]["timestampMs"]
    win = [c for c in day if c["timestampMs"] < start + minutes * 60_000]
    if not win:
        return None
    high, low = float("-inf"), float("inf")
    for b in win:
        if b["high"] > high:
            high = b["high"]
        if b["low"] < low:
            low = b["low"]
    return {"high": high, "low": low, "bars": len(win),
            "fromIst": ist_time_of(win[0]["timestampMs"]), "toIst": ist_time_of(win[-1]["timestampMs"])}


# --------------------------------------------------------------- structure.ts


def find_swing_points(candles, lookback=2):
    out = []
    if len(candles) < lookback * 2 + 1:
        return out
    for i in range(lookback, len(candles) - lookback):
        c = candles[i]
        is_high = is_low = True
        for j in range(i - lookback, i + lookback + 1):
            if j == i:
                continue
            if candles[j]["high"] >= c["high"]:
                is_high = False
            if candles[j]["low"] <= c["low"]:
                is_low = False
        if is_high:
            out.append({"index": i, "timestampMs": c["timestampMs"], "istTime": ist_time_of(c["timestampMs"]),
                        "price": c["high"], "kind": "high"})
        elif is_low:
            out.append({"index": i, "timestampMs": c["timestampMs"], "istTime": ist_time_of(c["timestampMs"]),
                        "price": c["low"], "kind": "low"})
    return out


# ------------------------------------------------------------------ levels.ts


def aggregate_candles(candles, minutes):
    if not candles or minutes <= 1:
        return candles
    bucket_ms = minutes * 60_000
    out, bucket = [], []
    start = (candles[0]["timestampMs"] // bucket_ms) * bucket_ms

    def flush():
        if not bucket:
            return
        high, low, volume, has_vol = float("-inf"), float("inf"), 0, False
        for b in bucket:
            if b["high"] > high:
                high = b["high"]
            if b["low"] < low:
                low = b["low"]
            if b["volume"] is not None:
                volume += b["volume"]
                has_vol = True
        out.append({"timestampMs": bucket[0]["timestampMs"], "open": bucket[0]["open"], "high": high,
                    "low": low, "close": bucket[-1]["close"], "volume": volume if has_vol else None,
                    "openInterest": bucket[-1]["openInterest"]})
        bucket.clear()

    for c in candles:
        this = (c["timestampMs"] // bucket_ms) * bucket_ms
        if this != start:
            flush()
            start = this
        bucket.append(c)
    flush()
    return out


def collect_candidates(candles, swing_lookback, consolidation_window):
    out = []
    sess = sessions(candles)
    current = sess[-1] if sess else None
    previous = sess[-2] if len(sess) > 1 else None
    orr = opening_range(candles, 15)
    for s in find_swing_points(candles, swing_lookback):
        out.append({"price": s["price"], "source": "swing_high" if s["kind"] == "high" else "swing_low"})
    if current:
        out.append({"price": current["high"], "source": "session_high"})
        out.append({"price": current["low"], "source": "session_low"})
    if previous:
        out.append({"price": previous["high"], "source": "previous_session_high"})
        out.append({"price": previous["low"], "source": "previous_session_low"})
    if orr:
        out.append({"price": orr["high"], "source": "opening_range_high"})
        out.append({"price": orr["low"], "source": "opening_range_low"})
    v = session_vwap(candles)
    if v is not None:
        out.append({"price": v, "source": "vwap"})
    window = candles[-min(consolidation_window, len(candles)):] if candles else []
    if window:
        hi, lo = float("-inf"), float("inf")
        for c in window:
            if c["high"] > hi:
                hi = c["high"]
            if c["low"] < lo:
                lo = c["low"]
        out.append({"price": hi, "source": "consolidation_high"})
        out.append({"price": lo, "source": "consolidation_low"})
    return [c for c in out if is_finite(c["price"])]


def count_touches(candles, price, tolerance, kind):
    n = 0
    for c in candles:
        probe = c["high"] if kind == "resistance" else c["low"]
        if abs(probe - price) <= tolerance:
            n += 1
    return n


def _cluster(cands, tolerance):
    clusters = []
    for c in sorted(cands, key=lambda x: x["price"]):
        if clusters:
            last = clusters[-1]
            mean = js_sum(last["prices"]) / len(last["prices"])
            if abs(c["price"] - mean) <= tolerance:
                last["prices"].append(c["price"])
                if c["source"] not in last["sources"]:
                    last["sources"].append(c["source"])
                continue
        clusters.append({"prices": [c["price"]], "sources": [c["source"]]})
    return clusters


def _round_to(value, step):
    if not step or step <= 0:
        return value
    return js_round(value / step) * step


def derive_levels(candles, spot, confirmation_buffer=None, tolerance=None, min_touches=None,
                  swing_lookback=None, consolidation_window=None, round_to=None) -> dict:
    diagnostics = []
    a = atr(candles, 14)
    buffer = 5 if confirmation_buffer is None else confirmation_buffer
    min_t = 2 if min_touches is None else min_touches
    look = 2 if swing_lookback is None else swing_lookback
    cwin = 20 if consolidation_window is None else consolidation_window
    rnd = 5 if round_to is None else round_to
    tol = tolerance if tolerance is not None else js_max(spot * 0.0003, a * 0.25 if a is not None else 0, 2)

    empty = {
        "spot": spot, "atr14": a, "toleranceUsed": tol, "confirmationBuffer": buffer,
        "resistance": None, "support": None, "breakoutAbove": None, "breakdownBelow": None,
        "nextResistance": None, "nextSupport": None, "allResistance": [], "allSupport": [],
        "rejected": [], "barsAnalyzed": len(candles), "timeframeMinutes": None, "diagnostics": diagnostics,
    }
    if not candles:
        diagnostics.append("No candles supplied — no level can be derived.")
        return empty
    if not is_finite(spot) or spot <= 0:
        diagnostics.append("Spot price unavailable — cannot place levels relative to price.")
        return empty

    tf_ms = candles[1]["timestampMs"] - candles[0]["timestampMs"] if len(candles) > 1 else None
    tf_min = js_round(tf_ms / 60_000) if tf_ms else None

    cands = collect_candidates(candles, look, cwin)
    if not cands:
        diagnostics.append("No candidate levels found in the series.")
        return {**empty, "timeframeMinutes": tf_min}

    confirmed, rejected = [], []
    for cl in _cluster(cands, tol):
        mean = js_sum(cl["prices"]) / len(cl["prices"])
        kind = "resistance" if mean > spot else "support"
        touches = count_touches(candles, mean, tol, kind)
        structural = any(s in STRUCTURAL for s in cl["sources"])
        if not structural and touches < min_t:
            rejected.append({
                "price": mean, "sources": cl["sources"], "touches": touches,
                "reason": "Isolated spike: %d touch(es) within %s pts, below the %s required for a "
                          "non-structural level." % (touches, to_fixed(tol, 1), js_str(min_t)),
            })
            continue
        distance = mean - spot
        confirmed.append({
            "price": mean, "kind": kind, "touches": touches, "sources": cl["sources"],
            "structural": structural, "tolerance": tol, "distanceFromSpot": distance,
            "distanceInAtr": distance / a if a and a > 0 else None,
            "note": ("Structural level (%s), %d touch(es)." % (", ".join(cl["sources"]), touches)) if structural
            else ("Confirmed by %d touches within %s pts." % (touches, to_fixed(tol, 1))),
        })

    all_res = sorted((l for l in confirmed if l["kind"] == "resistance"), key=lambda l: l["price"])
    all_sup = sorted((l for l in confirmed if l["kind"] == "support"), key=lambda l: -l["price"])
    res = all_res[0] if all_res else None
    sup = all_sup[0] if all_sup else None
    if not res:
        diagnostics.append("No confirmed resistance above spot. Price may be at the session extreme, "
                           "or every candidate above spot was an isolated spike.")
    if not sup:
        diagnostics.append("No confirmed support below spot. Price may be at the session extreme, "
                           "or every candidate below spot was an isolated spike.")
    diagnostics.append("%d level(s) confirmed, %d rejected as spikes." % (len(confirmed), len(rejected)))
    return {
        "spot": spot, "atr14": a, "toleranceUsed": tol, "confirmationBuffer": buffer,
        "resistance": res, "support": sup,
        "breakoutAbove": _round_to(res["price"] + buffer, rnd) if res else None,
        "breakdownBelow": _round_to(sup["price"] - buffer, rnd) if sup else None,
        "nextResistance": all_res[1] if len(all_res) > 1 else None,
        "nextSupport": all_sup[1] if len(all_sup) > 1 else None,
        "allResistance": all_res, "allSupport": all_sup, "rejected": rejected,
        "barsAnalyzed": len(candles), "timeframeMinutes": tf_min, "diagnostics": diagnostics,
    }


# ------------------------------------------------------------- trade plans

_NOTE = ("Premium targets are delta/gamma projections from current spot, not quotes. "
         "Underlying levels come from confirmed price action.")


def project_premium(entry, delta, gamma, move):
    if delta is None:
        return None
    second = 0.5 * gamma * move * move if gamma is not None else 0
    return js_max(0, entry + delta * move + second)


def build_trade_plan(side, levels, entry, delta, gamma):
    a = levels["atr14"]
    spot = levels["spot"]
    if side == "CE":
        if levels["breakoutAbove"] is None:
            return None
        trig = levels["breakoutAbove"]
        nr, sp = levels["nextResistance"], levels["support"]
        target = nr["price"] if nr else (trig + a if a else trig * 1.002)
        tsrc = ("Next confirmed resistance (%d touches)." % nr["touches"]) if nr else \
            "No further resistance confirmed; projected one ATR above the trigger."
        stop = sp["price"] if sp else (trig - a if a else trig * 0.998)
        ssrc = ("Nearest confirmed support (%d touches)." % sp["touches"]) if sp else \
            "No confirmed support; projected one ATR below the trigger."
        rr = abs((target - trig) / (trig - stop)) if trig - stop != 0 else None
    else:
        if levels["breakdownBelow"] is None:
            return None
        trig = levels["breakdownBelow"]
        ns, rs = levels["nextSupport"], levels["resistance"]
        target = ns["price"] if ns else (trig - a if a else trig * 0.998)
        tsrc = ("Next confirmed support (%d touches)." % ns["touches"]) if ns else \
            "No further support confirmed; projected one ATR below the trigger."
        stop = rs["price"] if rs else (trig + a if a else trig * 1.002)
        ssrc = ("Nearest confirmed resistance (%d touches)." % rs["touches"]) if rs else \
            "No confirmed resistance; projected one ATR above the trigger."
        rr = abs((trig - target) / (stop - trig)) if stop - trig != 0 else None
    return {
        "side": side, "triggerLevel": trig, "targetLevel": target, "targetSource": tsrc,
        "stopLevel": stop, "stopSource": ssrc, "entryPremium": entry,
        "triggerPremium": project_premium(entry, delta, gamma, trig - spot),
        "targetPremium": project_premium(entry, delta, gamma, target - spot),
        "stopPremium": project_premium(entry, delta, gamma, stop - spot),
        "riskRewardRatio": rr, "note": _NOTE,
    }
