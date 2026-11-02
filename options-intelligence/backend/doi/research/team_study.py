"""Team-size study and judgement scorecard (Phase 8, priority 5).

Compares single-agent, three-agent and full-team research on the same contexts.
The scorecard follows the project's judgement criteria rather than signal count:
  1. regime recognition: on REGIME_SHIFT contexts, did the regime specialist's stance follow the shift?
  2. rejecting weak opportunities: of WATCH/NO_ACTION decisions, the share whose CIO view would have missed;
  3. explanation: every recorded claim cites validated evidence (counted, not assumed);
  4. calibration: Brier score and per-conviction hit rates of the CIO's call;
  5. loss avoidance: the worst outcome among RECOMMEND_PAPER decisions;
  6. after costs: outcome-direction return of recommendations, and LLM cost per useful recommendation.
Outcomes come from a Python `outcome_fn` over recorded data: the index value at the horizon.
The study never trades. Results are labelled with their data class.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from doi.ai.meetings import QUORUM, SPECIALISTS, MeetingContext, MeetingEngine, MemoryMeetingStore
from doi.ai.scoring import CONF_PROB, realized_direction


@dataclass(frozen=True)
class Team:
    name: str
    specialists: tuple[str, ...]
    quorum: frozenset[str]
    contrarian: bool


TEAMS = (
    Team("single", ("regime",), frozenset({"regime"}), False),
    Team("trio", ("regime", "options_flow", "volatility"), frozenset({"regime", "options_flow", "volatility"}), True),
    Team("full", SPECIALISTS, QUORUM, True),
)


@dataclass(frozen=True)
class StudyCase:
    ctx: MeetingContext
    now: datetime
    ref_value: float  # index value at decision time (Python)
    regime_shift_to: str | None = None  # set for REGIME_SHIFT contexts


async def run_study(
    gateway_factory: Callable[[], object],
    cases: Sequence[StudyCase],
    outcome_fn: Callable[[StudyCase, int], float | None],
    *,
    teams: Sequence[Team] = TEAMS,
    data_class: str,
    flat_band: float = 0.0005,
) -> dict:
    if data_class not in ("HISTORICAL", "SIMULATED", "LIVE_RECORDED"):
        raise ValueError("data_class must be HISTORICAL, SIMULATED or LIVE_RECORDED")
    report = {"data_class": data_class, "cases": len(cases), "teams": {}}
    for team in teams:
        gw = gateway_factory()
        eng = MeetingEngine(
            gw, MemoryMeetingStore(), specialists=team.specialists, quorum=team.quorum, use_contrarian=team.contrarian
        )
        rows = []
        for case in cases:
            r = await eng.run(case.ctx, case.now)
            cio = next((t for t in r.turns if t.agent_id == "cio" and t.output is not None), None)
            pred = getattr(cio.output, "prediction", None) if cio else None
            regime = next((t for t in r.turns if t.agent_id == "regime" and t.output is not None), None)
            row = {"action": r.final_action, "aborted": r.status == "ABORTED", "cost_inr": r.cost_inr,
                   "claims": sum(len(getattr(t.output, k, []) or []) for t in r.turns if t.output for k in ("claims", "flaws", "rationale"))}  # fmt: skip
            if pred is not None:
                realized = outcome_fn(case, pred.horizon_min)
                if realized is not None:
                    ret = (realized - case.ref_value) / case.ref_value
                    hit = realized_direction(ret, flat_band) == pred.direction
                    sign = {"UP": 1, "DOWN": -1, "FLAT": 0}[pred.direction]
                    row |= {
                        "hit": hit,
                        "conf": cio.output.confidence,
                        "brier": (CONF_PROB[cio.output.confidence] - hit) ** 2,
                        "dir_return": sign * ret,
                    }
            if case.regime_shift_to and regime is not None:
                row["regime_followed"] = (case.regime_shift_to.startswith("TREND_UP") and regime.output.stance == "BULL") or (
                    case.regime_shift_to.startswith("TREND_DOWN") and regime.output.stance == "BEAR") or (
                    case.regime_shift_to == "RANGE" and regime.output.stance == "NEUTRAL")  # fmt: skip
            rows.append(row)
        report["teams"][team.name] = scorecard(rows, team)
    return report


def scorecard(rows: list[dict], team: Team) -> dict:
    scored = [r for r in rows if "hit" in r]
    recs = [r for r in rows if r["action"] == "RECOMMEND_PAPER"]
    recs_scored = [r for r in recs if "hit" in r]
    rejects = [r for r in rows if r["action"] in ("WATCH", "NO_ACTION") and "hit" in r]
    useful = [r for r in recs_scored if r["hit"]]
    cost = sum(r["cost_inr"] for r in rows)
    reg = [r["regime_followed"] for r in rows if "regime_followed" in r]
    calib = {}
    for c in range(1, 6):
        b = [r for r in scored if r["conf"] == c]
        if b:
            calib[str(c)] = {"n": len(b), "hit_rate": round(sum(r["hit"] for r in b) / len(b), 4)}
    return {
        "team": {"specialists": list(team.specialists), "contrarian": team.contrarian},
        "meetings": len(rows),
        "aborted": sum(r["aborted"] for r in rows),
        "recommendations": len(recs),
        "regime_recognition_rate": round(sum(reg) / len(reg), 4) if reg else None,
        "good_rejection_rate": round(sum(1 for r in rejects if not r["hit"]) / len(rejects), 4) if rejects else None,
        "evidence_cited_claims": sum(r["claims"] for r in rows),
        "cio_hit_rate": round(sum(r["hit"] for r in scored) / len(scored), 4) if scored else None,
        "cio_brier": round(statistics.mean(r["brier"] for r in scored), 6) if scored else None,
        "calibration": calib,
        "worst_recommendation_return": round(min(r["dir_return"] for r in recs_scored), 6) if recs_scored else None,
        "recommendation_mean_return": round(statistics.mean(r["dir_return"] for r in recs_scored), 6) if recs_scored else None,
        "llm_cost_inr": round(cost, 4),
        "cost_per_useful_recommendation_inr": round(cost / len(useful), 4) if useful else None,
        "useful_recommendations": len(useful),
        "note": "index-direction outcomes; trade P&L after charges comes from research_option_backtests",
    }
