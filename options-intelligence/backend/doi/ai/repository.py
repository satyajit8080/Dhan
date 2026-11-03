"""Read side of meeting memory (dashboard and API). Works on both stores."""

from __future__ import annotations

import psycopg

from doi.ai.meetings import MemoryMeetingStore, PgMeetingStore


def _rows(dsn: str, sql: str, args: tuple = ()) -> list[dict]:
    with psycopg.connect(dsn) as c:
        cur = c.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def list_meetings(store, limit: int = 50) -> list[dict]:
    if isinstance(store, PgMeetingStore):
        return _rows(
            store.dsn,
            "SELECT id, event_id, event->>'kind' AS kind, status, final_action, abort_reason, started_at, ended_at, cost_inr, llm_calls"
            " FROM ai_meetings ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
    ms = sorted(store.meetings.values(), key=lambda m: m["started_at"], reverse=True)[:limit]
    return [
        {k: m.get(k) for k in ("id", "status", "final_action", "abort_reason", "started_at", "cost_inr")}
        | {"kind": m["event"].get("kind")}
        for m in ms
    ]


def meeting_detail(store, mid: str) -> dict | None:
    if isinstance(store, PgMeetingStore):
        m = _rows(store.dsn, "SELECT * FROM ai_meetings WHERE id = %s", (mid,))
        if not m:
            return None
        return {
            "meeting": m[0],
            "turns": _rows(
                store.dsn,
                "SELECT seq, round, agent_id, agent_version, status, stance, confidence, output, errors, visible_sections,"
                " cost_inr, model FROM ai_meeting_turns WHERE meeting_id = %s ORDER BY seq",
                (mid,),
            ),
            "disagreements": _rows(store.dsn, "SELECT * FROM ai_disagreements WHERE meeting_id = %s ORDER BY seq", (mid,)),
            "decision": next(iter(_rows(store.dsn, "SELECT * FROM ai_decisions WHERE meeting_id = %s", (mid,))), None),
            "recommendation": next(
                iter(_rows(store.dsn, "SELECT * FROM ai_recommendations WHERE meeting_id = %s", (mid,))), None
            ),
            "predictions": _rows(store.dsn, "SELECT * FROM ai_predictions WHERE meeting_id = %s ORDER BY agent_id", (mid,)),
            "evidence": next(
                iter(
                    _rows(
                        store.dsn,
                        "SELECT p.* FROM ai_evidence_packs p JOIN ai_meetings m ON m.evidence_pack_id = p.id WHERE m.id = %s",
                        (mid,),
                    )
                ),
                None,
            ),
        }
    assert isinstance(store, MemoryMeetingStore)
    if mid not in store.meetings:
        return None
    m = store.meetings[mid]
    return {
        "meeting": m,
        "turns": [
            {
                "round": r,
                "agent_id": t.agent_id,
                "status": t.status,
                "stance": t.stance(),
                "confidence": t.confidence(),
                "output": t.output.model_dump(mode="json") if t.output else None,
                "errors": list(t.errors),
            }
            for r, t in store.turn_rows[mid]
        ],
        "disagreements": store.disagreements.get(mid, []),
        "decision": store.decisions.get(mid),
        "recommendation": next((r for r in store.recommendations.values() if r["meeting_id"] == mid), None),
        "predictions": [p for p in store.predictions if p["meeting_id"] == mid],
        "evidence": store.packs.get(m.get("pack")),
    }


def list_strong_calls(store) -> list[dict]:
    """Recommendations the committee has already verified as strong paper buys."""
    from doi.ai.committee_view import strong_call, summarize

    out = []
    for rec in list_recommendations(store):
        detail = meeting_detail(store, rec.get("meeting_id"))
        if not detail or not detail.get("recommendation"):
            continue
        row = dict(detail["recommendation"])
        row.setdefault("status", rec.get("status") or "PENDING_HUMAN")
        detail = {**detail, "recommendation": row, "summary": summarize(detail)}
        call = strong_call(detail)
        if call:
            out.append(call)
    return out


def list_recommendations(store, status: str | None = None) -> list[dict]:
    if isinstance(store, PgMeetingStore):
        q, a = "SELECT * FROM ai_recommendations", ()
        if status:
            q, a = q + " WHERE status = %s", (status,)
        return _rows(store.dsn, q + " ORDER BY created_at DESC LIMIT 200", a)
    rs = [{"status": "PENDING_HUMAN", **r} for r in store.recommendations.values()]
    return [r for r in rs if status is None or r["status"] == status]
