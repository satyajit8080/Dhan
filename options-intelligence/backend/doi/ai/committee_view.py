"""Display summary of a stored meeting (deterministic; no LLM). The frontend only renders this."""

from __future__ import annotations

from collections import Counter

from doi.ai.agents import AGENTS

_DIR = {"BULL": "UP", "BEAR": "DOWN", "NEUTRAL": "FLAT"}


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
