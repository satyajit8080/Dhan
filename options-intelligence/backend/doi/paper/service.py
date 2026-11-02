"""Serialised, persisted access to the paper engine.

Every mutation runs under one asyncio lock: engine operation, then a single
database transaction. If the save fails, the in-memory engine is discarded and
reloaded from the database, so memory never runs ahead of durable state. A
`ConcurrentModification` (another process saved first) triggers one reload and
retry. Duplicate signals are still caught after the reload by idempotency keys.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from doi.paper.engine import PaperExecution
from doi.paper.reconcile import assert_consistent
from doi.paper.store import ConcurrentModification, PaperStore, collect_changes, mark_persisted

log = logging.getLogger(__name__)
T = TypeVar("T")


class PaperService:
    def __init__(self, holder, store: PaperStore | None = None) -> None:
        """`holder` has a mutable `.paper` attribute (the Runtime), so a reload
        replaces the engine every caller sees."""
        self._holder = holder
        self.store = store
        self._lock = asyncio.Lock()

    @property
    def engine(self) -> PaperExecution:
        return self._holder.paper

    async def _reload(self) -> None:
        e = self.engine
        fresh = await asyncio.to_thread(self.store.load, e.account_id, e.gate, e.policy)  # type: ignore[union-attr]
        if fresh is None:
            raise RuntimeError(f"paper account {e.account_id} missing from database")
        self._holder.paper = fresh

    async def call(self, op: Callable[[PaperExecution], T], *, retries: int = 1) -> T:
        async with self._lock:
            for attempt in range(retries + 1):
                e = self.engine
                try:
                    result = op(e)
                    if self.store is not None:
                        cs = collect_changes(e)
                        await asyncio.to_thread(self.store.save, e, cs)
                        mark_persisted(e, cs)
                    return result
                except ConcurrentModification:
                    log.warning("paper account changed by another writer; reloading (attempt %d)", attempt + 1)
                    await self._reload()
                    if attempt == retries:
                        raise
                except Exception:
                    if self.store is not None:
                        await self._reload()  # discard un-persisted in-memory changes
                    raise
        raise AssertionError("unreachable")

    async def flush(self) -> None:
        await self.call(lambda e: None)

    async def recover(self, now: datetime) -> list[str]:
        assert_consistent(self.engine)
        actions = await self.call(lambda e: e.recover(now))
        assert_consistent(self.engine)
        return actions
