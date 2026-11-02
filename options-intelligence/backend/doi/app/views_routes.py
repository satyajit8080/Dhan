"""Read-only list views for the terminal UI. No writes, no broker access; every number is computed here."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row

from doi.app.security import requires
from doi.core.auth import Principal

router = APIRouter()

_SQL = {
    "predictions": "SELECT p.id, p.agent_id, p.agent_version, p.meeting_id, p.underlying_key, p.target, p.direction, p.confidence,"
    " p.horizon_min, p.ref_value, p.created_at, p.resolve_at, p.mode, o.resolved_at, o.realized_value, o.signed_return, o.hit, o.brier"
    " FROM ai_predictions p LEFT JOIN ai_prediction_outcomes o ON o.prediction_id = p.id ORDER BY p.created_at DESC LIMIT %s",
    "strategies": "SELECT s.id, s.name, s.active_version_id, s.created_at, count(v.id) AS versions, max(v.version) AS latest_version"
    " FROM strategies s LEFT JOIN strategy_versions v ON v.strategy_id = s.id GROUP BY s.id ORDER BY s.created_at DESC LIMIT %s",
    "experiments": "SELECT e.id, e.strategy_version_id, e.dataset_id, e.status, e.manifest_sha256, e.created_at,"
    " coalesce(jsonb_object_agg(r.stage, jsonb_build_object('passed', r.passed, 'metrics', r.metrics)) FILTER (WHERE r.stage IS NOT NULL), '{}') AS results"
    " FROM research_experiments e LEFT JOIN research_results r ON r.experiment_id = e.id GROUP BY e.id ORDER BY e.created_at DESC LIMIT %s",
    "option_backtests": "SELECT id, strategy_version_id, data_class, datasets, params, metrics, metrics_sha256, created_at"
    " FROM research_option_backtests ORDER BY created_at DESC LIMIT %s",
}


def _rows(dsn: str, name: str, limit: int) -> list[dict]:
    with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as c:
        return c.execute(_SQL[name], (limit,)).fetchall()


def _dsn(request: Request) -> str:
    rt = request.app.state.runtime
    if rt.paper_store is None:
        raise HTTPException(503, "requires DATABASE_URL")
    return rt.paper_store.dsn


def _view(name: str, path: str):
    async def handler(request: Request, limit: int = Query(200, ge=1, le=1000), who: Principal = Depends(requires("viewer"))):
        return jsonable_encoder(await asyncio.to_thread(_rows, _dsn(request), name, limit))

    router.add_api_route(path, handler, methods=["GET"], name=f"list_{name}")


_view("predictions", "/api/v1/ai/predictions")
_view("strategies", "/api/v1/research/strategies")
_view("experiments", "/api/v1/research/experiments")
_view("option_backtests", "/api/v1/research/option-backtests")


def paper_analytics(trades: list, starting_capital: Decimal) -> dict:
    """Closed-trade statistics after charges. Drawdown is measured on the closed-trade equity path."""
    ts = sorted(trades, key=lambda t: t.closed_at)
    wins = [t.net_pnl for t in ts if t.net_pnl > 0]
    losses = [t.net_pnl for t in ts if t.net_pnl <= 0]
    eq = peak = starting_capital
    max_dd = Decimal(0)
    for t in ts:
        eq += t.net_pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    groups: dict[str, dict] = {}
    for key in ("underlying_key", "exit_reason"):
        g: dict[str, dict] = {}
        for t in ts:
            k = str(getattr(t, key))
            a = g.setdefault(k, {"trades": 0, "net_pnl": Decimal(0), "charges": Decimal(0)})
            a["trades"] += 1
            a["net_pnl"] += t.net_pnl
            a["charges"] += t.charges
        groups[key] = g
    n = len(ts)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n if n else None,
        "gross_pnl": sum((t.gross_pnl for t in ts), Decimal(0)),
        "charges": sum((t.charges for t in ts), Decimal(0)),
        "net_pnl": sum((t.net_pnl for t in ts), Decimal(0)),
        "avg_win": sum(wins, Decimal(0)) / len(wins) if wins else None,
        "avg_loss": sum(losses, Decimal(0)) / len(losses) if losses else None,
        "expectancy": sum((t.net_pnl for t in ts), Decimal(0)) / n if n else None,
        "max_drawdown": max_dd,
        "max_drawdown_pct": float(max_dd / peak) if peak else None,
        "by_underlying": groups["underlying_key"],
        "by_exit_reason": groups["exit_reason"],
    }


@router.get("/api/v1/paper/analytics")
async def paper_analytics_route(request: Request, who: Principal = Depends(requires("viewer"))):
    p = request.app.state.runtime.paper
    if p is None:
        raise HTTPException(503, "paper engine not configured")
    return jsonable_encoder(paper_analytics(p.trades, p.starting_capital))
