"""Feed live chain snapshots into the paper engine. Read-only: it only calls
MarketDataProvider.get_option_chain (through the request-key scheduler)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

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
    """One pass: fetch each watched chain once, then apply all quotes plus the
    clock in ONE serialised engine call (one database transaction)."""
    if rt.paper is None or rt.provider is None:
        return 0
    quotes: list[Quote] = []
    for (key, expiry), contracts in rt.paper.watch().items():
        ref, cfg = rt.universe.get(key), rt.configs.get(key)
        if ref is None or cfg is None:
            continue
        try:
            snap = await rt.provider.get_option_chain(ref, expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)
        except DoiError as e:
            log.warning("paper feed %s %s: %s", key, expiry, type(e).__name__)
            continue
        await asyncio.to_thread(rt.recorder.snapshot, snap)  # isolated: never raises
        for c in contracts:
            leg = find_leg(snap, c)
            if leg is not None:
                quotes.append(quote_from_leg(leg, c, snap))
    clock = now or datetime.now(UTC)

    def apply(e: PaperExecution) -> None:
        for q in quotes:
            e.process_market_event(q)
        e.advance_time(clock)

    await rt.paper_service.call(apply)
    return len(quotes)


def feed_subscriptions(engine: PaperExecution) -> set[tuple[str, str]]:
    return {(c.exchange_segment, c.security_id) for cs in engine.watch().values() for c in cs}


async def record_chains_once(rt) -> int:
    """Record the nearest-expiry chain of every live-enabled index, so the feature engine and the
    AI committee have option data even with no paper position open. Failures are isolated."""
    if rt.provider is None:
        return 0
    n = 0
    for key, cfg in rt.configs.items():
        ref = rt.universe.get(key)
        if ref is None or not cfg.live_enabled or not ref.expiries:
            continue
        try:
            snap = await rt.provider.get_option_chain(ref, ref.expiries[0].expiry, max_age_s=cfg.freshness.chain_max_age_s / 2)
        except DoiError as e:
            log.warning("chain record %s: %s", key, type(e).__name__)
            continue
        await asyncio.to_thread(rt.recorder.snapshot, snap)
        n += 1
    return n


async def record_index_bars_once(rt, now: datetime | None = None, timeframe_min: int = 5, days_back: int = 0) -> int:
    """Fetch index candles (default 5m, the feature timeframe) for live-enabled underlyings and record them.
    `days_back` > 0 backfills warm-up history. Recording and fetch failures are logged and swallowed."""
    if rt.provider is None:
        return 0
    today = (now or datetime.now(UTC)).date()
    start = today - timedelta(days=days_back)
    n = 0
    for key, cfg in rt.configs.items():
        ref = rt.universe.get(key)
        if ref is None or not cfg.live_enabled:
            continue
        try:
            bars = await rt.provider.get_candles(
                ref.security_id, ref.segment, "INDEX", str(timeframe_min), start, today + timedelta(days=1)
            )
        except DoiError as e:
            log.warning("index bars %s: %s", key, type(e).__name__)
            continue
        await asyncio.to_thread(rt.recorder.bars, ref.security_id, timeframe_min, bars, "dhan_rest:intraday")
        n += len(bars)
    return n


async def run_forever(rt, interval_s: float = 3.0, bars_every_s: float = 60.0, chains_every_s: float = 60.0) -> None:
    last_bars = last_chains = -1e9
    backfilled = False
    loop = asyncio.get_running_loop()
    while True:
        try:
            if loop.time() - last_bars >= bars_every_s:
                last_bars = loop.time()
                await record_index_bars_once(rt, days_back=0 if backfilled else 7)  # warm-up for indicators
                backfilled = True
            market_open = rt.paper.cal.is_open(datetime.now(UTC)) if rt.paper is not None else True
            if market_open and loop.time() - last_chains >= chains_every_s:
                last_chains = loop.time()
                await record_chains_once(rt)
            await pump_once(rt)
            if rt.collector is not None and rt.paper is not None:
                await rt.collector.set_subscriptions(feed_subscriptions(rt.paper))
        except Exception:  # keep the feed alive; the ledger records every state change
            log.exception("paper feed pass failed")
        await asyncio.sleep(interval_s)
