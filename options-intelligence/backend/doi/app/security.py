"""FastAPI auth dependencies and the shared human-gated paper bridge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException, Request

from doi.core.auth import DEV_PRINCIPAL, Principal
from doi.core.errors import CircuitOpen, ConfigError, DhanError, InstrumentError, SchemaError
from doi.paper.driver import market_state
from doi.paper.engine import SubmitResult
from doi.risk.models import Candidate, Direction, Target


def requires(role: str):
    async def dep(request: Request) -> Principal:
        rt = request.app.state.runtime
        if rt.auth is None:
            return DEV_PRINCIPAL  # in-memory development mode only; DB deployments always enforce keys
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        who = await asyncio.to_thread(rt.auth.authenticate, token)
        if who is None:
            raise HTTPException(401, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})
        if not who.has(role):
            raise HTTPException(403, f"requires role {role}")
        return who

    return dep


@dataclass(frozen=True)
class PaperRequest:
    signal_id: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    stop_loss: float
    targets: tuple[tuple[float, float], ...]
    valid_for_s: float = 60
    underlying_invalidation: float | None = None
    feature_snapshot_id: str | None = None


async def submit_paper(rt, req: PaperRequest, upstream_error) -> SubmitResult:
    """The only route from a human decision into the paper engine:
    instrument master -> live chain -> Candidate -> RiskGate -> PaperService."""
    key = req.underlying.upper()
    cfg = rt.configs.get(key)
    ref = rt.universe.get(key)
    if cfg is None:
        raise HTTPException(404, f"unknown underlying {key}")
    if not cfg.live_enabled:
        raise HTTPException(409, f"{key} not enabled: {cfg.activation_gate}")
    if ref is None or rt.provider is None or rt.master is None:
        raise HTTPException(503, f"{key} market data not available")
    try:
        contract = rt.master.contract(cfg.exchange_family, cfg.master_symbol, req.expiry, req.strike, req.option_type)
    except InstrumentError as e:
        raise HTTPException(404, str(e)) from None
    try:
        snap = await rt.provider.get_option_chain(ref, req.expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)
    except (DhanError, CircuitOpen, SchemaError) as e:
        raise upstream_error(e) from None
    await asyncio.to_thread(rt.recorder.snapshot, snap)
    now = datetime.now(UTC)  # after the fetch, so quote age is never negative
    try:
        cand = Candidate(
            signal_id=req.signal_id,
            underlying_key=ref.key,
            direction=Direction.BUY_CE if req.option_type == "CE" else Direction.BUY_PE,
            contract=contract,
            created_at=now,
            valid_until=now + timedelta(seconds=req.valid_for_s),
            stop_loss=req.stop_loss,
            targets=tuple(Target(p, f) for p, f in req.targets),
            config_version=cfg.config_version,
            underlying_invalidation=req.underlying_invalidation,
            feature_snapshot_id=req.feature_snapshot_id,
        )
    except ConfigError as e:
        raise HTTPException(422, str(e)) from None
    market = market_state(snap, contract, now, cfg.freshness.chain_max_age_s)

    def submit(e):
        if rt.kill_switch:
            e.set_kill_switch(True, now)
        return e.submit_simulated(cand, market, now)

    return await rt.paper_service.call(submit)
