"""Builds the signal card: indicators on closed 5m bars, strike selection, entry/SL/targets, size, charges."""
import datetime as dt

import config
import indicators as ind
import rules

IST = ind.IST


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
    for (k, side), o in legs.items():
        s[side][0] += float(o.get("volume") or 0)
        s[side][1] += float(o.get("oi") or 0)
    pv = s["PE"][0] / s["CE"][0] if s["CE"][0] else float("nan")
    po = s["PE"][1] / s["CE"][1] if s["CE"][1] else float("nan")
    return pv, po


def pick_strike(legs, fwd, step, d, max_spread):
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


def build(name, b1, prev, legs, expiry, vix_v, lot, now):
    p = config.PARAMS
    bars5 = closed(resample(closed(b1, 1, now)), 5, now)
    card = {"index": name, "signal": "NO_TRADE", "setup": "NO_DATA", "reason": "only %d closed 5m bars" % len(bars5)}
    if len(bars5) < 40:
        return card, {}
    x = ind.compute(bars5)
    i = len(bars5) - 1
    lv = ind.pivots(prev["high"], prev["low"], prev["close"]) if prev else {}
    sig = rules.signal_at(x, i, lv, p)
    bt = dt.datetime.fromtimestamp(bars5[i]["ts"], IST).time()
    if sig["signal"] in ("LONG", "SHORT") and not (dt.time(9, 30) <= bt < dt.time(15, 10)):
        sig = {"signal": "NO_TRADE", "setup": "OUTSIDE_WINDOW",
               "reason": "%s %s ignored: bar %s outside entry window 09:30-15:10" % (sig["setup"], sig["signal"], bt.strftime("%H:%M"))}
    card = {"index": name, "signal": sig["signal"], "setup": sig["setup"], "reason": sig["reason"],
            "bar": dt.datetime.fromtimestamp(bars5[i]["ts"], IST).strftime("%H:%M")}
    table = {k: v[i] for k, v in x.items()}
    pv, po = pcr(legs)
    card["context"] = "PCR vol %.2f oi %.2f | VIX %.2f" % (pv, po, vix_v)
    if sig["signal"] not in ("LONG", "SHORT"):
        return card, table
    d = 1 if sig["signal"] == "LONG" else -1
    card.update({"entry": sig["entry_ref"], "sl": sig["stop"], "targets": sig["targets"],
                 "fut_lots": rules.lots_for(p["risk_rupees"], sig["stop_dist"], lot)})
    step = config.INDICES[name]["step"]
    best, notes = pick_strike(legs, sig["entry_ref"], step, d, p["max_spread_pct"])
    card["strike_notes"] = notes
    if not best:
        card["reason"] += " | no liquid ATM/ITM strike passed the spread check"
        return card, table
    _, K, side, bid, ask, sp = best
    T = max((dt.datetime.fromisoformat(expiry + "T15:30:00+05:30") - dt.datetime.now(IST)).total_seconds() / 31536000, 1e-5)
    F = sig["entry_ref"]
    iv = rules.implied_vol((bid + ask) / 2, F, K, T, config.RISK_FREE, side == "CE")
    if iv:
        adj = ask - rules.black76(F, K, T, config.RISK_FREE, iv, side == "CE")
        sl = max(rules.black76(F - d * sig["stop_dist"], K, T, config.RISK_FREE, iv, side == "CE") + adj, 0.05)
        tg = [rules.black76(t, K, T, config.RISK_FREE, iv, side == "CE") + adj for t in sig["targets"]]
    else:
        sl, tg = max(ask - 0.5 * sig["stop_dist"], 0.05), [ask + 0.5 * abs(t - F) for t in sig["targets"]]
    lots = rules.lots_for(p["risk_rupees"], ask - sl, lot)
    q = max(lots, 1) * lot
    exch = config.INDICES[name]["exch"]
    card["option"] = {"strike": K, "side": side, "entry": round(ask, 2), "sl": round(sl, 2), "targets": [round(t, 2) for t in tg],
                      "spread_pct": round(sp, 2), "iv": round(iv * 100, 2) if iv else None, "lots": lots,
                      "charges_at_sl": round(rules.charges("opt", exch, ask * q, sl * q), 2)}
    return card, table


def text(card):
    s = "[%s] %s %s @%s\n%s" % (card["index"], card["signal"], card.get("setup"), card.get("bar", ""), card.get("reason"))
    if "entry" in card:
        s += "\nFut entry %.1f SL %.1f T %s lots %d" % (card["entry"], card["sl"], ", ".join("%.1f" % t for t in card["targets"]), card["fut_lots"])
    o = card.get("option")
    if o:
        s += "\nOption %.0f%s entry %.2f SL %.2f T %s lots %d charges@SL Rs%.0f" % (
            o["strike"], o["side"], o["entry"], o["sl"], o["targets"], o["lots"], o["charges_at_sl"])
    if card.get("context"):
        s += "\n" + card["context"]
    return s
