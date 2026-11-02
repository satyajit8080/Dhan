"""PostgreSQL persistence for the paper engine.

Write path: after each engine operation, `collect_changes` diffs the engine
against what was last persisted, and `PaperStore.save` writes the whole change
set in ONE transaction:
  - the account row is locked (SELECT ... FOR UPDATE) and its optimistic
    `version` must match, so two processes cannot interleave writes;
  - the new ledger events must continue the stored hash chain exactly;
  - after the inserts, cash entries are re-summed in SQL and must equal the
    account cash, or the transaction rolls back.
Any failure leaves the database at the previous consistent state, and the
caller reloads the engine from it (see doi.paper.service).

Read path: `load` rebuilds the engine from the tables, verifies the ledger
chain and cash running balance, and returns it ready for `recover()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from doi.core.errors import DoiError
from doi.paper.codec import (
    decision_from,
    decision_to,
    fill_from,
    fill_to,
    order_from,
    order_to,
    position_from,
    position_to,
    trade_from,
    trade_to,
)
from doi.paper.engine import ExecutionPolicy, PaperExecution, SubmitResult
from doi.paper.ledger import GENESIS, CashEntry, Ledger, LedgerEvent, canonical
from doi.risk.gate import RiskGate


class ConcurrentModification(DoiError):
    """Another writer saved this account first; reload and retry."""


class PersistenceError(DoiError):
    pass


@dataclass
class ChangeSet:
    account_id: str
    starting_capital: Decimal
    cash: Decimal
    state: dict
    events: list[LedgerEvent]
    cash_entries: list[CashEntry]
    orders: list[dict] = field(default_factory=list)
    positions: list[dict] = field(default_factory=list)
    deleted_positions: list[str] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    submissions: list[dict] = field(default_factory=list)
    cache: dict[str, str | None] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.events or self.cash_entries or self.orders or self.positions or self.deleted_positions or self.fills)


def engine_state(e: PaperExecution) -> dict:
    return {
        "counters": dict(e._n),
        "consecutive_losses": e.consecutive_losses,
        "last_loss_at": e.last_loss_at.isoformat() if e.last_loss_at else None,
        "kill_switch": e.kill_switch,
        "daily_start_equity": str(e.daily_start_equity),
        "session": e._session.isoformat(),
        "last_ts": {k: v.isoformat() for k, v in e._last_ts.items()},
        "ignored_events": e.ignored_events,
        "starting_capital": str(e.starting_capital),
    }


def collect_changes(e: PaperExecution) -> ChangeSet:
    cs = ChangeSet(
        e.account_id,
        e.starting_capital,
        e.cash,
        engine_state(e),
        e.ledger.events[e._persisted_events :],
        e.ledger.cash[e._persisted_cash :],
    )
    cache = e._persist_cache

    def diff(key: str, d: dict, bucket: list) -> None:
        s = canonical(d)
        if cache.get(key) != s:
            bucket.append(d)
            cs.cache[key] = s

    for o in sorted(e.orders.values(), key=lambda o: o.id):
        diff(f"o:{o.id}", order_to(o), cs.orders)
    live_ids = set()
    for p in sorted(list(e.positions.values()) + list(e._planned.values()), key=lambda p: p.id):
        live_ids.add(p.id)
        diff(f"p:{p.id}", position_to(p, planned=p.id in e._planned), cs.positions)
    for key in [k for k in cache if k.startswith("p:") and k[2:] not in live_ids]:
        cs.deleted_positions.append(key[2:])  # planned entry abandoned before any fill
        cs.cache[key] = None
    for f in sorted(e.fills.values(), key=lambda f: f.id):
        if f"f:{f.id}" not in cache:
            cs.fills.append(fill_to(f))
            cs.cache[f"f:{f.id}"] = "1"
    for t in e.trades:
        if f"t:{t.position_id}" not in cache:
            cs.trades.append(trade_to(t))
            cs.cache[f"t:{t.position_id}"] = "1"
    for key, res in e._submissions.items():
        if f"s:{key}" not in cache:
            cs.submissions.append(
                {
                    "key": key,
                    "signal_id": key.split(":", 1)[1],
                    "approved": bool(res.decision and res.decision.approved),
                    "order_id": res.order.id if res.order else None,
                    "decision": decision_to(res.decision),
                }
            )
            cs.cache[f"s:{key}"] = "1"
    return cs


def mark_persisted(e: PaperExecution, cs: ChangeSet) -> None:
    for k, v in cs.cache.items():
        if v is None:
            e._persist_cache.pop(k, None)
        else:
            e._persist_cache[k] = v
    e._persisted_events += len(cs.events)
    e._persisted_cash += len(cs.cash_entries)


class PaperStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn)

    def exists(self, account_id: str) -> bool:
        with self._connect() as c:
            return c.execute("SELECT 1 FROM paper_accounts WHERE id = %s", (account_id,)).fetchone() is not None

    def save(self, e: PaperExecution, cs: ChangeSet) -> None:
        if cs.empty and e._db_version is not None:
            return
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT version, ledger_head, event_count FROM paper_accounts WHERE id = %s FOR UPDATE", (cs.account_id,)
            ).fetchone()
            if row is None:
                if e._db_version is not None:
                    raise ConcurrentModification(f"account {cs.account_id} vanished")
                head, count = GENESIS, 0
                conn.execute(
                    "INSERT INTO paper_accounts (id, starting_capital, cash, state, ledger_head, event_count, version)"
                    " VALUES (%s, %s, %s, %s, %s, 0, 0)",
                    (cs.account_id, cs.starting_capital, cs.cash, Jsonb(cs.state), GENESIS),
                )
                version = 0
            else:
                version, head, count = row
                if e._db_version is None or version != e._db_version:
                    raise ConcurrentModification(f"account {cs.account_id}: stored v{version}, engine v{e._db_version}")
            if cs.events and (cs.events[0].prev_hash != head or cs.events[0].seq != count + 1):
                raise ConcurrentModification("ledger chain does not continue the stored head")

            cur = conn.cursor()
            if cs.events:
                cur.executemany(
                    "INSERT INTO paper_ledger_events (account_id, seq, ts, ts_iso, kind, payload_json, payload, prev_hash, hash)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            cs.account_id,
                            ev.seq,
                            ev.ts,
                            ev.ts.isoformat(),
                            ev.kind,
                            canonical(ev.payload),
                            Jsonb(ev.payload),
                            ev.prev_hash,
                            ev.hash,
                        )
                        for ev in cs.events
                    ],
                )
            if cs.cash_entries:
                cur.executemany(
                    "INSERT INTO paper_cash_entries (account_id, seq, ts, kind, amount, ref, balance_after, amount_txt, balance_txt)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            cs.account_id,
                            c.seq,
                            c.ts,
                            c.kind,
                            c.amount,
                            c.ref,
                            c.balance_after,
                            str(c.amount),
                            str(c.balance_after),
                        )
                        for c in cs.cash_entries
                    ],
                )
            for o in cs.orders:
                cur.execute(
                    "INSERT INTO paper_orders (account_id, id, idempotency_key, signal_id, position_id, purpose, side, security_id,"
                    " quantity, filled_qty, status, data) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (account_id, id) DO UPDATE SET filled_qty = EXCLUDED.filled_qty, status = EXCLUDED.status,"
                    " data = EXCLUDED.data",
                    (
                        cs.account_id, o["id"], o["idempotency_key"], o["signal_id"], o["position_id"], o["purpose"], o["side"],
                        o["contract"]["security_id"], o["quantity"], o["filled_qty"], o["status"], Jsonb(o),
                    ),
                )  # fmt: skip
            if cs.deleted_positions:
                cur.execute(
                    "DELETE FROM paper_positions WHERE account_id = %s AND id = ANY(%s) AND state = 'PLANNED'",
                    (cs.account_id, cs.deleted_positions),
                )
            for p in cs.positions:
                cur.execute(
                    "INSERT INTO paper_positions (account_id, id, signal_id, underlying_key, security_id, state, qty_entered,"
                    " qty_exited, data) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (account_id, id) DO UPDATE SET state = EXCLUDED.state, qty_entered = EXCLUDED.qty_entered,"
                    " qty_exited = EXCLUDED.qty_exited, data = EXCLUDED.data",
                    (
                        cs.account_id, p["id"], p["signal_id"], p["underlying_key"], p["contract"]["security_id"], p["state"],
                        p["qty_entered"], p["qty_exited"], Jsonb(p),
                    ),
                )  # fmt: skip
            if cs.fills:
                cur.executemany(
                    "INSERT INTO paper_fills (account_id, id, order_id, idempotency_key, ts, side, price, quantity, charges_total, data)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            cs.account_id, f["id"], f["order_id"], f["idempotency_key"], f["ts"], f["side"], f["price"],
                            f["quantity"], sum(Decimal(v) for k, v in f["charges"].items() if k != "schedule_version"), Jsonb(f),
                        )
                        for f in cs.fills
                    ],
                )  # fmt: skip
            if cs.trades:
                cur.executemany(
                    "INSERT INTO paper_trades (account_id, position_id, net_pnl, gross_pnl, charges, closed_at, data)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (cs.account_id, t["position_id"], t["net_pnl"], t["gross_pnl"], t["charges"], t["closed_at"], Jsonb(t))
                        for t in cs.trades
                    ],
                )
            if cs.submissions:
                cur.executemany(
                    "INSERT INTO paper_submissions (account_id, idempotency_key, signal_id, approved, order_id, decision)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (
                            cs.account_id,
                            s["key"],
                            s["signal_id"],
                            s["approved"],
                            s["order_id"],
                            Jsonb(s["decision"]) if s["decision"] else None,
                        )
                        for s in cs.submissions
                    ],
                )
            new_head = cs.events[-1].hash if cs.events else head
            cur.execute(
                "UPDATE paper_accounts SET cash = %s, state = %s, ledger_head = %s, event_count = %s, version = version + 1,"
                " updated_at = now() WHERE id = %s",
                (cs.cash, Jsonb(cs.state), new_head, count + len(cs.events), cs.account_id),
            )
            total, last = cur.execute(
                "SELECT COALESCE(SUM(amount), 0),"
                " (SELECT balance_after FROM paper_cash_entries WHERE account_id = %s ORDER BY seq DESC LIMIT 1)"
                " FROM paper_cash_entries WHERE account_id = %s",
                (cs.account_id, cs.account_id),
            ).fetchone()
            if total != cs.cash or last != cs.cash:
                raise PersistenceError(f"cash mismatch after save: entries {total}, last {last}, engine {cs.cash}")
            self._before_commit_hook()
        e._db_version = version + 1

    def _before_commit_hook(self) -> None:
        """Test seam for crash injection; no-op in production."""

    def load(self, account_id: str, gate: RiskGate, policy: ExecutionPolicy | None = None) -> PaperExecution | None:
        with self._connect() as c:
            acct = c.execute(
                "SELECT starting_capital, cash, state, ledger_head, event_count, version FROM paper_accounts WHERE id = %s",
                (account_id,),
            ).fetchone()
            if acct is None:
                return None
            _starting, cash, state, head, count, version = acct
            events = c.execute(
                "SELECT seq, ts_iso, kind, payload_json, prev_hash, hash FROM paper_ledger_events WHERE account_id = %s ORDER BY seq",
                (account_id,),
            ).fetchall()
            cash_rows = c.execute(
                "SELECT seq, ts, kind, amount_txt, ref, balance_txt FROM paper_cash_entries WHERE account_id = %s ORDER BY seq",
                (account_id,),
            ).fetchall()
            orders = [r[0] for r in c.execute("SELECT data FROM paper_orders WHERE account_id = %s ORDER BY id", (account_id,))]
            positions = [
                r[0] for r in c.execute("SELECT data FROM paper_positions WHERE account_id = %s ORDER BY id", (account_id,))
            ]
            fills = [r[0] for r in c.execute("SELECT data FROM paper_fills WHERE account_id = %s ORDER BY id", (account_id,))]
            trades = [
                r[0]
                for r in c.execute(
                    "SELECT data FROM paper_trades WHERE account_id = %s ORDER BY closed_at, position_id", (account_id,)
                )
            ]
            subs = c.execute(
                "SELECT idempotency_key, order_id, decision FROM paper_submissions WHERE account_id = %s ORDER BY created_at, idempotency_key",
                (account_id,),
            ).fetchall()

        import json

        ledger = Ledger()
        ledger.events = [LedgerEvent(s, datetime.fromisoformat(ts), k, json.loads(pj), ph, h) for s, ts, k, pj, ph, h in events]
        ledger.cash = [CashEntry(s, ts, account_id, k, Decimal(a), r, Decimal(b)) for s, ts, k, a, r, b in cash_rows]
        ledger.verify()  # raises LedgerCorrupt on any tampering
        if len(ledger.events) != count or ledger.head != head.strip():
            raise PersistenceError("ledger rows do not match the account head/count")
        if ledger.cash_balance() != cash:
            raise PersistenceError(f"cash rows sum {ledger.cash_balance()} != account cash {cash}")

        e = PaperExecution.restored(
            account_id=account_id,
            starting_capital=Decimal(state["starting_capital"]),
            gate=gate,
            ledger=ledger,
            session=date.fromisoformat(state["session"]),
            daily_start_equity=Decimal(state["daily_start_equity"]),
            policy=policy,
        )
        for k, v in state["counters"].items():
            e._n[k] = v
        e.consecutive_losses = state["consecutive_losses"]
        e.last_loss_at = datetime.fromisoformat(state["last_loss_at"]) if state["last_loss_at"] else None
        e.kill_switch = state["kill_switch"]
        e.ignored_events = state["ignored_events"]
        e._last_ts = {k: datetime.fromisoformat(v) for k, v in state["last_ts"].items()}
        for d in orders:
            o = order_from(d)
            e.orders[o.id] = o
            e._persist_cache[f"o:{o.id}"] = canonical(order_to(o))
        for d in positions:
            p, planned = position_from(d)
            (e._planned if planned else e.positions)[p.id] = p
            e._persist_cache[f"p:{p.id}"] = canonical(position_to(p, planned=planned))
        for d in fills:
            f = fill_from(d)
            e.fills[f.id] = f
            e._fill_keys.add(f.idempotency_key)
            e._persist_cache[f"f:{f.id}"] = "1"
        for d in trades:
            t = trade_from(d)
            e.trades.append(t)
            e._persist_cache[f"t:{t.position_id}"] = "1"
        for key, order_id, dec in subs:
            e._submissions[key] = SubmitResult(decision_from(dec), e.orders.get(order_id) if order_id else None)
            e._persist_cache[f"s:{key}"] = "1"
        e._persisted_events = len(ledger.events)
        e._persisted_cash = len(ledger.cash)
        e._db_version = version
        return e
