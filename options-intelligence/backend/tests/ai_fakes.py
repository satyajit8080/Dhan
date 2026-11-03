"""Deterministic offline stand-ins for Grok responses (tests and documented examples).

These are SIMULATED responses built only from the evidence in the prompt. They
exercise the full pipeline (validation, budgeting, meetings, recording) and say
nothing about real model quality."""

from __future__ import annotations

import json
import re

from doi.ai.agents import agent_by_role_name

_EVID = re.compile(r"<evidence>\n(.*?)\n</evidence>", re.S)


def _evidence(messages) -> dict:
    m = _EVID.search(messages[-1]["content"])
    return json.loads(m.group(1)) if m else {}


def _first_ref(ev: dict, prefer: tuple[str, ...]) -> tuple[str, object]:
    for sec in prefer + tuple(ev):
        body = ev.get(sec)
        if isinstance(body, dict):
            for k, v in body.items():
                if isinstance(v, int | float | str) and not isinstance(v, bool):
                    return f"{sec}.{k}", v
    return "event", ""


def claim(ev, prefer, text_fmt="{ref} is {val}"):
    ref, val = _first_ref(ev, prefer)
    return {"text": text_fmt.format(ref=ref, val=val), "evidence_refs": [ref]}


def scripted(overrides: dict | None = None):
    """overrides: agent_id -> dict (merged into the default reply) or callable(ev) -> dict | str."""
    overrides = overrides or {}

    def respond(model, messages):
        spec = agent_by_role_name(messages[0]["content"])
        ev = _evidence(messages)
        aid = spec.id if spec else "?"
        o = overrides.get(aid)
        if callable(o):
            r = o(ev)
            return r if isinstance(r, str) else json.dumps(r)
        prefer = spec.sections[3:] if spec else ()
        base = {
            "Opinion": {"stance": "BULL", "confidence": 3, "claims": [claim(ev, prefer)], "risks": [],
                        "prediction": {"target": "INDEX", "direction": "UP", "horizon_min": 30}},
            "RiskOpinion": {"stance": "NEUTRAL", "confidence": 3, "claims": [claim(ev, ("risk",))], "risks": [], "veto": False},
            "Challenge": {"veto": False, "confidence": 2, "flaws": [claim(ev, ("liquidity", "volatility"))], "kill_criteria": [claim(ev, ("oi",))],
                          "prediction": {"target": "INDEX", "direction": "FLAT", "horizon_min": 30}},
            "Decision": {"action": "RECOMMEND_PAPER", "confidence": 3, "rationale": [claim(ev, ("regime", "oi"))], "dissent_addressed": [],
                         "prediction": {"target": "INDEX", "direction": "UP", "horizon_min": 30}},
            "ComplianceCheck": {"passed": True, "violations": [], "claims": [claim(ev, ("risk", "proposal"))]},
            "Review": {"verdict": "PASS", "confidence": 3, "claims": [claim(ev, ("research", "performance", "quality"))]},
            "Rebuttal": {"maintain": True, "confidence": 3, "claims": [claim(ev, prefer)]},
        }  # fmt: skip
        schema = re.search(r"JSON schema of your reply:\n(.*)$", messages[0]["content"], re.S)
        title = json.loads(schema.group(1)).get("title") if schema else "Opinion"
        reply = dict(base.get(title, base["Opinion"]))
        if aid in ("macro_events", "breadth"):  # their sources are unverified/unconfigured: abstain, as instructed
            reply.update(stance="ABSTAIN", confidence=1, prediction=None)
        if isinstance(o, dict):
            reply.update(o)
        return json.dumps(reply)

    return respond
