"""Live Grok validation, capped at ₹20 per run (Phase 8, priority 1).

Needs: XAI_API_KEY, network access to api.x.ai. Optional: DATABASE_URL (calls are then also
logged to llm_calls and charged to the monthly budget, which keeps its ₹3,000 cap).

Steps (each step's result is printed as JSON and marked LIVE):
  1. GET /v1/language-models: model availability and provider prices (proto units -> USD/M)
  2. one minimal chat call: request format and usage fields (prompt/completion/reasoning/cached/cost ticks)
  3. each of the 15 agents once on a SIMULATED evidence pack: schema and evidence validation per agent
  4. one full meeting
  5. budget reconciliation: our settled cost vs the provider-reported cost per call
The run stops as soon as the ₹20 run budget is exhausted (the gateway refuses further calls).

    XAI_API_KEY=... python scripts/grok_live_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import SecretStr  # noqa: E402

from doi.ai.agents import AGENTS, run_agent  # noqa: E402
from doi.ai.meetings import MeetingEngine, MemoryMeetingStore  # noqa: E402
from doi.llm.budget import MemoryBudget  # noqa: E402
from doi.llm.gateway import LLMGateway, LLMRequest, MemoryCallLog, XaiClient  # noqa: E402
from doi.llm.pricing import SEED_PRICES, prices_from_listing  # noqa: E402

RUN_CAP_INR = Decimal(os.environ.get("GROK_LIVE_CAP_INR", "20"))


def out(step: str, **kw) -> None:
    print(json.dumps({"step": step, "data_class": "LIVE", **kw}, default=str))


async def main() -> int:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        out("preflight", ok=False, blocker="XAI_API_KEY not set")
        return 2
    client = XaiClient(SecretStr(key), os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"))
    try:
        models = await client.list_models()
    except Exception as e:
        out("models", ok=False, error=type(e).__name__)
        return 1
    prices = prices_from_listing(models)
    out("models", ok=True, models=sorted(prices), seed_models_present={m: m in prices for m in SEED_PRICES})
    book = {**SEED_PRICES, **prices}  # provider prices replace seeds wherever available
    calls = MemoryCallLog()
    gw = LLMGateway(client, MemoryBudget(RUN_CAP_INR, Decimal("2")), calls, prices=book, enabled=True)

    r = await gw.complete(LLMRequest(purpose="live:probe", system="Reply with the JSON object {\"ok\": true} only.", user="ping", max_tokens=20))
    out("probe", ok=True, model=r.model, usage=r.usage.__dict__, cost_inr=r.cost_inr, provider_cost_reported=r.usage.cost_ticks is not None)

    from tests.unit.test_meetings import ctx  # SIMULATED evidence pack (clearly labelled)

    c = ctx()
    from doi.ai.evidence import build_pack

    pack = build_pack(as_of=c.features.as_of, underlying_key="NIFTY", event={"kind": "LIVE_CHECK", "evidence": "SIMULATED"}, features=c.features,
                      extra={"proposal": c.proposal, "risk": c.risk_dry_run, "calendar": {"status": "UNVERIFIED_SOURCE"}, "breadth": {"status": "NO_SOURCE"}})  # fmt: skip
    for a in AGENTS:
        t = await run_agent(a, pack, gw, task="Live validation: give your assessment.", turns=[{"agent": "regime", "stance": "NEUTRAL"}])
        out("agent", agent=a.id, status=t.status, errors=list(t.errors)[:3], cost_inr=t.cost_inr)
    m = await MeetingEngine(gw, MemoryMeetingStore()).run(c, c.features.as_of)
    out("meeting", status=m.status, final_action=m.final_action, abort_reason=m.abort_reason, overrides=m.overrides, cost_inr=m.cost_inr)
    ok_rows = [x for x in calls.rows if x["status"] == "OK"]
    ticks = [x for x in ok_rows if x.get("cost_source") == "PROVIDER_TICKS"]
    st = gw.budget.status()
    out("budget", run_cap_inr=RUN_CAP_INR, spent_inr=st.spent_inr, reserved_inr=st.reserved_inr, overrun_inr=st.overrun_inr, frozen=st.frozen,
        calls_ok=len(ok_rows), calls_with_provider_cost=len(ticks), refusals=sum(1 for x in calls.rows if x["status"].startswith("REFUSED")))  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
