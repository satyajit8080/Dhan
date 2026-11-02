"""Paper engine wired through the runtime, chain driver and REST API (offline)."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from doi.app.main import create_app
from doi.app.runtime import Runtime, build_paper
from doi.core.config import DEFAULT_STRATEGY_DIR
from doi.core.market import SessionCalendar
from doi.core.models import ChainRow, OptionChainSnapshot, OptionLeg
from doi.data.instruments.master import InstrumentMaster
from doi.intelligence.strategy_configs.loader import load_index_configs
from doi.paper.driver import find_leg, market_state, pump_once
from doi.paper.engine import ExecutionPolicy, PaperExecution
from doi.risk.models import RiskConfig
from tests.fixtures import HEADER

EXP = datetime.now(UTC).date() + timedelta(days=7)


class AlwaysOpen(SessionCalendar):
    def is_open(self, ts):  # tests must not depend on wall-clock market hours
        return True


def master() -> InstrumentMaster:
    rows = [HEADER, "NSE,I,13,NA,INDEX,13,NIFTY,NIFTY,Nifty 50,INDEX,NA,1,,,,0.05,"]
    for i, k in enumerate((23900, 23950, 24000, 24050, 24100)):
        for j, ot in enumerate(("CE", "PE")):
            rows.append(f'NSE,D,{70000 + i * 2 + j},NA,OPTIDX,13,NIFTY,x,"x",OP,NA,75,{EXP} 14:30:00,{k},{ot},0.05,W')
    return InstrumentMaster.parse("\n".join(rows) + "\n")


def leg(sid, bid, ask, qty=750):
    return OptionLeg(sid, (bid + ask) / 2, 50000, 40000, 20000, 12.0, 0.5, 0.001, -10, 12, bid, qty, ask, qty)


class ChainProvider:
    def __init__(self, master):
        self.ce = master.contract("NSE", "NIFTY", EXP, 24000, "CE")
        self.bid, self.ask, self.calls = 99.5, 100.0, 0

    async def get_expiries(self, u):
        return (EXP,)

    async def get_option_chain(self, u, expiry, *, max_age_s=None):
        self.calls += 1
        now = datetime.now(UTC)
        rows = (
            ChainRow(23950.0, leg(None, 120, 121), leg(None, 70, 71)),
            ChainRow(24000.0, leg(self.ce.security_id, self.bid, self.ask), leg(None, 95, 96)),
        )
        return OptionChainSnapshot("NIFTY", expiry, "k", now, now, 24010.0, rows, u.master_version)


def runtime():
    cfgs = load_index_configs(DEFAULT_STRATEGY_DIR)
    cfgs["NIFTY"] = cfgs["NIFTY"].model_copy(update={"live_enabled": True})
    m = master()
    rt = Runtime(configs=cfgs, provider=ChainProvider(m))
    rt.resolve(m)
    rt.paper = build_paper(
        cfgs,
        starting_capital=Decimal(500000),
        now=datetime.now(UTC) - timedelta(seconds=5),
        calendar=AlwaysOpen(),
        risk=RiskConfig(last_entry_time=time(23, 59, 59)),
        policy=ExecutionPolicy(square_off=None),
    )
    return rt


ORDER = {
    "signal_id": "SIG-1",
    "underlying": "NIFTY",
    "expiry": EXP.isoformat(),
    "strike": 24000,
    "option_type": "CE",
    "stop_loss": 90,
    "targets": [{"price": 125, "fraction": 0.5}, {"price": 135, "fraction": 0.5}],
}


def test_api_submit_duplicate_portfolio_ledger():
    rt = runtime()
    c = TestClient(create_app(runtime=rt))
    r = c.post("/api/v1/paper/orders", json=ORDER)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["approved"] and body["order"]["quantity"] == 150 and body["order"]["status"] == "PAPER_ORDER_PENDING"
    d = c.post("/api/v1/paper/orders", json=ORDER)
    assert d.status_code == 200 and d.json()["duplicate"] is True
    pf = c.get("/api/v1/paper/portfolio").json()
    assert pf["paper_only"] and len(pf["working_orders"]) == 1
    led = c.get("/api/v1/paper/ledger").json()
    assert led["head"] == rt.paper.ledger.head and led["events"][0]["kind"] == "ACCOUNT_OPENED"


def test_api_rejection_returns_reasons():
    rt = runtime()
    c = TestClient(create_app(runtime=rt))
    r = c.post("/api/v1/paper/orders", json={**ORDER, "signal_id": "SIG-2", "targets": [{"price": 101, "fraction": 1}]})
    assert r.status_code == 422 and any("reward_risk" in x for x in r.json()["detail"]["reasons"])
    bad = c.post("/api/v1/paper/orders", json={**ORDER, "signal_id": "SIG-3", "targets": [{"price": 125, "fraction": 0.4}]})
    assert bad.status_code == 422
    assert c.post("/api/v1/paper/orders", json={**ORDER, "strike": 24025}).status_code == 404


def test_driver_feeds_quotes_fills_and_exits():
    rt = runtime()
    c = TestClient(create_app(runtime=rt))
    assert c.post("/api/v1/paper/orders", json=ORDER).status_code == 201
    eng: PaperExecution = rt.paper
    import time as _t

    _t.sleep(0.3)  # beyond simulated latency
    assert asyncio.run(pump_once(rt)) == 1
    pos = next(iter(eng.positions.values()))
    assert pos.qty_open == 150
    rt.provider.bid, rt.provider.ask = 80.0, 80.5
    _t.sleep(0.01)
    asyncio.run(pump_once(rt))  # stop triggers
    _t.sleep(0.3)
    asyncio.run(pump_once(rt))  # exit fills
    assert eng.trades and eng.trades[0].exit_reason == "STOP_LOSS"
    assert c.get("/api/v1/paper/trades").json()[0]["exit_reason"] == "STOP_LOSS"
    eng.ledger.verify()


def test_close_route_and_conflict():
    rt = runtime()
    c = TestClient(create_app(runtime=rt))
    c.post("/api/v1/paper/orders", json=ORDER)
    import time as _t

    _t.sleep(0.3)
    asyncio.run(pump_once(rt))
    pid = next(iter(rt.paper.positions))
    r = c.post(f"/api/v1/paper/positions/{pid}/close")
    assert r.status_code == 200 and r.json()["exit_order"]["purpose"] == "EXIT"
    assert c.post("/api/v1/paper/positions/POS-999999/close").status_code == 409


def test_market_state_matches_by_strike_and_flags_missing():
    m = master()
    p = ChainProvider(m)
    snap = asyncio.run(p.get_option_chain(type("U", (), {"master_version": "v"})(), EXP))
    now = datetime.now(UTC)
    assert find_leg(snap, p.ce) is not None
    assert market_state(snap, p.ce, now, 10).quote.ask == 100.0
    pe = m.contract("NSE", "NIFTY", EXP, 24000, "PE")
    assert market_state(snap, pe, now, 10).quote is not None  # leg without sid -> matched by strike/type
    other = m.contract("NSE", "NIFTY", EXP, 24100, "CE")
    assert market_state(snap, other, now, 10).contract_ambiguous
    wrong = replace(p.ce, security_id="999")
    assert find_leg(snap, wrong) is None  # chain sid disagrees with master: refuse


def test_runtime_without_dhan_still_has_paper_engine(tmp_path):
    from doi.app.runtime import build_runtime
    from doi.core.config import Settings

    rt = asyncio.run(build_runtime(Settings(paper_ledger_path=tmp_path / "l.jsonl")))
    assert rt.paper is not None and rt.provider is None and (tmp_path / "l.jsonl").exists()


def test_db_backed_runtime_restart_with_polling_and_feed(pg_dsn):
    """Chain polling and the WebSocket collector both drive the persisted engine; a
    restart restores the identical state and recovery keeps it consistent."""
    from doi.app.runtime import init_paper
    from doi.core.config import Settings
    from doi.data.dhan.collector import MarketFeedCollector
    from doi.paper.reconcile import reconcile
    from doi.paper.store import PaperStore
    from tests.unit.test_feed import FakeWS, full_packet

    settings = Settings(database_url=pg_dsn, holidays_file=None)
    rt = runtime()
    asyncio.run(init_paper(rt, settings, datetime.now(UTC) - timedelta(seconds=5)))
    assert rt.paper_store is not None
    # make the restored engine permissive about wall-clock hours, like runtime() does
    rt.paper.gate.calendar = AlwaysOpen()
    rt.paper.gate.config = RiskConfig(last_entry_time=time(23, 59, 59))
    rt.paper.policy = ExecutionPolicy(square_off=None)
    c = TestClient(create_app(runtime=rt))
    assert c.post("/api/v1/paper/orders", json=ORDER).status_code == 401  # DB deployments always enforce auth
    _, token = rt.auth.create_key(rt.auth.create_user("approver@test"), "approver")
    assert c.post("/api/v1/paper/orders", json=ORDER, headers={"Authorization": f"Bearer {token}"}).status_code == 201
    assert [r.action for r in rt.approvals.records()] == ["PAPER_ORDER"]

    ce = rt.provider.ce
    ws = FakeWS([full_packet(sid=int(ce.security_id), bid=99.5, ask=100.0, bq=75, aq=75)])

    async def on_quote(q):
        await rt.paper_service.call(lambda e: e.process_market_event(q))

    import time as _t

    _t.sleep(0.3)
    feed = MarketFeedCollector("cid", lambda: "tok", on_quote, connect=lambda url: _ret(ws), backoff_base_s=0.001)

    async def stream():
        await feed.set_subscriptions({(ce.exchange_segment, ce.security_id)})
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.05)
        await feed.stop()
        task.cancel()

    asyncio.run(stream())
    order = rt.paper.orders["ORD-000001"]
    assert order.filled_qty == 75  # partial fill from feed depth (1 lot shown)
    _t.sleep(0.01)
    asyncio.run(pump_once(rt))  # chain polling completes the fill
    assert rt.paper.orders["ORD-000001"].filled_qty == 150

    restored = PaperStore(pg_dsn).load(rt.paper.account_id, rt.paper.gate, rt.paper.policy)
    assert restored.ledger.head == rt.paper.ledger.head and reconcile(restored) == []
    rt2 = runtime()
    asyncio.run(init_paper(rt2, settings, datetime.now(UTC)))  # second boot: load + recover
    assert rt2.paper.orders["ORD-000001"].filled_qty == 150 and reconcile(rt2.paper) == []
    assert "RECOVERY" in [ev.kind for ev in rt2.paper.ledger.events]


async def _ret(x):
    return x
