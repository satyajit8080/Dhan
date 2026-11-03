"""Display summary of a stored meeting (deterministic; no LLM). The frontend only renders this."""

from __future__ import annotations

from collections import Counter

from doi.ai.agents import AGENTS

_DIR = {"BULL": "UP", "BEAR": "DOWN", "NEUTRAL": "FLAT"}


# A strong call is a paper BUY the committee has already filtered. Confidence below this
# stays a plain bullish/bearish label and never surfaces as a trade signal.
STRONG_CONFIDENCE = 4


def strong_call(d: dict) -> dict | None:
    """One committee-verified strong paper call, or None.

    The meeting must have finished as RECOMMEND_PAPER with CIO confidence at least
    STRONG_CONFIDENCE, no risk, contrarian, or compliance veto, and a risk-gate
    approval. CE is included only for an up view and PE only for a down view.
    The paper engine buys the option; this does not create an order.
    """
    summary = d.get("summary") or summarize(d)
    rec = d.get("recommendation")
    if not rec or summary.get("final_action") != "RECOMMEND_PAPER":
        return None
    if summary.get("label") not in ("STRONG BULLISH", "STRONG BEARISH"):
        return None
    conf = summary.get("cio_confidence")
    if not isinstance(conf, int) or isinstance(conf, bool) or conf < STRONG_CONFIDENCE:
        return None
    vetoes = summary.get("vetoes") or {}
    if vetoes.get("risk_officer") or vetoes.get("contrarian") or vetoes.get("compliance_failed"):
        return None
    if (summary.get("size") or {}).get("risk_gate_approved") is not True:
        return None
    direction, opt = summary.get("direction"), str(rec.get("option_type") or "").upper()
    if not ((direction == "UP" and opt == "CE") or (direction == "DOWN" and opt == "PE")):
        return None
    meeting = d.get("meeting") or {}
    return {
        "id": rec.get("id"),
        "underlying": rec.get("underlying_key"),
        "underlying_key": rec.get("underlying_key"),
        "option_type": opt,
        "side": "BUY",
        "signal": f"BUY {opt}",
        "confidence": conf,
        "label": summary["label"],
        "meeting_id": rec.get("meeting_id") or meeting.get("id"),
        "recommendation_id": rec.get("id"),
        "status": rec.get("status") or "PENDING_HUMAN",
        "strike": rec.get("strike"),
        "expiry": rec.get("expiry"),
        "valid_until": rec.get("valid_until"),
    }


def summarize(d: dict) -> dict:
    latest: dict[str, dict] = {}
    for t in sorted(d.get("turns") or [], key=lambda x: x.get("seq") or 0):
        latest[t["agent_id"]] = t
    out = lambda a: (latest.get(a) or {}).get("output") or {}
    stances = Counter(t["stance"] for t in latest.values() if t.get("status") == "OK" and t.get("stance") in _DIR)
    dec = d.get("decision") or {}
    final = dec.get("final_action") or (d.get("meeting") or {}).get("final_action")
    cio = out("cio")
    direction = (cio.get("prediction") or {}).get("direction")
    if direction is None and stances:
        top = stances.most_common()
        direction = _DIR[top[0][0]] if len(top) == 1 or top[0][1] > top[1][1] else "FLAT"
    conf = cio.get("confidence")
    if final not in ("RECOMMEND_PAPER", "WATCH") or direction is None:
        label = "WAIT"
    elif direction == "FLAT":
        label = "NEUTRAL"
    else:
        label = ("STRONG " if (conf or 0) >= 4 else "") + ("BULLISH" if direction == "UP" else "BEARISH")
    agree = sum(1 for t in latest.values() if t.get("status") == "OK" and _DIR.get(t.get("stance") or "") == direction)
    dry = dec.get("risk_dry_run") or {}
    comp = dec.get("compliance") or out("compliance")
    return {
        "label": label,
        "final_action": final,
        "cio_confidence": conf,
        "direction": direction,
        "consensus": agree,
        "panel_size": len(AGENTS),
        "stance_counts": dict(stances),
        "vetoes": {
            "risk_officer": bool(out("risk_officer").get("veto")),
            "contrarian": bool(out("contrarian").get("veto")),
            "compliance_failed": comp.get("passed") is False,
        },
        "size": {"lots": dry.get("lots"), "quantity": dry.get("quantity"), "risk_gate_approved": dry.get("approved")},
        "agents": [
            {"id": a.id, "name": a.name, "status": (latest.get(a.id) or {}).get("status", "IDLE"),
             "stance": (latest.get(a.id) or {}).get("stance"), "confidence": (latest.get(a.id) or {}).get("confidence"),
             "disagrees": direction is not None and _DIR.get((latest.get(a.id) or {}).get("stance") or "") not in (None, direction)}
            for a in AGENTS
        ],
    }  # fmt: skip
