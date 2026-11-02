"""AI meeting engine: independent specialists, contrarian challenge, rebuttals,
risk veto, CIO synthesis, compliance check, and permanent recording.

Authority order (Python, never the model):
  1. The data-quality gate can abort a meeting before any LLM call.
  2. The deterministic RiskGate dry run and the AI Risk Officer's veto are applied
     by Python AFTER the CIO speaks. The CIO can't override either one.
  3. A contrarian veto stands unless the CIO gives an explicit evidence-based rebuttal.
  4. A failed compliance check downgrades the decision to WATCH.
  5. RECOMMEND_PAPER only creates a PENDING_HUMAN recommendation. A human approves,
     then the live RiskGate re-check runs, then the paper engine.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from doi.ai.agents import REGISTRY, AgentTurn, Rebuttal, run_agent
from doi.ai.evidence import EvidencePack, build_pack
from doi.core.canonical import canonical
from doi.intelligence.feature_engine.engine import FeatureVector
from doi.llm.gateway import LLMGateway

SPECIALISTS = ("regime", "technical", "options_flow", "volatility", "liquidity", "macro_events", "breadth")
QUORUM = frozenset({"regime", "options_flow", "volatility", "liquidity"})


@dataclass(frozen=True)
class MeetingContext:
    event: dict
    underlying_key: str | None
    features: FeatureVector | None
    proposal: dict | None = None  # {"expiry","strike","option_type","stop_loss","targets":[[p,f],...],"valid_for_s"}
    risk_dry_run: dict | None = None  # deterministic RiskGate output on the proposal
    portfolio: dict | None = None
    calendar: dict | None = None
    breadth: dict | None = None
    performance: dict | None = None
    event_id: str | None = None
    mode: str = "LIVE"  # LIVE | REPLAY (replay predictions are scored separately)
    min_quality: float = 80.0


@dataclass
class MeetingResult:
    meeting_id: str
    status: str  # DECIDED | ABORTED
    final_action: str
    abort_reason: str | None
    turns: list[AgentTurn]
    overrides: list[str]
    disagreements: list[dict]
    recommendation_id: str | None
    prediction_ids: list[str]
    cost_inr: float
    pack: EvidencePack | None


def _j(x: Any) -> Any:
    """Plain JSON (datetimes, Decimals, enums, dataclasses) for jsonb columns."""
    return json.loads(canonical(x))


def _override(overrides: list[str], action: str, reason: str) -> str:
    overrides.append(reason)
    return action


def turn_summary(t: AgentTurn, rnd: str) -> dict:
    return {
        "agent": t.agent_id,
        "round": rnd,
        "status": t.status,
        "output": t.output.model_dump(mode="json") if t.output else None,
    }


class MeetingStore(Protocol):
    def save_pack(self, pack: EvidencePack) -> None: ...
    def open(self, mid: str, ctx: MeetingContext, pack_id: str | None, now: datetime) -> None: ...
    def turns(self, mid: str) -> dict[tuple[str, str], AgentTurn]: ...
    def save_turn(self, mid: str, rnd: str, t: AgentTurn) -> None: ...
    def save_disagreement(self, mid: str, d: dict) -> None: ...
    def save_decision(self, mid: str, d: dict) -> None: ...
    def save_recommendation(self, rec: dict) -> None: ...
    def save_prediction(self, p: dict) -> None: ...
    def close(self, mid: str, status: str, action: str, reason: str | None, cost: float, calls: int, now: datetime) -> None: ...


class MemoryMeetingStore:
    def __init__(self) -> None:
        self.packs: dict[str, dict] = {}
        self.meetings: dict[str, dict] = {}
        self.turn_rows: dict[str, list[tuple[str, AgentTurn]]] = {}
        self.disagreements: dict[str, list[dict]] = {}
        self.decisions: dict[str, dict] = {}
        self.recommendations: dict[str, dict] = {}
        self.predictions: list[dict] = []
        self._lock = threading.Lock()

    def save_pack(self, pack):
        self.packs[pack.id] = pack.to_dict()

    def open(self, mid, ctx, pack_id, now):
        self.meetings.setdefault(mid, {"id": mid, "event": ctx.event, "pack": pack_id, "status": "OPEN", "started_at": now})
        self.turn_rows.setdefault(mid, [])

    def turns(self, mid):
        return {(r, t.agent_id): t for r, t in self.turn_rows.get(mid, [])}

    def save_turn(self, mid, rnd, t):
        with self._lock:
            self.turn_rows[mid].append((rnd, t))

    def save_disagreement(self, mid, d):
        self.disagreements.setdefault(mid, []).append(d)

    def save_decision(self, mid, d):
        self.decisions[mid] = d

    def save_recommendation(self, rec):
        self.recommendations[rec["id"]] = rec

    def save_prediction(self, p):
        self.predictions.append(p)

    def get_recommendation(self, rid):
        r = self.recommendations.get(rid)
        return None if r is None else {"status": "PENDING_HUMAN", **r}

    def update_recommendation(self, rid, status, who, result, now):
        r = self.recommendations[rid]
        if r.get("status", "PENDING_HUMAN") not in ("PENDING_HUMAN", "APPROVED"):
            raise ValueError(f"recommendation {rid} is {r.get('status')}")
        r.update(status=status, decided_by=who, decided_at=now, paper_result=result)

    def close(self, mid, status, action, reason, cost, calls, now):
        self.meetings[mid].update(
            status=status, final_action=action, abort_reason=reason, cost_inr=cost, llm_calls=calls, ended_at=now
        )


class PgMeetingStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _x(self, sql: str, args: tuple) -> None:
        with psycopg.connect(self.dsn) as c:
            c.execute(sql, args)

    def save_pack(self, pack):
        self._x(
            "INSERT INTO ai_evidence_packs (id, sha256, as_of, underlying_key, content) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (pack.id, pack.sha256, pack.as_of, pack.underlying_key, Jsonb(_j(pack.to_dict()))),
        )

    def open(self, mid, ctx, pack_id, now):
        self._x(
            "INSERT INTO ai_meetings (id, event_id, event, evidence_pack_id, status, started_at) VALUES (%s, %s, %s, %s, 'OPEN', %s)"
            " ON CONFLICT (id) DO NOTHING",
            (mid, ctx.event_id, Jsonb(_j(ctx.event)), pack_id, now),
        )

    def turns(self, mid):
        from doi.ai.agents import REGISTRY as R

        out = {}
        with psycopg.connect(self.dsn) as c:
            for rnd, aid, ver, status, output, errors, vis, calls, cost, model in c.execute(
                "SELECT round, agent_id, agent_version, status, output, errors, visible_sections, call_ids, cost_inr, model"
                " FROM ai_meeting_turns WHERE meeting_id = %s ORDER BY seq",
                (mid,),
            ):
                spec = R[aid]
                schema = Rebuttal if rnd.split("#")[0] == "R2" else spec.schema
                o = schema.model_validate(output) if output else None
                out[(rnd, aid)] = AgentTurn(aid, ver, status, o, tuple(errors), list(calls), float(cost), model, None, tuple(vis))
        return out

    def save_turn(self, mid, rnd, t):
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mid,))
            seq = c.execute("SELECT COALESCE(max(seq), 0) + 1 FROM ai_meeting_turns WHERE meeting_id = %s", (mid,)).fetchone()[0]
            c.execute(
                "INSERT INTO ai_meeting_turns (meeting_id, seq, round, agent_id, agent_version, status, stance, confidence, output, errors,"
                " visible_sections, call_ids, cost_inr, model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (mid, seq, rnd, t.agent_id, t.agent_version, t.status, t.stance(), t.confidence(),
                 Jsonb(t.output.model_dump(mode="json")) if t.output else None, Jsonb(list(t.errors)), list(t.visible_sections),
                 t.call_ids, Decimal(str(t.cost_inr)), t.model),
            )  # fmt: skip

    def save_disagreement(self, mid, d):
        with psycopg.connect(self.dsn) as c, c.transaction():
            seq = c.execute("SELECT COALESCE(max(seq), 0) + 1 FROM ai_disagreements WHERE meeting_id = %s", (mid,)).fetchone()[0]
            c.execute(
                "INSERT INTO ai_disagreements (meeting_id, seq, topic, agents, positions, resolution) VALUES (%s, %s, %s, %s, %s, %s)",
                (mid, seq, d["topic"], d["agents"], Jsonb(_j(d["positions"])), d.get("resolution")),
            )

    def save_decision(self, mid, d):
        self._x(
            "INSERT INTO ai_decisions (meeting_id, cio_action, final_action, overrides, rationale, dissent, risk_dry_run, compliance)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (mid, d["cio_action"], d["final_action"], Jsonb(_j(d["overrides"])), Jsonb(_j(d["rationale"])), Jsonb(_j(d["dissent"])),
             Jsonb(_j(d["risk_dry_run"])), Jsonb(_j(d["compliance"]))),
        )  # fmt: skip

    def save_recommendation(self, r):
        self._x(
            "INSERT INTO ai_recommendations (id, meeting_id, underlying_key, expiry, strike, option_type, stop_loss, targets, valid_until, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING_HUMAN') ON CONFLICT DO NOTHING",
            (r["id"], r["meeting_id"], r["underlying_key"], r["expiry"], r["strike"], r["option_type"], r["stop_loss"], Jsonb(_j(r["targets"])),
             r["valid_until"]),
        )  # fmt: skip

    def save_prediction(self, p):
        self._x(
            "INSERT INTO ai_predictions (id, agent_id, agent_version, meeting_id, underlying_key, target, direction, confidence, horizon_min,"
            " ref_value, created_at, resolve_at, mode) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (p["id"], p["agent_id"], p["agent_version"], p["meeting_id"], p["underlying_key"], p["target"], p["direction"], p["confidence"],
             p["horizon_min"], p["ref_value"], p["created_at"], p["resolve_at"], p["mode"]),
        )  # fmt: skip

    def get_recommendation(self, rid):
        cols = ("id", "meeting_id", "underlying_key", "expiry", "strike", "option_type", "stop_loss", "targets", "valid_until", "status",
                "decided_by", "decided_at", "paper_result")  # fmt: skip
        with psycopg.connect(self.dsn) as c:
            r = c.execute(f"SELECT {', '.join(cols)} FROM ai_recommendations WHERE id = %s", (rid,)).fetchone()  # nosec B608 - constant column list, values bound as parameters
        return dict(zip(cols, r, strict=True)) if r else None

    def update_recommendation(self, rid, status, who, result, now):
        with psycopg.connect(self.dsn) as c:
            n = c.execute(
                "UPDATE ai_recommendations SET status = %s, decided_by = %s, decided_at = %s, paper_result = %s"
                " WHERE id = %s AND status IN ('PENDING_HUMAN', 'APPROVED')",
                (status, who, now, Jsonb(_j(result)), rid),
            ).rowcount
        if n != 1:
            raise ValueError(f"recommendation {rid} is not pending")

    def close(self, mid, status, action, reason, cost, calls, now):
        self._x(
            "UPDATE ai_meetings SET status = %s, final_action = %s, abort_reason = %s, cost_inr = %s, llm_calls = %s, ended_at = %s WHERE id = %s",
            (status, action, reason, Decimal(str(cost)), calls, now, mid),
        )


@dataclass
class MeetingEngine:
    gateway: LLMGateway
    store: Any = field(default_factory=MemoryMeetingStore)
    disagreement_min_confidence: int = 3
    specialists: tuple[str, ...] = SPECIALISTS  # team composition (Phase 8 team-size studies)
    quorum: frozenset[str] = QUORUM
    use_contrarian: bool = True

    async def _turn(self, mid, rnd, aid, pack, done, now, *, task, turns=None, schema=None, turns_visible=None) -> AgentTurn:
        # resume: a recorded successful turn is never re-run or re-billed; failed ones are retried
        attempts = [k for k in done if k[1] == aid and (k[0] == rnd or k[0].startswith(rnd + "#"))]
        for k in attempts:
            if done[k].status == "OK":
                return done[k]
        label = rnd if not attempts else f"{rnd}#{len(attempts)}"
        t = await run_agent(REGISTRY[aid], pack, self.gateway, task=task, turns=turns, meeting_id=mid, now=now, schema=schema,
                            turns_visible=turns_visible)  # fmt: skip
        await asyncio.to_thread(self.store.save_turn, mid, label, t)
        done[(label, aid)] = t
        return t

    async def run(self, ctx: MeetingContext, now: datetime, meeting_id: str | None = None) -> MeetingResult:
        mid = meeting_id or ("MTG-" + secrets.token_hex(6))
        turns: list[AgentTurn] = []
        overrides: list[str] = []

        # 1. deterministic data-quality gate (no LLM call)
        f = ctx.features
        q_ok = f is not None and f.coverage_status == "LIVE_OK" and (f.data_quality_score or 0) >= ctx.min_quality
        pack = None
        if q_ok:
            extra: dict[str, dict] = {}
            for name in ("proposal", "portfolio", "calendar", "breadth", "performance"):
                v = getattr(ctx, name)
                if v is not None:
                    extra[name] = v
            if ctx.risk_dry_run is not None:
                extra["risk"] = ctx.risk_dry_run
            extra.setdefault("calendar", {"status": "UNVERIFIED_SOURCE", "events": []})
            extra.setdefault("breadth", {"status": "NO_SOURCE_CONFIGURED"})
            pack = build_pack(as_of=now, underlying_key=ctx.underlying_key, event=ctx.event, features=f, extra=extra)
            await asyncio.to_thread(self.store.save_pack, pack)
        await asyncio.to_thread(self.store.open, mid, ctx, pack.id if pack else None, now)
        if not q_ok:
            return await self._abort(mid, "DATA_QUALITY", turns, overrides, now, pack)
        done = await asyncio.to_thread(self.store.turns, mid)

        # 2. Round 1: independent specialists (separate evidence views, no access to each other)
        task = f"Event {ctx.event.get('kind')} on {ctx.underlying_key}. Give your independent assessment."
        r1 = await asyncio.gather(*(self._turn(mid, "R1", a, pack, done, now, task=task) for a in self.specialists))
        turns += r1
        ok = {t.agent_id for t in r1 if t.status == "OK" and t.stance() != "ABSTAIN"}
        if not self.quorum <= ok:
            unavailable = any(t.status == "UNAVAILABLE" for t in r1)
            return await self._abort(mid, "AI_UNAVAILABLE" if unavailable else "QUORUM", turns, overrides, now, pack)
        summaries = [turn_summary(t, "R1") for t in r1]

        # 3. contrarian: independent challenge (sees the evidence and Round 1, never the CIO)
        if self.use_contrarian:
            con = await self._turn(
                mid,
                "CONTRARIAN",
                "contrarian",
                pack,
                done,
                now,
                task="Challenge the proposal and the specialists' consensus.",
                turns=summaries,
            )
            turns.append(con)
        else:  # study configuration without a contrarian: recorded as such, never silently
            con = AgentTurn("contrarian", "disabled", "ABSTAIN", None, ("contrarian disabled for this team configuration",))
        con_s = [turn_summary(con, "CONTRARIAN")] if self.use_contrarian else []

        # 4. disagreements, then a rebuttal round only when there is real conflict
        disagreements = []
        strong = [t for t in r1 if t.output is not None and t.confidence() and t.confidence() >= self.disagreement_min_confidence]
        bulls = [t for t in strong if t.stance() == "BULL"]
        bears = [t for t in strong if t.stance() == "BEAR"]
        if bulls and bears:
            d = {"topic": "direction", "agents": sorted(t.agent_id for t in bulls + bears),
                 "positions": {t.agent_id: {"stance": t.stance(), "confidence": t.confidence()} for t in bulls + bears}}  # fmt: skip
            disagreements.append(d)
        if con.output is not None and getattr(con.output, "veto", False):
            disagreements.append(
                {"topic": "contrarian_veto", "agents": ["contrarian"], "positions": {"contrarian": {"veto": True}}}
            )
        r2: list[AgentTurn] = []
        if any(d["topic"] == "direction" for d in disagreements):
            opp = summaries + con_s
            involved = disagreements[0]["agents"]
            r2 = await asyncio.gather(*(
                self._turn(mid, "R2", a, pack, done, now, task="Respond to the opposing views. Maintain or revise your stance.", turns=opp,
                           schema=Rebuttal, turns_visible=True)
                for a in involved
            ))  # fmt: skip
            turns += r2

        # 5. risk officer: independent of the CIO; its veto is applied by Python
        risk_turns = summaries + con_s
        risk = await self._turn(
            mid,
            "RISK",
            "risk_officer",
            pack,
            done,
            now,
            task="Assess risk; veto if material.",
            turns=risk_turns,
            turns_visible=False,
        )
        turns.append(risk)

        # 6. CIO synthesis
        cio_view = summaries + con_s + [turn_summary(t, "R2") for t in r2] + [turn_summary(risk, "RISK")]
        cio = await self._turn(
            mid, "CIO", "cio", pack, done, now, task="Decide: RECOMMEND_PAPER, WATCH, NO_ACTION or ESCALATE.", turns=cio_view
        )
        turns.append(cio)
        if cio.output is None:
            return await self._abort(mid, "CIO_UNAVAILABLE", turns, overrides, now, pack, disagreements)

        # 7. deterministic authority over the CIO
        action = cio.output.action
        dry = ctx.risk_dry_run or {}
        if action == "RECOMMEND_PAPER" and ctx.proposal is None:
            action = _override(overrides, "WATCH", "no Python proposal: cannot recommend a trade")
        if action == "RECOMMEND_PAPER" and not dry.get("approved", False):
            action = _override(overrides, "NO_ACTION", "deterministic RiskGate dry run did not approve: risk veto")
        if action == "RECOMMEND_PAPER" and (risk.output is None or getattr(risk.output, "veto", True)):
            action = _override(overrides, "NO_ACTION", "AI Risk Officer veto or unavailable: risk veto")
        if (
            action == "RECOMMEND_PAPER"
            and con.output is not None
            and con.output.veto
            and cio.output.rebuttal_to_contrarian is None
        ):
            action = _override(overrides, "WATCH", "contrarian veto without CIO rebuttal")
        for d in disagreements:
            d["resolution"] = f"CIO action {cio.output.action}; final {action}"

        # 8. compliance check of the (already constrained) decision
        comp_view = [turn_summary(cio, "CIO"), {"final_action_after_python_rules": action, "overrides": overrides}]
        comp = await self._turn(
            mid, "COMPLIANCE", "compliance", pack, done, now, task="Check this decision against policy.", turns=comp_view
        )
        turns.append(comp)
        if action == "RECOMMEND_PAPER" and (comp.output is None or not comp.output.passed):
            action = _override(overrides, "WATCH", "compliance check failed or unavailable")

        for d in disagreements:
            await asyncio.to_thread(self.store.save_disagreement, mid, d)
        await asyncio.to_thread(
            self.store.save_decision,
            mid,
            {"cio_action": cio.output.action, "final_action": action, "overrides": overrides,
             "rationale": [c.model_dump(mode="json") for c in cio.output.rationale],
             "dissent": con_s + [d for d in disagreements],
             "risk_dry_run": dry, "compliance": comp.output.model_dump(mode="json") if comp.output else None},
        )  # fmt: skip
        rec_id = None
        if action == "RECOMMEND_PAPER":
            p = ctx.proposal
            rec_id = "REC-" + secrets.token_hex(6)
            await asyncio.to_thread(self.store.save_recommendation, {
                "id": rec_id, "meeting_id": mid, "underlying_key": ctx.underlying_key, "expiry": p["expiry"], "strike": p["strike"],
                "option_type": p["option_type"], "stop_loss": p["stop_loss"], "targets": p["targets"],
                "valid_until": now + timedelta(seconds=p.get("valid_for_s", 300)),
            })  # fmt: skip
        preds = await self._predictions(mid, ctx, turns, now)
        cost = sum(t.cost_inr for t in turns)
        await asyncio.to_thread(self.store.close, mid, "DECIDED", action, None, cost, sum(len(t.call_ids) for t in turns), now)
        return MeetingResult(mid, "DECIDED", action, None, turns, overrides, disagreements, rec_id, preds, cost, pack)

    async def _predictions(self, mid: str, ctx: MeetingContext, turns: list[AgentTurn], now: datetime) -> list[str]:
        """Pre-register every directional view BEFORE its outcome exists."""
        # Reference = index LTP (forward - basis): the same quantity the scorer later observes.
        ref = None
        if ctx.features is not None:
            fw, basis = ctx.features.values.get("forward"), ctx.features.values.get("forward_basis")
            if fw is not None and basis is not None and fw.value is not None and basis.value is not None:
                ref = fw.value - basis.value
        if ref is None:
            return []
        ids = []
        seen = set()
        for t in turns:
            pr = getattr(t.output, "prediction", None) if t.output is not None else None
            if pr is None or t.agent_id in seen:
                continue
            seen.add(t.agent_id)
            pid = "PRD-" + secrets.token_hex(6)
            await asyncio.to_thread(self.store.save_prediction, {
                "id": pid, "agent_id": t.agent_id, "agent_version": t.agent_version, "meeting_id": mid, "underlying_key": ctx.underlying_key,
                "target": pr.target, "direction": pr.direction, "confidence": t.confidence() or 1, "horizon_min": pr.horizon_min,
                "ref_value": ref, "created_at": now, "resolve_at": now + timedelta(minutes=pr.horizon_min), "mode": ctx.mode,
            })  # fmt: skip
            ids.append(pid)
        return ids

    async def _abort(self, mid, reason, turns, overrides, now, pack, disagreements=None) -> MeetingResult:
        cost = sum(t.cost_inr for t in turns)
        await asyncio.to_thread(
            self.store.close, mid, "ABORTED", "NO_ACTION", reason, cost, sum(len(t.call_ids) for t in turns), now
        )
        return MeetingResult(mid, "ABORTED", "NO_ACTION", reason, turns, overrides, disagreements or [], None, [], cost, pack)
