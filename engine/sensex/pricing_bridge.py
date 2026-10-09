"""
Forward, gate and per-leg Black-76 for ONE snapshot.
Port of server/src/pricingBridge.ts (fairPrice, attachPricing).

Refuses (raises) when the chain and futures quote are not one snapshot, when
the contract has expired, or when the gate blocks. No path to the index LTP.
"""

from __future__ import annotations

from .errors import GateBlockedError, PricingValidationError, SnapshotSkewError
from .gate import check_gate
from .integrity import check_snapshot_skew
from .jscompat import is_finite, is_number, js_str
from .pricing import b76_greeks, b76_iv, discount_factor, parity_forward, year_fraction_to_expiry

MAX_MID_SPREAD_FRAC = 0.05


def fair_price(leg):
    """Tight two-sided mid, else LTP, else None."""
    if not leg:
        return None
    b, a = leg["topBidPrice"], leg["topAskPrice"]
    if b is not None and a is not None and b > 0 and a > 0 and a >= b:
        mid = (a + b) / 2
        if (a - b) / mid <= MAX_MID_SPREAD_FRAC:
            return {"price": mid, "source": "mid"}
    l = leg["lastPrice"]
    return {"price": l, "source": "ltp"} if is_number(l) and l > 0 else None


def _nearest_strike(strikes, reference):
    if not strikes:
        return None
    best = strikes[0]
    for k in strikes[1:]:
        if abs(k - reference) < abs(best - reference):
            best = k
    return best


def attach_pricing(chain: dict, futures, risk_free_rate: float, max_snapshot_skew_ms: float,
                   atm_hint=None, band_pct=None, thresholds=None, now_ms=None, futures_expiry=None) -> dict:
    # 1. single-timestamp rule
    skew_ms, same_fetch = 0, True
    if futures:
        skew = check_snapshot_skew(
            {"fetchId": chain["provenance"]["fetchId"], "epochMs": chain["provenance"]["epochMs"]},
            {"fetchId": futures["provenance"]["fetchId"], "epochMs": futures["provenance"]["epochMs"]},
            max_snapshot_skew_ms,
        )
        if not skew["ok"]:
            raise SnapshotSkewError(skew["message"], {
                "chainFetchId": chain["provenance"]["fetchId"],
                "futuresFetchId": futures["provenance"]["fetchId"],
                "skewMs": skew["skewMs"], "maxSkewMs": max_snapshot_skew_ms})
        skew_ms, same_fetch = skew["skewMs"], skew["sameFetch"]

    # 2. time from the snapshot's own clock
    as_of = now_ms if now_ms is not None else chain["provenance"]["epochMs"]
    T = year_fraction_to_expiry(as_of, chain["expiry"])
    if not (T > 0):
        raise PricingValidationError(
            "Expiry %s is at or before the snapshot time — T = %s. Cannot price an expired contract."
            % (chain["expiry"], js_str(T)),
            {"expiry": chain["expiry"], "asOfMs": as_of, "T": T})
    r = risk_free_rate
    DF = discount_factor(r, T)

    # 3. complete two-sided pairs
    legs = []
    for s in chain["strikes"]:
        c, p = fair_price(s["ce"]), fair_price(s["pe"])
        if not c or not p:
            continue
        legs.append({"strike": s["strike"], "callPrice": c["price"], "putPrice": p["price"]})
    if len(legs) < 2:
        raise GateBlockedError([
            "Only %d strike(s) have a usable CE/PE pair. Put-call parity needs at least 2 to recover a forward."
            % len(legs)])

    paired = [l["strike"] for l in legs]
    atm_from_parity = paired[len(paired) // 2]
    best = float("inf")
    for l in legs:
        d = abs(l["callPrice"] - l["putPrice"])
        if d < best:
            best, atm_from_parity = d, l["strike"]
    hint = atm_hint if atm_hint is not None else atm_from_parity

    # 4. forward (median, never the regression slope)
    fd = parity_forward(legs, hint, r, T, band_pct=0.015 if band_pct is None else band_pct)

    # 5. gate
    listed = futures["ltp"] if futures and is_finite(futures["ltp"]) else None
    gap = (year_fraction_to_expiry(as_of, futures_expiry) - T
           if futures_expiry and futures_expiry != chain["expiry"] else 0)
    gate = check_gate(
        parity_forward=fd["forward"], listed_future=listed, per_strike_spread=fd["spread"],
        index_ltp=chain["underlyingLtpDoNotUseAsSpot"],
        slope_within_tolerance=fd["slopeDiagnostic"]["withinTolerance"],
        slope_relative_error=fd["slopeDiagnostic"]["relativeError"],
        future_expiry_gap_years=gap, thresholds=thresholds)
    if gate["blocked"]:
        raise GateBlockedError(gate["reasons"], {
            "parityForward": fd["forward"],
            "listedFuture": futures["ltp"] if futures else None,
            "perStrikeSpread": fd["spread"],
            "divergenceVsFuture": gate["divergenceVsFuture"],
            "impliedCarryAnnual": gate["impliedCarryAnnual"],
            "futureCheckMode": gate["futureCheckMode"],
            "futuresExpiry": futures_expiry,
            "indexDivergence": gate["indexDivergence"],
            "thresholds": gate["thresholds"],
        })

    # 6. price every leg against THIS forward
    F = fd["forward"]
    priced = []
    for s in chain["strikes"]:
        for typ in ("CE", "PE"):
            leg = s["ce"] if typ == "CE" else s["pe"]
            if not leg or not is_number(leg["lastPrice"]) or not (leg["lastPrice"] > 0):
                continue
            fp = fair_price(leg)
            sigma = b76_iv(fp["price"] if fp else leg["lastPrice"], F, s["strike"], T, r, typ)
            g = None if sigma is None or sigma <= 0 else b76_greeks(F, s["strike"], T, sigma, r, typ)
            vendor = leg["vendorQuarantined"]["impliedVolatility"]
            ours = None if sigma is None else sigma * 100
            priced.append({
                "strike": s["strike"], "type": typ, "securityId": leg["securityId"],
                "marketPrice": leg["lastPrice"], "ivPct": ours,
                "delta": g["delta"] if g else None, "gamma": g["gamma"] if g else None,
                "vega": g["vega"] if g else None, "theta": g["theta"] if g else None,
                "rho": g["rho"] if g else None, "moneyness": s["strike"] / F,
                "vendorIvPct": vendor,
                "vendorIvDeltaPct": ours - vendor if ours is not None and vendor is not None else None,
            })

    return {
        "asOfMs": as_of, "expiry": chain["expiry"], "T": T, "calendarDaysToExpiry": T * 365,
        "riskFreeRate": r, "discountFactor": DF, "forward": F, "forwardDetail": fd,
        "listedFuture": listed, "indexLtpDoNotUseAsSpot": chain["underlyingLtpDoNotUseAsSpot"],
        "gate": gate, "legs": priced, "atmStrike": _nearest_strike(paired, F),
        "snapshot": {"chainFetchId": chain["provenance"]["fetchId"],
                     "futuresFetchId": futures["provenance"]["fetchId"] if futures else None,
                     "skewMs": skew_ms, "sameFetch": same_fetch},
    }
