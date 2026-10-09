"""
Two-stage liquidity. Port of server/src/liquidity.ts.

Stage 1 screens chain top-of-book (no extra request). Stage 2 walks 5-level
depth for a real size and grades the round-trip cost. PURE.
"""

from __future__ import annotations

import math

from .jscompat import js_max, js_min, js_str, to_fixed

GRADE_THRESHOLDS = {"A": 0.6, "B": 1.2, "C": 2.5}


def walk_book(levels, quantity):
    remaining = quantity
    notional = 0
    filled = 0
    consumed = 0
    for lvl in levels:
        if remaining <= 0:
            break
        if not (lvl["price"] > 0) or not (lvl["quantity"] > 0):
            continue
        take = js_min(remaining, lvl["quantity"])
        notional += take * lvl["price"]
        filled += take
        remaining -= take
        consumed += 1
    return {
        "filledQuantity": filled,
        "shortfall": js_max(0, remaining),
        "vwap": notional / filled if filled > 0 else math.nan,
        "notional": notional,
        "levelsConsumed": consumed,
        "incomplete": remaining > 0,
    }


def _grade(roundtrip, incomplete):
    if roundtrip is None or not math.isfinite(roundtrip):
        return "F"
    if incomplete:
        return "F"
    if roundtrip <= GRADE_THRESHOLDS["A"]:
        return "A"
    if roundtrip <= GRADE_THRESHOLDS["B"]:
        return "B"
    if roundtrip <= GRADE_THRESHOLDS["C"]:
        return "C"
    return "F"


def assess_depth(quote: dict, lots, lot_size) -> dict:
    quantity = lots * lot_size
    reasons = []
    if not quote["depth"]:
        return {
            "securityId": quote["securityId"], "lots": lots, "lotSize": lot_size, "quantity": quantity,
            "bestBid": None, "bestAsk": None, "midPrice": None, "quotedSpreadPct": None,
            "effectiveBuy": None, "effectiveSell": None, "roundtripPct": None, "slippageMultiple": None,
            "grade": "F", "reasons": ["No market depth in the response."], "buyWalk": None, "sellWalk": None,
        }
    asks = sorted((l for l in quote["depth"]["sell"] if l["price"] > 0 and l["quantity"] > 0),
                  key=lambda l: l["price"])
    bids = sorted((l for l in quote["depth"]["buy"] if l["price"] > 0 and l["quantity"] > 0),
                  key=lambda l: -l["price"])
    best_ask = asks[0]["price"] if asks else None
    best_bid = bids[0]["price"] if bids else None
    mid = (best_ask + best_bid) / 2 if best_ask is not None and best_bid is not None else None
    quoted = ((best_ask - best_bid) / mid) * 100 if mid is not None and mid > 0 else None
    buy, sell = walk_book(asks, quantity), walk_book(bids, quantity)
    if buy["incomplete"]:
        reasons.append("Ask side covers only %s/%s units across %d visible level(s)."
                       % (js_str(buy["filledQuantity"]), js_str(quantity), len(asks)))
    if sell["incomplete"]:
        reasons.append("Bid side covers only %s/%s units across %d visible level(s)."
                       % (js_str(sell["filledQuantity"]), js_str(quantity), len(bids)))
    eb = buy["vwap"] if math.isfinite(buy["vwap"]) else None
    es = sell["vwap"] if math.isfinite(sell["vwap"]) else None
    rt = ((eb - es) / mid) * 100 if eb is not None and es is not None and mid is not None and mid > 0 else None
    slip = rt / quoted if rt is not None and quoted is not None and quoted > 0 else None
    if slip is not None and slip > 2:
        reasons.append("Real round-trip is %sx the quoted spread at %s lot(s)." % (to_fixed(slip, 2), js_str(lots)))
    grade = _grade(rt, buy["incomplete"] or sell["incomplete"])
    if grade == "A":
        reasons.append("Executable at size with minimal give-up.")
    return {
        "securityId": quote["securityId"], "lots": lots, "lotSize": lot_size, "quantity": quantity,
        "bestBid": best_bid, "bestAsk": best_ask, "midPrice": mid, "quotedSpreadPct": quoted,
        "effectiveBuy": eb, "effectiveSell": es, "roundtripPct": rt, "slippageMultiple": slip,
        "grade": grade, "reasons": reasons, "buyWalk": buy, "sellWalk": sell,
    }


def screen_chain(strikes, max_quoted_spread_pct=1.5, min_oi=0, min_top_quantity=1,
                 band_pct=None, reference=None, max_candidates=12) -> dict:
    out = []
    for s in strikes:
        if band_pct is not None and reference is not None:
            if abs(s["strike"] - reference) > reference * band_pct:
                continue
        for typ in ("CE", "PE"):
            leg = s["ce"] if typ == "CE" else s["pe"]
            if not leg:
                continue
            bid, ask = leg["topBidPrice"], leg["topAskPrice"]
            mid = (bid + ask) / 2 if bid is not None and ask is not None and bid > 0 and ask > 0 else None
            spread = ((ask - bid) / mid) * 100 if mid is not None and mid > 0 else None
            rej = []
            if mid is None:
                rej.append("No two-sided top-of-book.")
            if bid is not None and ask is not None and bid >= ask and bid > 0 and ask > 0:
                rej.append("Crossed top-of-book.")
            if spread is not None and spread > max_quoted_spread_pct:
                rej.append("Quoted spread %s%% exceeds %s%%." % (to_fixed(spread, 2), js_str(max_quoted_spread_pct)))
            if min_oi > 0 and (leg["oi"] or 0) < min_oi:
                rej.append("OI %s below %s." % (js_str(leg["oi"] if leg["oi"] is not None else 0), js_str(min_oi)))
            if (leg["topBidQuantity"] if leg["topBidQuantity"] is not None else 0) < min_top_quantity:
                rej.append("Bid side shows no size.")
            if (leg["topAskQuantity"] if leg["topAskQuantity"] is not None else 0) < min_top_quantity:
                rej.append("Ask side shows no size.")
            if leg["securityId"] is None:
                rej.append("No security_id — cannot fetch depth.")
            out.append({
                "strike": s["strike"], "type": typ, "securityId": leg["securityId"],
                "lastPrice": leg["lastPrice"], "topBid": bid, "topAsk": ask,
                "topBidQty": leg["topBidQuantity"], "topAskQty": leg["topAskQuantity"],
                "midPrice": mid, "quotedSpreadPct": spread, "oi": leg["oi"], "volume": leg["volume"],
                "candidate": len(rej) == 0, "rejectReasons": rej,
            })
    cands = sorted((c for c in out if c["candidate"]),
                   key=lambda c: c["quotedSpreadPct"] if c["quotedSpreadPct"] is not None else 1e9)
    return {"all": out, "candidates": cands[:max_candidates]}
