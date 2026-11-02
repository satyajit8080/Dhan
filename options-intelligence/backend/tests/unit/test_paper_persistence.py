"""PostgreSQL persistence: restore, crash recovery, reconciliation, idempotency and
concurrency. Uses the `pg_dsn` fixture (real PostgreSQL), and skips without one."""

import asyncio
import shutil
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import psycopg
import pytest

from doi.core.errors import ConfigError
from doi.db.migrate import DEFAULT_DIR, discover, migrate
from doi.paper.engine import PaperExecution
from doi.paper.ledger import LedgerCorrupt
from doi.paper.models import ExitReason, OrderStatus, PositionState
from doi.paper.reconcile import assert_consistent, project_ledger, reconcile
from doi.paper.service import PaperService
from doi.paper.store import ConcurrentModification, PaperStore, PersistenceError, collect_changes
from tests.unit.test_paper import BNF, CE, PE, T0, cand, engine, mkt, q, t


def make(dsn, e=None):
    holder = SimpleNamespace(paper=e or engine())
    return PaperService(holder, PaperStore(dsn))


def load(dsn, like: PaperExecution) -> PaperExecution:
    return PaperStore(dsn).load(like.account_id, like.gate, like.policy)


def rows(dsn, table):
    with psycopg.connect(dsn) as c:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def state(e: PaperExecution):
    return (
        e.ledger.head,
        e.cash,
        sorted((o.id, o.status, o.filled_qty) for o in e.orders.values()),
        sorted((p.id, p.state, p.qty_entered, p.qty_exited, p.mark) for p in e.positions.values()),
        sorted(e._planned),
        sorted(e.fills),
        e.trades,
        e.consecutive_losses,
        dict(e._n),
        dict(e._last_ts),
        e.daily_start_equity,
    )


async def scenario(svc: PaperService):
    await svc.flush()
    await svc.call(lambda e: e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)))
    await svc.call(lambda e: e.process_market_event(q(1, 99.5, 100.0, aq=75)))  # partial
    await svc.call(lambda e: e.process_market_event(q(2, 99.5, 100.0)))  # complete
    await svc.call(lambda e: e.process_market_event(q(5, 125.0, 125.5)))  # target 1 triggers
    await svc.call(lambda e: e.process_market_event(q(6, 125.0, 125.5)))  # target 1 fills
    await svc.call(
        lambda e: e.submit_simulated(cand("S2", contract=BNF, key="BANKNIFTY"), mkt(q(7, 99.5, 100.0, sec="60001")), t(7))
    )


# ---------------- migrations ----------------


def test_migrations_idempotent_and_checksummed(pg_dsn, tmp_path):
    assert migrate(pg_dsn) == []
    assert [v for v, _, _ in discover()] == ["001", "002"]
    d = tmp_path / "m"
    shutil.copytree(DEFAULT_DIR, d)
    (d / "002_paper_persistence.sql").write_text((d / "002_paper_persistence.sql").read_text() + "\n-- edited\n")
    with pytest.raises(ConfigError, match="changed"):
        migrate(pg_dsn, d)
    (d / "003_x.sql").write_text("SELECT 1;")
    (d / "bad.sql").write_text("SELECT 1;")
    with pytest.raises(ConfigError, match="bad migration"):
        discover(d)


def test_failed_migration_rolls_back(pg_dsn, tmp_path):
    d = tmp_path / "m"
    shutil.copytree(DEFAULT_DIR, d)
    (d / "003_broken.sql").write_text("CREATE TABLE half_done (x int); SELECT * FROM no_such_table;")
    with pytest.raises(psycopg.Error):
        migrate(pg_dsn, d)
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT to_regclass('half_done')").fetchone()[0] is None
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == "002"


# ---------------- restore ----------------


