"""AI components wiring, deterministic risk dry run, human approval bridge,
and the AI worker loop (Phase 7.3).

Isolation: the worker runs as its own process (`python -m doi.ai.worker`). It
reads recorded market data from PostgreSQL and never calls Dhan, so Grok outages,
API errors or budget exhaustion can't touch the collector or the paper engine.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from doi.ai.events import AdmissionPolicy, DetectorConfig, Event, EventKind, MemoryEventQueue, PgEventQueue, admit, detect
from doi.ai.meetings import MeetingContext, MeetingEngine, MeetingResult, MemoryMeetingStore, PgMeetingStore
from doi.core.auth import Principal
from doi.core.config import Settings
from doi.intelligence.feature_engine.engine import FeatureEngine, FeatureVector
from doi.llm.budget import MemoryBudget, PgBudget
from doi.llm.gateway import LLMGateway, MemoryCallLog, PgCallLog, XaiClient
from doi.llm.pricing import CostModel, TierModels
from doi.paper.driver import find_leg, market_state
from doi.risk.models import Candidate, Direction, Target

log = logging.getLogger(__name__)
D = Decimal


@dataclass
class AIComponents:
    gateway: LLMGateway
    store: Any
    queue: Any
    budget: Any
    calls: Any
    engine: MeetingEngine
    live: bool  # a real provider client is configured (AI_ENABLED and XAI_API_KEY)
    feature_engine: FeatureEngine = field(default_factory=FeatureEngine)
    features_fn: Any = None  # optional override (tests / alternative feature sources)
    snapshot_fn: Any = None


def build_ai(settings: Settings, dsn: str | None, kill_switch, client=None) -> AIComponents:
    budget = (
        PgBudget(dsn, D(str(settings.llm_monthly_limit_inr)), D(str(settings.llm_safety_buffer_inr)))
        if dsn
        else MemoryBudget(D(str(settings.llm_monthly_limit_inr)), D(str(settings.llm_safety_buffer_inr)))
    )
    calls = PgCallLog(dsn) if dsn else MemoryCallLog()
    live = client is not None or bool(settings.ai_enabled and settings.xai_api_key)
    if client is None and live:
        client = XaiClient(settings.xai_api_key, settings.xai_base_url)  # type: ignore[arg-type]
    gw = LLMGateway(
        client,
        budget,
        calls,
        tiers=TierModels(settings.llm_fast_model, settings.llm_reasoning_model),
        cost=CostModel(D(str(settings.llm_fx_inr_per_usd)), D(str(settings.llm_tax_multiplier))),
        kill_switch=kill_switch,
        enabled=live,
    )
    store = PgMeetingStore(dsn) if dsn else MemoryMeetingStore()
    queue = PgEventQueue(dsn) if dsn else MemoryEventQueue()
    return AIComponents(gw, store, queue, budget, calls, MeetingEngine(gw, store), live)


def proposal_contract(rt, underlying: str, proposal: dict):
    cfg = rt.configs[underlying]
    return rt.master.contract(
        cfg.exchange_family, cfg.master_symbol, _d(proposal["expiry"]), float(proposal["strike"]), proposal["option_type"]
    )


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def risk_dry_run(rt, underlying: str, proposal: dict, snapshot, now: datetime, signal_id: str = "DRY-RUN") -> dict:
    """Deterministic RiskGate verdict on a proposal, on a throwaway copy of the
    paper engine. Nothing is submitted or persisted."""
    cfg = rt.configs[underlying]
    contract = proposal_contract(rt, underlying, proposal)
    cand = Candidate(
        signal_id=signal_id,
        underlying_key=underlying,
        direction=Direction.BUY_CE if proposal["option_type"] == "CE" else Direction.BUY_PE,
        contract=contract,
        created_at=now,
        valid_until=now + timedelta(seconds=proposal.get("valid_for_s", 300)),
        stop_loss=float(proposal["stop_loss"]),
        targets=tuple(Target(float(p), float(f)) for p, f in proposal["targets"]),
        config_version=cfg.config_version,
    )
    m = market_state(snapshot, contract, now, cfg.freshness.chain_max_age_s)
    d = rt.paper.gate.approve(cand, rt.paper.account_state(now, roll=False), m, now)  # pure: no engine mutation
    leg = find_leg(snapshot, contract)
    return {
        "approved": d.approved,
        "lots": d.lots,
        "quantity": d.quantity,
        "entry_price_est": float(d.entry_price_est) if d.entry_price_est is not None else None,
        "risk_per_lot": float(d.risk_per_lot) if d.risk_per_lot is not None else None,
        "reasons": list(d.reasons),
        "checks": {c.name: c.passed for c in d.checks},
        "contract_bid": leg.bid if leg else None,
        "contract_ask": leg.ask if leg else None,
        "config_version": d.config_version,
    }


async def approve_recommendation(rt, rec_id: str, who: Principal, upstream_error, note: str | None = None) -> dict:
    """Human approval: approval record, then the live re-check through the shared
    paper bridge (RiskGate decides again on fresh data), then the paper engine."""
    from doi.app.security import PaperRequest, submit_paper

    store = rt.ai.store
    rec = await asyncio.to_thread(store.get_recommendation, rec_id)
    now = datetime.now(UTC)
    if rec is None:
        raise KeyError(rec_id)
    if rec["status"] != "PENDING_HUMAN":
        raise ValueError(f"recommendation {rec_id} is {rec['status']}")
    if rec["valid_until"] < now:
        await asyncio.to_thread(
            store.update_recommendation, rec_id, "EXPIRED", who.actor, {"reason": "expired before approval"}, now
        )
        raise ValueError(f"recommendation {rec_id} expired")
    await asyncio.to_thread(
        rt.approvals.append, who, "APPROVE", "ai_recommendation", rec_id, note, {"meeting_id": rec["meeting_id"]}
    )
    req = PaperRequest(
        signal_id=rec_id,  # the paper idempotency key: a second approval returns the same order
        underlying=rec["underlying_key"],
        expiry=_d(rec["expiry"]),
        strike=float(rec["strike"]),
        option_type=rec["option_type"],
        stop_loss=float(rec["stop_loss"]),
        targets=tuple((float(p), float(f)) for p, f in rec["targets"]),
        valid_for_s=max(5.0, (rec["valid_until"] - now).total_seconds()),
        feature_snapshot_id=rec["meeting_id"],
    )
    res = await submit_paper(rt, req, upstream_error)
    approved = bool(res.decision and res.decision.approved)
    result = {
        "risk_gate_approved": approved,
        "reasons": list(res.decision.reasons) if res.decision else [],
        "order_id": res.order.id if res.order else None,
    }
    await asyncio.to_thread(
        store.update_recommendation, rec_id, "SUBMITTED" if approved else "RISK_REJECTED", who.actor, result, now
    )
    return result


async def reject_recommendation(rt, rec_id: str, who: Principal, note: str | None = None) -> None:
    rec = await asyncio.to_thread(rt.ai.store.get_recommendation, rec_id)
    if rec is None:
        raise KeyError(rec_id)
    await asyncio.to_thread(
        rt.approvals.append, who, "REJECT", "ai_recommendation", rec_id, note, {"meeting_id": rec["meeting_id"]}
    )
    await asyncio.to_thread(rt.ai.store.update_recommendation, rec_id, "REJECTED", who.actor, {"note": note}, datetime.now(UTC))


@dataclass
class AIWorker:
    rt: Any
    timeframe_min: int = 5
    policy: AdmissionPolicy = field(default_factory=AdmissionPolicy)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    features_fn: Any = None  # (underlying, now) -> FeatureVector | None ; defaults to recorded-data replay
    snapshot_fn: Any = None  # (underlying, expiry, now) -> latest OptionChainSnapshot | None
    history: dict[str, list[FeatureVector]] = field(default_factory=dict)

    def _features(self, underlying: str, now: datetime) -> FeatureVector | None:
        fn = self.features_fn or self.rt.ai.features_fn
        if fn is not None:
            return fn(underlying, now)
        from doi.data.historical_ingestion.runner import replay_features
        from doi.data.recorder import ReplayStore

        ref = self.rt.universe.get(underlying)
        if ref is None or self.rt.paper_store is None:
            return None
        store = ReplayStore(self.rt.paper_store.dsn)
        return replay_features(store, self.rt.ai.feature_engine, underlying_key=underlying, index_security_id=ref.security_id,
                               timeframe_min=self.timeframe_min, as_of_points=[now], lookback=timedelta(days=5))[0]  # fmt: skip

    def _snapshot(self, underlying: str, expiry: date, now: datetime):
        fn = self.snapshot_fn or self.rt.ai.snapshot_fn
        if fn is not None:
            return fn(underlying, expiry, now)
        from doi.data.recorder import ReplayStore

        snaps = ReplayStore(self.rt.paper_store.dsn).snapshots(underlying, now, now - timedelta(minutes=5), expiry)
        return snaps[-1] if snaps else None

    def _ai_available(self, now: datetime) -> bool:
        if not self.rt.ai.live:
            return False
        st = self.rt.ai.budget.status(now)
        return not st.frozen and self.rt.ai.gateway.routing.tier(None, "fast", st.utilisation) is not None

    async def observe(self, now: datetime) -> list[str]:
        """Detect events from the newest feature vectors and admit them. No LLM calls happen here."""
        ids = []
        for key, cfg in self.rt.configs.items():
            if not cfg.live_enabled:
                continue
            fv = await asyncio.to_thread(self._features, key, now)
            if fv is None:
                continue
            hist = self.history.setdefault(key, [])
            hist.append(fv)
            del hist[:-10]
            for ev in detect(hist, self.detector):
                ids.append(self.enqueue(ev, fv.data_quality_score, now))
        return ids

    def enqueue(self, ev: Event, quality: float | None, now: datetime) -> str:
        decision = admit(ev, self.rt.ai.queue, self.policy, calendar=self.rt.paper.cal, kill_switch=self.rt.kill_switch,
                         quality=quality, ai_available=self._ai_available(now))  # fmt: skip
        return self.rt.ai.queue.admit(ev, decision)

    async def process_one(self, now: datetime) -> MeetingResult | None:
        row = await asyncio.to_thread(self.rt.ai.queue.claim, now)
        if row is None:
            return None
        try:
            ctx = await self._context(row, now)
            res = await self.rt.ai.engine.run(ctx, now)
            await asyncio.to_thread(self.rt.ai.queue.finish, row["id"], "DONE", res.abort_reason)
            return res
        except Exception as e:  # isolation: a bad meeting never kills the worker
            log.exception("meeting for event %s failed", row["id"])
            await asyncio.to_thread(self.rt.ai.queue.finish, row["id"], "FAILED", type(e).__name__)
            return None

    async def _context(self, row: dict, now: datetime) -> MeetingContext:
        u = row["underlying_key"]
        fv = await asyncio.to_thread(self._features, u, now) if u else None
        payload = row["payload"] or {}
        proposal = payload.get("proposal")
        dry = None
        if proposal and u:
            snap = await asyncio.to_thread(self._snapshot, u, _d(proposal["expiry"]), now)
            if snap is None:
                dry = {"approved": False, "reasons": ["no recorded chain"]}
            else:  # evaluate at the later of meeting time and quote time: a quote is never 'from the future'
                dry_now = max(now, snap.source_ts or snap.received_at)
                dry = risk_dry_run(self.rt, u, proposal, snap, dry_now, signal_id=f"DRY-{row['id']}")
        port = self.rt.paper.portfolio(now) if self.rt.paper else None
        portfolio = None
        if port:
            portfolio = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in port.items()
                         if k in ("cash", "equity", "day_pnl", "open_risk", "consecutive_losses", "kill_switch")}  # fmt: skip
            portfolio["open_positions"] = len(port["positions"])
        return MeetingContext(event={"id": row["id"], "kind": str(row["kind"]), "payload": payload}, underlying_key=u, features=fv,
                              proposal=proposal, risk_dry_run=dry, portfolio=portfolio, event_id=row["id"])  # fmt: skip

    async def run_forever(self, interval_s: float = 60.0) -> None:
        while True:
            now = datetime.now(UTC)
            try:
                await self.observe(now)
                while await self.process_one(datetime.now(UTC)) is not None:
                    pass
            except Exception:
                log.exception("AI worker pass failed")
            await asyncio.sleep(interval_s)


def human_request_event(underlying: str, proposal: dict | None, who: Principal, now: datetime) -> Event:
    return Event(EventKind.HUMAN_REQUEST, underlying, now, {"key": f"{who.actor}:{now.isoformat()}", "proposal": proposal, "requested_by": who.actor},
                 valid_for=timedelta(minutes=10), priority=1)  # fmt: skip
