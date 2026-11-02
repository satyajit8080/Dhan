"""Operations: system status, Prometheus metrics, daily report, dashboard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, PlainTextResponse

from doi.app.security import requires
from doi.core.auth import Principal
from doi.core.market import IST, SessionCalendar

router = APIRouter()
DASHBOARD = Path(__file__).resolve().parent / "dashboard.html"
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"

UNVERIFIED = [
    "Live Grok call against this deployment's key (docs.x.ai request shape, models and prices verified 2026-11-02)",
    "Own-transport Dhan REST and WebSocket calls with this deployment's credentials (limits verified from dhanhq.co docs 2026-11-02)",
    "Dhan Live Market Feed packet layout (taken from official DhanHQ-py source, not live-tested)",
    "NSE/BSE holiday list and NSE options charge 0.03553% (NSE/FA/73061) are secondary-source verified; official NSE PDFs unreachable",
    "INR/USD rate and GST treatment used for Grok cost conversion",
]


def session_state(now: datetime, cal: SessionCalendar) -> dict:
    """Exchange session from the holiday-aware calendar. PRE-OPEN is 09:00-09:15 IST on trading days."""
    local = now.astimezone(IST)
    trading = cal.is_trading_day(local.date())
    state = "CLOSED"
    if trading and time(9, 0) <= local.time() < cal.open:
        state = "PRE-OPEN"
    elif cal.is_open(now):
        state = "OPEN"
    return {"state": state, "trading_day": trading, "ist": local.isoformat(timespec="seconds")}


def _db_info(dsn: str) -> dict:
    with psycopg.connect(dsn, connect_timeout=3) as c:
        mig = c.execute("SELECT max(version), count(*) FROM schema_migrations").fetchone()
        q = c.execute("SELECT status, count(*) FROM ai_events GROUP BY status").fetchall()
        last = c.execute(
            "SELECT id, status, final_action, started_at FROM ai_meetings ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        snaps = c.execute("SELECT count(*), max(received_at) FROM option_chain_snapshots").fetchone()
    return {
        "reachable": True,
        "migration_version": mig[0],
        "migrations_applied": mig[1],
        "ai_queue": {s: n for s, n in q},
        "last_meeting": None if last is None else dict(zip(("id", "status", "final_action", "started_at"), last, strict=True)),
        "recorded_snapshots": snaps[0],
        "last_snapshot_at": snaps[1],
    }


async def system_status(rt) -> dict:
    now = datetime.now(UTC)
    db = None
    if rt.paper_store is not None:
        try:
            db = await asyncio.to_thread(_db_info, rt.paper_store.dsn)
        except Exception as e:  # status must never fail because a component is down
            db = {"reachable": False, "error": type(e).__name__}
    budget = None
    if getattr(rt, "ai", None) is not None:
        try:
            st = await asyncio.to_thread(rt.ai.budget.status)
            budget = {"spent_inr": st.spent_inr, "reserved_inr": st.reserved_inr, "usable_inr": st.usable_inr, "hard_limit_inr": st.hard_limit_inr,
                      "utilisation": round(st.utilisation, 4), "frozen": st.frozen, "overrun_inr": st.overrun_inr}  # fmt: skip
        except Exception as e:
            budget = {"error": type(e).__name__}
    tok = rt.token.peek_status() if rt.token else None
    port = rt.paper.portfolio(now) if rt.paper else None
    return {
        "at": now,
        "mode": "RESEARCH_AND_PAPER_ONLY",
        "live_broker_execution": "NOT_IMPLEMENTED",
        "kill_switch": rt.kill_switch,
        "session": session_state(now, rt.paper.cal if rt.paper else SessionCalendar()),
        "auth": "enforced" if rt.auth else "dev-mode (no database)",
        "market_data": {
            "provider_configured": rt.provider is not None,
            "token": None if tok is None else {"state": tok.state, "expires_at": tok.expires_at},
            "feed_collector": None if rt.collector is None else jsonable_encoder(rt.collector.status),
            "recorder": jsonable_encoder(rt.recorder.health),
            "universe": {k: (k in rt.universe) for k in rt.configs},
        },
        "paper": None
        if port is None
        else {
            k: port[k] for k in ("cash", "equity", "day_pnl", "open_risk", "consecutive_losses", "kill_switch", "ledger_events")
        }
        | {
            "open_positions": len(port["positions"]),
            "working_orders": len(port["working_orders"]),
            "persistence": "postgres" if rt.paper_store else "memory",
        },
        "ai": None if getattr(rt, "ai", None) is None else {"provider_live": rt.ai.live, "budget": budget},
        "database": db,
        "unverified_integrations": UNVERIFIED,
    }


def prometheus(status: dict) -> str:
    lines = []

    def g(name: str, value, help_: str) -> None:
        if value is None:
            return
        v = (1.0 if value else 0.0) if isinstance(value, bool) else float(value)
        lines.extend([f"# HELP doi_{name} {help_}", f"# TYPE doi_{name} gauge", f"doi_{name} {v}"])

    g("kill_switch", status["kill_switch"], "1 when the kill switch is engaged")
    rec = status["market_data"]["recorder"]
    g("recorder_failures_total", rec["failures"], "market recorder write failures")
    g("recorder_snapshots_total", rec["snapshots"], "chain snapshots recorded by this process")
    if status["paper"]:
        g("paper_equity_inr", status["paper"]["equity"], "paper account equity")
        g("paper_open_positions", status["paper"]["open_positions"], "open paper positions")
        g("paper_open_risk_inr", status["paper"]["open_risk"], "aggregate open paper risk")
    ai = status.get("ai") or {}
    b = ai.get("budget") or {}
    if "spent_inr" in b:
        g("llm_spent_inr", b["spent_inr"], "Grok spend this month (INR)")
        g("llm_reserved_inr", b["reserved_inr"], "Grok in-flight reservations (INR)")
        g("llm_budget_utilisation", b["utilisation"], "share of usable monthly budget committed")
        g("llm_budget_frozen", b["frozen"], "1 when the month is frozen")
    db = status.get("database") or {}
    g("database_reachable", db.get("reachable", False) if db else False, "1 when PostgreSQL answers")
    for s, n in (db.get("ai_queue") or {}).items():
        lines.append(f'doi_ai_events{{status="{s}"}} {float(n)}')
    return "\n".join(lines) + "\n"


@router.get("/api/v1/system/status")
async def status_route(request: Request, who: Principal = Depends(requires("viewer"))):
    return jsonable_encoder(await system_status(request.app.state.runtime))


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics_route(request: Request, who: Principal = Depends(requires("viewer"))):
    return prometheus(jsonable_encoder(await system_status(request.app.state.runtime)))


@router.get("/api/v1/reports/daily", response_class=PlainTextResponse)
async def daily_report_route(request: Request, who: Principal = Depends(requires("viewer"))):
    from doi.reporting import daily_report

    return await daily_report(request.app.state.runtime)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Static page. All data is fetched with the user's API key (kept in sessionStorage)."""
    return HTMLResponse(
        DASHBOARD.read_text(),
        headers={
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
