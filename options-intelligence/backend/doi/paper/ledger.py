"""Append-only, hash-chained paper ledger.

Every state change is written as an event. Each event's hash covers the previous
hash plus a canonical JSON of the event, so any edit, reordering or deletion
breaks `verify()`. Cash movements are kept as separate entries so the account
balance can always be reconciled. An optional JSONL sink keeps the audit
trail across restarts."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from doi.core.canonical import canonical
from doi.core.errors import DoiError

GENESIS = "0" * 64


class LedgerCorrupt(DoiError):
    pass


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    seq: int
    ts: datetime
    kind: str
    payload: dict
    prev_hash: str
    hash: str


@dataclass(frozen=True, slots=True)
class CashEntry:
    seq: int
    ts: datetime
    account_id: str
    kind: str  # PREMIUM_DEBIT / PREMIUM_CREDIT / CHARGES / DEPOSIT
    amount: Decimal  # signed
    ref: str
    balance_after: Decimal


def _digest(prev: str, seq: int, ts: datetime, kind: str, payload: dict) -> str:
    body = canonical({"seq": seq, "ts": ts, "kind": kind, "payload": payload})
    return hashlib.sha256((prev + body).encode()).hexdigest()


class Ledger:
    def __init__(self, sink: Path | None = None) -> None:
        self.events: list[LedgerEvent] = []
        self.cash: list[CashEntry] = []
        self._sink = sink

    @property
    def head(self) -> str:
        return self.events[-1].hash if self.events else GENESIS

    def append(self, kind: str, ts: datetime, payload: dict) -> LedgerEvent:
        payload = json.loads(canonical(payload))  # freeze to plain JSON types
        seq = len(self.events) + 1
        ev = LedgerEvent(seq, ts, kind, payload, self.head, _digest(self.head, seq, ts, kind, payload))
        self.events.append(ev)
        if self._sink is not None:
            with self._sink.open("a", encoding="utf-8") as f:
                f.write(canonical(dataclasses.asdict(ev)) + "\n")
        return ev

    def post_cash(self, ts: datetime, account_id: str, kind: str, amount: Decimal, ref: str) -> CashEntry:
        bal = (self.cash[-1].balance_after if self.cash else Decimal(0)) + amount
        entry = CashEntry(len(self.cash) + 1, ts, account_id, kind, amount, ref, bal)
        self.cash.append(entry)
        return entry

    def cash_balance(self) -> Decimal:
        return self.cash[-1].balance_after if self.cash else Decimal(0)

    def verify(self) -> None:
        prev = GENESIS
        for i, ev in enumerate(self.events, start=1):
            if ev.seq != i or ev.prev_hash != prev or ev.hash != _digest(prev, ev.seq, ev.ts, ev.kind, ev.payload):
                raise LedgerCorrupt(f"ledger chain broken at seq {i}")
            prev = ev.hash
        running = Decimal(0)
        for c in self.cash:
            running += c.amount
            if running != c.balance_after:
                raise LedgerCorrupt(f"cash ledger broken at entry {c.seq}")

    def since(self, after_seq: int = 0) -> list[LedgerEvent]:
        return self.events[after_seq:]
