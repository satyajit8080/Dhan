"""Feed live chain snapshots into the paper engine. Read-only: it only calls
MarketDataProvider.get_option_chain (through the request-key scheduler)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from doi.core.errors import DoiError
from doi.core.market import Quote
from doi.core.models import Instrument, OptionChainSnapshot, OptionLeg
from doi.paper.engine import PaperExecution
from doi.risk.models import MarketState

log = logging.getLogger(__name__)


def find_leg(snap: OptionChainSnapshot, contract: Instrument) -> OptionLeg | None:
    for row in snap.rows:
        if contract.strike is not None and abs(row.strike - contract.strike) < 1e-6:
            leg = row.ce if contract.option_type == "CE" else row.pe
            if leg is not None and leg.security_id not in (None, contract.security_id):
                return None  # chain and master disagree: never trade on a mismatched leg
            return leg
    return None


def quote_from_leg(leg: OptionLeg, contract: Instrument, snap: OptionChainSnapshot) -> Quote:
    return Quote(
        security_id=contract.security_id,
        ts=snap.source_ts or snap.received_at,
        bid=leg.bid,
        bid_qty=leg.bid_qty,
        ask=leg.ask,
        ask_qty=leg.ask_qty,
        ltp=leg.ltp,
        underlying_ltp=snap.underlying_ltp,
        oi=leg.oi,
        volume=leg.volume,
    )


def chain_quality(snap: OptionChainSnapshot, now: datetime, max_age_s: float) -> tuple[float, bool]:
    """(score 0-100, stale). Score = share of legs with a two-sided quote; 0 when stale."""
    legs = [leg for r in snap.rows for leg in (r.ce, r.pe) if leg is not None]
    stale = snap.age_s(now) > max_age_s
    if stale or not legs:
        return 0.0, stale or not legs
    return 100.0 * sum(1 for leg in legs if leg.two_sided) / len(legs), False


def market_state(snap: OptionChainSnapshot, contract: Instrument, now: datetime, max_age_s: float) -> MarketState:
    score, stale = chain_quality(snap, now, max_age_s)
    leg = find_leg(snap, contract)
    return MarketState(
        quote=quote_from_leg(leg, contract, snap) if leg else None,
        data_quality_score=score,
        data_stale=stale,
        contract_ambiguous=leg is None,
    )


async def pump_once(rt, now: datetime | None = None) -> int:
    """One pass: fetch each watched chain once and feed matching quotes. Returns quotes fed."""
    engine: PaperExecution | None = rt.paper
    if engine is None or rt.provider is None:
        return 0
    fed = 0
    for (key, expiry), contracts in engine.watch().items():
        ref, cfg = rt.universe.get(key), rt.configs.get(key)
        if ref is None or cfg is None:
            continue
        try:
            snap = await rt.provider.get_option_chain(ref, expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)
        except DoiError as e:
            log.warning("paper feed %s %s: %s", key, expiry, type(e).__name__)
            continue
        for c in contracts:
            leg = find_leg(snap, c)
            if leg is not None:
                engine.process_market_event(quote_from_leg(leg, c, snap))
                fed += 1
    engine.advance_time(now or datetime.now(UTC))
    return fed


async def run_forever(rt, interval_s: float = 3.0) -> None:
    while True:
        try:
            await pump_once(rt)
        except Exception:  # keep the feed alive; the ledger records every state change
            log.exception("paper feed pass failed")
        await asyncio.sleep(interval_s)