def test_restore_after_restart_is_exact(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    before = svc.engine
    after = load(pg_dsn, before)
    assert state(after) == state(before)
    assert after.portfolio(t(8)) == before.portfolio(t(8))
    assert_consistent(after)
    assert rows(pg_dsn, "paper_ledger_events") == len(before.ledger.events)
    assert rows(pg_dsn, "paper_fills") == len(before.fills) == 3


def test_continuing_after_restart_matches_uninterrupted_run(pg_dsn):
    """Deterministic replay across a restart: same inputs give the same ledger hash."""
    uninterrupted = make(pg_dsn)
    asyncio.run(scenario(uninterrupted))
    tail = [q(10, 80.0, 80.5), q(11, 80.0, 80.5)]
    for x in tail:
        uninterrupted.engine.process_market_event(x)
    expected = uninterrupted.engine.ledger.head

    with psycopg.connect(pg_dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    migrate(pg_dsn)
    first = make(pg_dsn)
    asyncio.run(scenario(first))
    restarted = make(pg_dsn, load(pg_dsn, first.engine))

    async def go():
        for x in tail:
            await restarted.call(lambda e, x=x: e.process_market_event(x))

    asyncio.run(go())
    assert restarted.engine.ledger.head == expected
    assert load(pg_dsn, restarted.engine).ledger.head == expected


def test_duplicate_order_prevented_after_restart(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    restarted = make(pg_dsn, load(pg_dsn, svc.engine))
    res = asyncio.run(restarted.call(lambda e: e.submit_simulated(cand(), mkt(q(20, 99.5, 100.0)), t(20))))
    assert res.duplicate and res.order.id == "ORD-000001"
    assert rows(pg_dsn, "paper_submissions") == 2 and rows(pg_dsn, "paper_orders") == 3


def test_database_rejects_duplicate_idempotency_key(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    with psycopg.connect(pg_dsn) as c, pytest.raises(psycopg.errors.UniqueViolation):
        c.execute(
            "INSERT INTO paper_orders (account_id, id, idempotency_key, signal_id, position_id, purpose, side, security_id,"
            " quantity, filled_qty, status, data) VALUES ('A', 'ORD-999', 'A:S1', 'S1', 'POS-9', 'ENTRY', 'BUY', '1', 75, 0,"
            " 'PAPER_ORDER_PENDING', '{}')"
        )


# ---------------- crash recovery ----------------


def test_crash_before_commit_rolls_back_and_replays_cleanly(pg_dsn):
    svc = make(pg_dsn)

    async def go():
        await svc.flush()
        await svc.call(lambda e: e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)))
        pre = state(svc.engine)
        boom = {"n": 1}

        def crash():
            if boom["n"]:
                boom["n"] -= 1
                raise RuntimeError("power cut")

        svc.store._before_commit_hook = crash
        with pytest.raises(RuntimeError):
            await svc.call(lambda e: e.process_market_event(q(1, 99.5, 100.0)))
        assert state(svc.engine) == pre  # memory reloaded from the last committed state
        assert rows(pg_dsn, "paper_fills") == 0
        await svc.call(lambda e: e.process_market_event(q(1, 99.5, 100.0)))  # redelivered tick
        await svc.call(lambda e: e.process_market_event(q(1, 99.5, 100.0)))  # duplicate tick
        return svc.engine

    e = asyncio.run(go())
    assert len(e.fills) == 1 and rows(pg_dsn, "paper_fills") == 1
    assert_consistent(load(pg_dsn, e))


def test_cash_mismatch_aborts_transaction(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(svc.flush())
    e = svc.engine
    last = e.ledger.cash[-1]
    e.ledger.cash.append(last.__class__(last.seq + 1, t(1), "A", "DEPOSIT", D(1), "phantom", last.balance_after + 5))

    async def go():
        with pytest.raises(PersistenceError, match="cash mismatch"):
            await svc.call(lambda en: None)

    asyncio.run(go())
    assert rows(pg_dsn, "paper_cash_entries") == 1  # rolled back
    assert load(pg_dsn, svc.engine).cash == e.starting_capital


def test_tampered_ledger_or_cash_refused_on_load(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    with psycopg.connect(pg_dsn) as c:
        c.execute("UPDATE paper_ledger_events SET payload_json = replace(payload_json, '100.05', '90.05') WHERE kind = 'FILL'")
    with pytest.raises(LedgerCorrupt):
        load(pg_dsn, svc.engine)


def test_tampered_cash_refused_on_load(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    with psycopg.connect(pg_dsn) as c:
        c.execute("UPDATE paper_accounts SET cash = cash + 1000")
    with pytest.raises(PersistenceError):
        load(pg_dsn, svc.engine)


def test_recovery_after_downtime_expires_entries(pg_dsn):
    svc = make(pg_dsn)

    async def go():
        await svc.flush()
        await svc.call(lambda e: e.submit_simulated(cand(valid_s=30), mkt(q(0, 99.5, 100.0)), t(0)))

    asyncio.run(go())
    assert rows(pg_dsn, "paper_positions") == 1  # PLANNED
    restarted = make(pg_dsn, load(pg_dsn, svc.engine))
    actions = asyncio.run(restarted.recover(t(120)))
    e = restarted.engine
    assert actions == [] and e.orders["ORD-000001"].status is OrderStatus.EXPIRED and not e._planned
    assert rows(pg_dsn, "paper_positions") == 0  # abandoned plan deleted in the same transaction
    assert "RECOVERY" in [ev.kind for ev in load(pg_dsn, e).ledger.events]


def test_recovery_squares_off_position_carried_to_next_session(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    next_day = T0 + timedelta(days=1)  # Tuesday 10:00 IST
    restarted = make(pg_dsn, load(pg_dsn, svc.engine))
    asyncio.run(restarted.recover(next_day))
    e = restarted.engine
    live = [p for p in e.positions.values() if p.state is not PositionState.EVALUATED]
    assert live and all(p.exit_reason is ExitReason.TIME_SQUARE_OFF for p in live)
    assert not e._planned  # the S2 entry expired while down
    assert_consistent(load(pg_dsn, e))


# ---------------- concurrency ----------------


def test_concurrent_submissions_serialised_without_duplicates(pg_dsn):
    svc = make(pg_dsn)

    async def go():
        await svc.flush()
        jobs = []
        for i in range(12):
            sig, contract, key, sec = [
                ("S1", CE, "NIFTY", "50001"),
                ("S2", BNF, "BANKNIFTY", "60001"),
                ("S3", PE, "NIFTY", "50002"),
            ][i % 3]
            jobs.append(
                svc.call(
                    lambda e, s=sig, c=contract, k=key, x=sec, i=i: e.submit_simulated(
                        cand(s, contract=c, key=k), mkt(q(0.01 * i, 99.5, 100.0, sec=x)), t(0.01 * i)
                    )
                )
            )
        return await asyncio.gather(*jobs)

    results = asyncio.run(go())
    assert sum(not r.duplicate for r in results) == 3
    assert rows(pg_dsn, "paper_submissions") == 3
    approved = {r.order.id for r in results if r.order}
    assert rows(pg_dsn, "paper_orders") == len(approved) == 2  # S3 rejected: NIFTY group already exposed
    assert_consistent(load(pg_dsn, svc.engine))


def test_concurrent_quotes_and_partial_fills(pg_dsn):
    svc = make(pg_dsn)

    async def go():
        await svc.flush()
        await svc.call(lambda e: e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0)))
        ticks = [q(1 + i * 0.1, 99.5, 100.0, aq=75) for i in range(6)]
        ticks += ticks[:3]  # redelivered duplicates
        await asyncio.gather(*(svc.call(lambda e, x=x: e.process_market_event(x)) for x in ticks))

    asyncio.run(go())
    e = svc.engine
    o = e.orders["ORD-000001"]
    assert o.status is OrderStatus.FILLED and o.filled_qty == 150 and len(o.fills) == 2
    restored = load(pg_dsn, e)
    assert state(restored) == state(e)
    assert_consistent(restored)


def test_two_processes_optimistic_lock(pg_dsn):
    a = make(pg_dsn)
    asyncio.run(a.flush())
    b = make(pg_dsn, load(pg_dsn, a.engine))
    asyncio.run(a.call(lambda e: e.submit_simulated(cand(), mkt(q(0, 99.5, 100.0)), t(0))))
    # b is stale: its save conflicts, it reloads and retries. The retry sees S1 as a duplicate.
    res = asyncio.run(b.call(lambda e: e.submit_simulated(cand(), mkt(q(0.5, 99.5, 100.0)), t(0.5))))
    assert res.duplicate and rows(pg_dsn, "paper_orders") == 1
    stale = load(pg_dsn, a.engine)
    asyncio.run(
        a.call(lambda e: e.submit_simulated(cand("S2", contract=BNF, key="BANKNIFTY"), mkt(q(1, 99.5, 100.0, sec="60001")), t(1)))
    )
    stale.submit_simulated(cand("S9", contract=PE), mkt(q(2, 99.5, 100.0, sec="50002")), t(2))
    with pytest.raises(ConcurrentModification):  # a direct stale write is refused, never merged
        PaperStore(pg_dsn).save(stale, collect_changes(stale))
    assert_consistent(load(pg_dsn, a.engine))
    assert rows(pg_dsn, "paper_submissions") == 2


def test_projection_matches_restored_engine(pg_dsn):
    svc = make(pg_dsn)
    asyncio.run(scenario(svc))
    e = load(pg_dsn, svc.engine)
    pr = project_ledger(e.ledger)
    assert pr.cash == e.cash and set(pr.fill_ids) == set(e.fills) and reconcile(e) == []
