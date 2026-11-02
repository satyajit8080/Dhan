"""AI research API: agents, budget, meetings, recommendations (human approval)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from doi.ai.agents import AGENTS
from doi.ai.repository import list_meetings, list_recommendations, meeting_detail
from doi.ai.service import AIWorker, approve_recommendation, human_request_event, reject_recommendation
from doi.app.security import requires
from doi.core.auth import Principal

router = APIRouter(prefix="/api/v1/ai")


class ProposalIn(BaseModel):
    expiry: str
    strike: float = Field(gt=0)
    option_type: str = Field(pattern="^(CE|PE)$")
    stop_loss: float = Field(gt=0)
    targets: list[tuple[float, float]] = Field(min_length=1, max_length=5)
    valid_for_s: float = Field(default=300, gt=0, le=1800)


class MeetingIn(BaseModel):
    underlying: str
    proposal: ProposalIn | None = None
    run_now: bool = False


class DecisionIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _ai(request: Request):
    rt = request.app.state.runtime
    if getattr(rt, "ai", None) is None:
        raise HTTPException(503, "AI components not configured")
    return rt


@router.get("/agents")
async def agents(who: Principal = Depends(requires("viewer"))):
    return [{"id": a.id, "name": a.name, "responsibility": a.responsibility, "tier": a.tier, "version": a.version,
             "evidence_sections": list(a.sections), "sees_other_turns": a.sees_turns, "scored_on": a.scored_on} for a in AGENTS]  # fmt: skip


@router.get("/budget")
async def budget(request: Request, who: Principal = Depends(requires("viewer"))):
    rt = _ai(request)
    st = await asyncio.to_thread(rt.ai.budget.status)
    r = rt.ai.gateway.routing
    return jsonable_encoder(
        {
            "month": st.month, "hard_limit_inr": st.hard_limit_inr, "usable_inr": st.usable_inr, "spent_inr": st.spent_inr,
            "reserved_inr": st.reserved_inr, "remaining_inr": st.remaining_inr, "overrun_inr": st.overrun_inr, "frozen": st.frozen,
            "frozen_reason": st.frozen_reason, "utilisation": round(st.utilisation, 4), "provider_live": rt.ai.live,
            "routing": {"specialists_downgrade_at": r.downgrade_at, "all_fast_at": r.all_fast_at, "stop_at": r.stop_at,
                        "current_tier_for_cio": r.tier("cio", "reasoning", st.utilisation),
                        "current_tier_for_specialists": r.tier("regime", "reasoning", st.utilisation)},
        }
    )  # fmt: skip


def _usage(dsn: str) -> dict:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=3) as c:
        agents = c.execute(
            "SELECT coalesce(agent_id, '(none)'), count(*), coalesce(sum(prompt_tokens),0), coalesce(sum(cached_tokens),0),"
            " coalesce(sum(completion_tokens),0), coalesce(sum(reasoning_tokens),0), coalesce(sum(cost_inr),0)"
            " FROM llm_calls WHERE created_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata') AT TIME ZONE 'Asia/Kolkata'"
            " GROUP BY 1 ORDER BY 7 DESC"
        ).fetchall()
        errors = c.execute(
            "SELECT created_at, agent_id, model, status, left(coalesce(error,''), 200) FROM llm_calls"
            " WHERE status NOT IN ('OK', 'CACHE_HIT') ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    cols = ("agent_id", "calls", "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "cost_inr")
    return {"by_agent": [dict(zip(cols, r, strict=True)) for r in agents],
            "recent_errors": [dict(zip(("at", "agent_id", "model", "status", "error"), r, strict=True)) for r in errors]}  # fmt: skip


@router.get("/usage")
async def usage(request: Request, who: Principal = Depends(requires("viewer"))):
    """Month-to-date token usage and cost per agent, plus recent non-OK provider calls."""
    rt = _ai(request)
    return jsonable_encoder(await asyncio.to_thread(_usage, rt.ai.budget.dsn))


@router.post("/meetings", status_code=202)
async def request_meeting(request: Request, body: MeetingIn, who: Principal = Depends(requires("approver"))):
    rt = _ai(request)
    key = body.underlying.upper()
    if key not in rt.configs:
        raise HTTPException(404, f"unknown underlying {key}")
    now = datetime.now(UTC)
    ev = human_request_event(key, body.proposal.model_dump() if body.proposal else None, who, now)
    worker = AIWorker(rt)
    event_id = worker.enqueue(ev, None, now)
    out = {"event_id": event_id}
    if body.run_now:
        res = await worker.process_one(datetime.now(UTC))
        out["meeting"] = None if res is None else {"id": res.meeting_id, "status": res.status, "final_action": res.final_action,
                                                   "abort_reason": res.abort_reason, "recommendation_id": res.recommendation_id}  # fmt: skip
    return out


@router.get("/meetings")
async def meetings(request: Request, limit: int = 50, who: Principal = Depends(requires("viewer"))):
    rt = _ai(request)
    return jsonable_encoder(await asyncio.to_thread(list_meetings, rt.ai.store, min(limit, 500)))


@router.get("/meetings/{meeting_id}")
async def meeting(request: Request, meeting_id: str, who: Principal = Depends(requires("viewer"))):
    rt = _ai(request)
    d = await asyncio.to_thread(meeting_detail, rt.ai.store, meeting_id)
    if d is None:
        raise HTTPException(404, "meeting not found")
    return jsonable_encoder(d)


@router.get("/recommendations")
async def recommendations(request: Request, status: str | None = None, who: Principal = Depends(requires("viewer"))):
    rt = _ai(request)
    return jsonable_encoder(await asyncio.to_thread(list_recommendations, rt.ai.store, status))


@router.post("/recommendations/{rec_id}/approve")
async def approve(request: Request, rec_id: str, body: DecisionIn | None = None, who: Principal = Depends(requires("approver"))):
    from doi.app.main import _upstream

    rt = _ai(request)
    try:
        return jsonable_encoder(await approve_recommendation(rt, rec_id, who, _upstream, body.note if body else None))
    except KeyError:
        raise HTTPException(404, "recommendation not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.post("/recommendations/{rec_id}/reject")
async def reject(request: Request, rec_id: str, body: DecisionIn | None = None, who: Principal = Depends(requires("approver"))):
    rt = _ai(request)
    try:
        await reject_recommendation(rt, rec_id, who, body.note if body else None)
    except KeyError:
        raise HTTPException(404, "recommendation not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"rejected": rec_id}


class BudgetIn(BaseModel):
    hard_limit_inr: float = Field(gt=0)
    unfreeze: bool = False
    note: str | None = Field(default=None, max_length=500)


@router.post("/budget")
async def set_budget(request: Request, body: BudgetIn, who: Principal = Depends(requires("admin"))):
    """Lower the cap or unfreeze it. Never above the configured hard limit (₹3,000 by default)."""
    rt = _ai(request)
    ceiling = rt.ai.budget.limit
    if Decimal(str(body.hard_limit_inr)) > ceiling:
        raise HTTPException(422, f"cannot exceed the configured hard limit ₹{ceiling}")
    if not hasattr(rt.ai.budget, "set_limit"):
        raise HTTPException(409, "budget changes need the database-backed budget")
    await asyncio.to_thread(rt.ai.budget.set_limit, Decimal(str(body.hard_limit_inr)), None, unfreeze=body.unfreeze)
    await asyncio.to_thread(rt.approvals.append, who, "BUDGET_CHANGE", "llm_budget", "current_month", body.note,
                            {"hard_limit_inr": body.hard_limit_inr, "unfreeze": body.unfreeze})  # fmt: skip
    return {"ok": True}


@router.get("/leaderboard")
async def ai_leaderboard(request: Request, mode: str = "LIVE", by_version: bool = False, min_n: int = 30,
                         who: Principal = Depends(requires("viewer"))):  # fmt: skip
    rt = _ai(request)
    if rt.paper_store is None:
        raise HTTPException(503, "leaderboard requires DATABASE_URL")
    from doi.ai.scoring import PgScoring

    if mode not in ("LIVE", "REPLAY"):
        raise HTTPException(422, "mode must be LIVE or REPLAY")
    rows = await asyncio.to_thread(PgScoring(rt.paper_store.dsn).leaderboard, mode, min_n=max(1, min_n), by_version=by_version)
    names = {a.id: a.name for a in AGENTS}
    for r in rows:
        r["name"] = names.get(r["agent_id"], r["agent_id"])
    return jsonable_encoder({"mode": mode, "min_n_for_rank": min_n, "agents": rows,
                             "unscored_agents": sorted(set(names) - {r["agent_id"] for r in rows})})  # fmt: skip


@router.post("/scoring/run")
async def run_scoring(request: Request, who: Principal = Depends(requires("admin"))):
    rt = _ai(request)
    if rt.paper_store is None:
        raise HTTPException(503, "scoring requires DATABASE_URL")
    from doi.ai.scoring import PgScoring

    sc = PgScoring(rt.paper_store.dsn)
    now = datetime.now(UTC)
    ids = {k: v.security_id for k, v in rt.universe.items()}
    resolved = await asyncio.to_thread(sc.resolve_due, now, ids)
    snap = await asyncio.to_thread(sc.snapshot, now, "LIVE")
    return {"resolved": resolved, "scorecards_written": snap}
