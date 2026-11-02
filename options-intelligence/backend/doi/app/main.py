"""FastAPI application. The frontend calls only these routes; Dhan credentials
never leave the backend. No route can place, modify or cancel a broker order."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from doi.app.runtime import Runtime, build_runtime
from doi.core.config import Settings
from doi.core.errors import (
    CircuitOpen,
    ConfigError,
    DhanError,
    InstrumentError,
    RateLimited,
    SchemaError,
    TokenRejected,
    UpstreamError,
    UpstreamTimeout,
)
from doi.core.models import UnderlyingRef
from doi.paper.driver import market_state, run_forever
from doi.paper.engine import PaperExecution
from doi.paper.models import InvalidTransition
from doi.risk.models import Candidate, Direction, Target


class TargetIn(BaseModel):
    price: float = Field(gt=0)
    fraction: float = Field(gt=0, le=1)


class PaperOrderIn(BaseModel):
    signal_id: str = Field(min_length=1, max_length=128)
    underlying: str
    expiry: date
    strike: float = Field(gt=0)
    option_type: str = Field(pattern="^(CE|PE)$")
    stop_loss: float = Field(gt=0)
    targets: list[TargetIn] = Field(min_length=1, max_length=5)
    valid_for_s: float = Field(default=60, gt=0, le=900)
    underlying_invalidation: float | None = None
    feature_snapshot_id: str | None = None


def _paper(rt: Runtime) -> PaperExecution:
    if rt.paper is None:
        raise HTTPException(503, "paper engine not configured")
    return rt.paper


def _rt(request: Request) -> Runtime:
    return request.app.state.runtime


def _underlying(rt: Runtime, key: str, *, require_live: bool = True) -> UnderlyingRef:
    key = key.upper()
    cfg = rt.configs.get(key)
    if cfg is None:
        raise HTTPException(404, f"unknown underlying {key}")
    if require_live and not cfg.live_enabled:
        raise HTTPException(409, f"{key} not enabled: {cfg.activation_gate}")
    ref = rt.universe.get(key)
    if ref is None:
        raise HTTPException(503, rt.universe_errors.get(key, f"{key} not resolved from instrument master"))
    if rt.provider is None:
        raise HTTPException(503, "market data provider not configured")
    return ref


def _upstream(e: Exception) -> HTTPException:
    if isinstance(e, TokenRejected):
        return HTTPException(503, "Dhan token rejected or expired; re-authenticate")
    if isinstance(e, (CircuitOpen, RateLimited, UpstreamError, UpstreamTimeout)):
        return HTTPException(503, f"Dhan unavailable: {type(e).__name__}")
    if isinstance(e, SchemaError):
        return HTTPException(502, f"Dhan payload rejected: {e}")
    return HTTPException(502, f"Dhan request failed: {type(e).__name__}")


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "runtime", None) is None:
            app.state.runtime = await build_runtime(settings or Settings.from_env())
        rt = app.state.runtime
        tasks = []
        if rt.paper is not None and rt.provider is not None:
            tasks.append(asyncio.create_task(run_forever(rt, (settings or Settings.from_env()).paper_feed_interval_s)))
        if rt.collector is not None:
            tasks.append(asyncio.create_task(rt.collector.run()))
        yield
        if rt.collector is not None:
            await rt.collector.stop()
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    app = FastAPI(title="Dhan Options Intelligence", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/healthz")
    async def healthz(request: Request):
        rt = _rt(request)
        tok = rt.token.peek_status() if rt.token else None
        return {
            "status": "ok",
            "paper_only": True,
            "kill_switch": rt.kill_switch,
            "paper_persistence": "postgres" if rt.paper_store else "memory",
            "market_feed": None if rt.collector is None else jsonable_encoder(rt.collector.status),
            "master_version": rt.master_version,
            "token": None if tok is None else {"state": tok.state, "fingerprint": tok.fingerprint, "expires_at": tok.expires_at},
        }

    @app.get("/api/v1/market/underlyings")
    async def underlyings(request: Request):
        rt = _rt(request)
        out = []
        for key, cfg in rt.configs.items():
            ref = rt.universe.get(key)
            out.append(
                {
                    "key": key,
                    "exchange_family": cfg.exchange_family,
                    "config_version": cfg.config_version,
                    "live_enabled": cfg.live_enabled,
                    "historical_status": cfg.historical.status,
                    "resolved": ref is not None,
                    "error": rt.universe_errors.get(key),
                    "security_id": ref.security_id if ref else None,
                    "derivative_segment": ref.derivative_segment if ref else None,
                    "expiries": [asdict(p) for p in ref.expiries[:4]] if ref else [],
                }
            )
        return jsonable_encoder(out)

    @app.get("/api/v1/market/expiries")
    async def expiries(request: Request, underlying: str = Query(...)):
        rt = _rt(request)
        ref = _underlying(rt, underlying)
        try:
            exps = await rt.provider.get_expiries(ref)  # type: ignore[union-attr]
        except (DhanError, CircuitOpen, SchemaError) as e:
            raise _upstream(e) from None
        return {"underlying": ref.key, "expiries": [d.isoformat() for d in exps], "master_version": ref.master_version}

    @app.get("/api/v1/market/option-chain")
    async def option_chain(request: Request, underlying: str = Query(...), expiry: date = Query(...)):
        rt = _rt(request)
        if rt.kill_switch:
            raise HTTPException(503, "kill switch engaged")
        ref = _underlying(rt, underlying)
        if ref.profile(expiry) is None:
            raise HTTPException(404, f"{ref.key}: expiry {expiry} not in instrument master {ref.master_version[:12]}")
        cfg = rt.configs[ref.key]
        try:
            snap = await rt.provider.get_option_chain(ref, expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)  # type: ignore[union-attr]
        except (DhanError, CircuitOpen, SchemaError) as e:
            raise _upstream(e) from None
        age = snap.age_s(datetime.now(UTC))
        prof = ref.profile(expiry)
        body = jsonable_encoder(snap)
        body["quality"] = {
            "age_s": round(age, 3),
            "is_stale": age > cfg.freshness.chain_max_age_s,
            "max_age_s": cfg.freshness.chain_max_age_s,
        }
        body["contract"] = jsonable_encoder(prof)
        return body

    # ---- Paper trading: simulated only. Nothing here reaches a broker order API. ----

    @app.post("/api/v1/paper/orders", status_code=201)
    async def paper_order(request: Request, body: PaperOrderIn):
        rt = _rt(request)
        engine = _paper(rt)
        ref = _underlying(rt, body.underlying)
        cfg = rt.configs[ref.key]
        if rt.master is None:
            raise HTTPException(503, "instrument master not loaded")
        try:
            contract = rt.master.contract(cfg.exchange_family, cfg.master_symbol, body.expiry, body.strike, body.option_type)
        except InstrumentError as e:
            raise HTTPException(404, str(e)) from None
        try:
            snap = await rt.provider.get_option_chain(ref, body.expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)  # type: ignore[union-attr]
        except (DhanError, CircuitOpen, SchemaError) as e:
            raise _upstream(e) from None
        now = datetime.now(UTC)  # after the fetch, so quote age is never negative
        try:
            cand = Candidate(
                signal_id=body.signal_id,
                underlying_key=ref.key,
                direction=Direction.BUY_CE if body.option_type == "CE" else Direction.BUY_PE,
                contract=contract,
                created_at=now,
                valid_until=now + timedelta(seconds=body.valid_for_s),
                stop_loss=body.stop_loss,
                targets=tuple(Target(t.price, t.fraction) for t in body.targets),
                config_version=cfg.config_version,
                underlying_invalidation=body.underlying_invalidation,
                feature_snapshot_id=body.feature_snapshot_id,
            )
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None
        market = market_state(snap, contract, now, cfg.freshness.chain_max_age_s)

        def submit(e):
            if rt.kill_switch:
                e.set_kill_switch(True, now)
            return e.submit_simulated(cand, market, now)

        res = await rt.paper_service.call(submit)
        engine = rt.paper
        out = jsonable_encoder(
            {
                "duplicate": res.duplicate,
                "approved": bool(res.decision and res.decision.approved),
                "decision": res.decision,
                "reasons": res.decision.reasons if res.decision else [],
                "order": engine._order_view(res.order) if res.order else None,
            }
        )
        if res.duplicate:
            return JSONResponse(out, status_code=200)
        if not out["approved"]:
            raise HTTPException(422, out)
        return out

    @app.get("/api/v1/paper/portfolio")
    async def paper_portfolio(request: Request):
        return jsonable_encoder(_paper(_rt(request)).portfolio(datetime.now(UTC)))

    @app.get("/api/v1/paper/trades")
    async def paper_trades(request: Request):
        return jsonable_encoder(_paper(_rt(request)).trades)

    @app.get("/api/v1/paper/ledger")
    async def paper_ledger(request: Request, after_seq: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=5000)):
        led = _paper(_rt(request)).ledger
        return jsonable_encoder({"head": led.head, "events": led.since(after_seq)[:limit]})

    @app.post("/api/v1/paper/positions/{position_id}/close")
    async def paper_close(request: Request, position_id: str):
        rt = _rt(request)
        engine = _paper(rt)
        try:
            order = await rt.paper_service.call(lambda e: e.close_position(position_id, datetime.now(UTC)))
        except InvalidTransition as e:
            raise HTTPException(409, str(e)) from None
        return jsonable_encoder({"exit_order": engine._order_view(order) if order else None})

    return app
