"""
Structural and staleness checks plus the snapshot-skew rule.
Port of server/src/integrity.ts. PURE: the caller supplies `now_ms`.
"""

from __future__ import annotations

from .jscompat import is_finite, js_str, to_fixed


def _report(findings, checked_at_ms):
    return {"ok": not any(f["severity"] == "block" for f in findings), "findings": findings,
            "checkedAtMs": checked_at_ms}


def check_quote(q: dict, now_ms, max_age_ms) -> dict:
    f = []
    sid = "%s:%s" % (q["segment"], q["securityId"])
    if not is_finite(q["ltp"]):
        f.append({"severity": "block", "code": "QUOTE_NO_LTP", "message": "No usable last_price for %s." % sid})
    if q["lastTradeTimeMs"] is None:
        f.append({"severity": "warn", "code": "QUOTE_NO_TRADE_TIME", "message":
                  "%s has no last_trade_time (never traded, or the 01/01/1980 sentinel). "
                  "Staleness cannot be established from the vendor clock." % sid})
    else:
        age = now_ms - q["lastTradeTimeMs"]
        if age > max_age_ms:
            f.append({"severity": "warn", "code": "QUOTE_STALE", "message":
                      "%s last traded %ss ago, beyond the %ss freshness window."
                      % (sid, to_fixed(age / 1000, 1), to_fixed(max_age_ms / 1000, 1))})
    if q["depth"]:
        bid = next((l for l in q["depth"]["buy"] if l["price"] > 0 and l["quantity"] > 0), None)
        ask = next((l for l in q["depth"]["sell"] if l["price"] > 0 and l["quantity"] > 0), None)
        if bid and ask and bid["price"] >= ask["price"]:
            f.append({"severity": "block", "code": "QUOTE_CROSSED", "message":
                      "Crossed book on %s: bid %s >= ask %s. Refusing to derive liquidity from it."
                      % (sid, js_str(bid["price"]), js_str(ask["price"]))})
        if not bid or not ask:
            f.append({"severity": "warn", "code": "QUOTE_ONE_SIDED", "message": "One-sided or empty book on %s." % sid})
    if len(q["quarantined"]) > 0:
        f.append({"severity": "warn", "code": "QUOTE_QUARANTINED_FIELDS", "message":
                  "Quarantined unparseable fields: %s." % ", ".join(q["quarantined"].keys())})
    return _report(f, now_ms)


def check_chain(chain: dict, now_ms) -> dict:
    f = []
    if len(chain["strikes"]) == 0:
        f.append({"severity": "block", "code": "CHAIN_EMPTY", "message":
                  "Option chain for %s %s has no strikes." % (chain["underlying"], chain["expiry"])})
        return _report(f, now_ms)
    both = crossed = zero = suspect = 0
    for s in chain["strikes"]:
        if s["ce"] and s["pe"]:
            both += 1
        for leg in (s["ce"], s["pe"]):
            if not leg:
                continue
            if leg["lastPrice"] is not None and leg["lastPrice"] <= 0:
                zero += 1
            b, a = leg["topBidPrice"], leg["topAskPrice"]
            if b is not None and a is not None and b > 0 and a > 0 and b >= a:
                crossed += 1
            iv = leg["vendorQuarantined"]["impliedVolatility"]
            if iv is None or iv <= 0 or iv > 60:
                suspect += 1
    if both < 2:
        f.append({"severity": "block", "code": "CHAIN_INSUFFICIENT_PAIRS", "message":
                  "Only %d strike(s) have both a CE and a PE. Put-call parity needs at least 2 "
                  "complete pairs to recover a forward." % both})
    if crossed > 0:
        f.append({"severity": "warn", "code": "CHAIN_CROSSED_LEGS", "message":
                  "%d leg(s) show a crossed top-of-book; they are excluded from parity." % crossed})
    if zero > 0:
        f.append({"severity": "warn", "code": "CHAIN_ZERO_PRICES", "message":
                  "%d leg(s) have a non-positive last price." % zero})
    total = sum(int(bool(s["ce"])) + int(bool(s["pe"])) for s in chain["strikes"])
    if total > 0:
        pct = (suspect / total) * 100
        f.append({"severity": "info", "code": "VENDOR_IV_QUARANTINED", "message":
                  "%d/%d legs (%s%%) carry vendor IV that is zero, missing or >60%%. All vendor IV and "
                  "Greeks are quarantined regardless; this server computes its own."
                  % (suspect, total, to_fixed(pct, 1))})
    if chain["underlyingLtpDoNotUseAsSpot"] is not None:
        f.append({"severity": "info", "code": "CHAIN_CARRIES_INDEX_LTP", "message":
                  "Chain reports underlying last_price %s. This is the INDEX LTP and is never used as "
                  "spot for option maths." % js_str(chain["underlyingLtpDoNotUseAsSpot"])})
    return _report(f, now_ms)


def check_snapshot_skew(a: dict, b: dict, max_skew_ms) -> dict:
    skew = abs(a["epochMs"] - b["epochMs"])
    same = a["fetchId"] == b["fetchId"]
    if same:
        return {"ok": True, "skewMs": skew, "sameFetch": same, "message": "Single atomic fetch."}
    if skew <= max_skew_ms:
        return {"ok": True, "skewMs": skew, "sameFetch": same, "message":
                "Distinct fetches %sms apart, inside the %sms window." % (js_str(skew), js_str(max_skew_ms))}
    return {"ok": False, "skewMs": skew, "sameFetch": same, "message":
            "Snapshot skew %sms exceeds the %sms limit. The chain and the futures leg are not one "
            "snapshot; mixing them would price across two timestamps." % (js_str(skew), js_str(max_skew_ms))}
