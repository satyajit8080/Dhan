"""Daily operations and research report (markdown). `python -m doi.reporting` prints it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg


def _q(dsn: str, sql: str, args: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn, connect_timeout=3) as c:
        return c.execute(sql, args).fetchall()


async def daily_report(rt, now: datetime | None = None) -> str:
    from doi.app.ops_routes import system_status

    now = now or datetime.now(UTC)
    st = await system_status(rt)
    lines = [
        f"# Bull50 research & paper report - {now:%Y-%m-%d %H:%M} UTC",
        "",
        "Mode: research and paper trading only. No live broker execution exists.",
        "",
    ]
    lines += ["## System", f"- Kill switch: {'ON' if st['kill_switch'] else 'off'}", f"- Auth: {st['auth']}",
              f"- Recorder failures (this process): {st['market_data']['recorder']['failures']}"]  # fmt: skip
    if st["paper"]:
        p = st["paper"]
        lines += [
            "",
            "## Paper account",
            f"- Equity: ₹{p['equity']}  (day P&L ₹{p['day_pnl']})",
            f"- Open positions: {p['open_positions']}, open risk ₹{p['open_risk']}",
        ]
    ai = st.get("ai") or {}
    b = ai.get("budget") or {}
    if "spent_inr" in b:
        lines += ["", "## Grok budget", f"- Spent ₹{float(b['spent_inr']):.2f} of usable ₹{float(b['usable_inr']):.2f} (hard limit ₹{float(b['hard_limit_inr']):.2f})",
                  f"- Utilisation {float(b['utilisation']) * 100:.1f}%  frozen={b['frozen']}  provider live={ai.get('provider_live')}"]  # fmt: skip
    if rt.paper_store is not None:
        dsn, since = rt.paper_store.dsn, now - timedelta(days=1)
        try:
            m = _q(dsn, "SELECT final_action, count(*) FROM ai_meetings WHERE started_at >= %s GROUP BY 1 ORDER BY 1", (since,))
            r = _q(dsn, "SELECT status, count(*) FROM ai_recommendations WHERE created_at >= %s GROUP BY 1 ORDER BY 1", (since,))
            a = _q(
                dsn,
                "SELECT abort_reason, count(*) FROM ai_meetings WHERE started_at >= %s AND status = 'ABORTED' GROUP BY 1",
                (since,),
            )
            calls = _q(
                dsn,
                "SELECT status, count(*), COALESCE(sum(cost_inr), 0) FROM llm_calls WHERE created_at >= %s GROUP BY 1 ORDER BY 1",
                (since,),
            )
            lines += ["", "## Last 24 h", "- Meetings: " + (", ".join(f"{x or 'OPEN'}={n}" for x, n in m) or "none"),
                      "- Aborted: " + (", ".join(f"{x}={n}" for x, n in a) or "none"),
                      "- Recommendations: " + (", ".join(f"{x}={n}" for x, n in r) or "none"),
                      "- Grok calls: " + (", ".join(f"{s}={n} (₹{float(c):.2f})" for s, n, c in calls) or "none")]  # fmt: skip
            from doi.ai.scoring import PgScoring

            lb = PgScoring(dsn).leaderboard("LIVE")
            lines += ["", "## Leaderboard (LIVE)"]
            lines += [f"- #{x['rank'] or '-'} {x['agent_id']}: n={x['n']} hit={x['hit_rate']} wilsonLB={x['hit_rate_wilson_lb']} brier={x['brier']}"
                      + (" (provisional)" if x["provisional"] else "") for x in lb[:15]] or ["- no resolved predictions yet"]  # fmt: skip
        except Exception as e:
            lines += ["", f"_Database section unavailable: {type(e).__name__}_"]
    lines += ["", "## Unverified integrations"] + [f"- {u}" for u in st["unverified_integrations"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    from doi.app.runtime import build_runtime
    from doi.core.config import Settings

    async def _main() -> None:
        rt = await build_runtime(Settings.from_env())
        print(await daily_report(rt))

    asyncio.run(_main())
