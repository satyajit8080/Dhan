"""FastAPI application. The frontend calls only these routes; Dhan credentials
never leave the backend. No route can place, modify or cancel a broker order."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder

from doi.app.runtime import Runtime, build_runtime
from doi.core.config import Settings
from doi.core.errors import CircuitOpen, DhanError, RateLimited, SchemaError, TokenRejected, UpstreamError, UpstreamTimeout
from doi.core.models import UnderlyingRef


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
        yield

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

    return app
