"""Conservative fill model (blueprint section 10).

- Buys fill at the ask plus adverse slippage, sells at the bid minus slippage.
  The LTP is never used as an executable price.
- An order can only fill on a quote observed at or after `submitted_at + latency`.
  This means there is no look-ahead and no fill on the quote that triggered the order.
- Quantities are whole lots. DEPTH fills up to the displayed top-of-book quantity
  times a participation rate. ALL_OR_NONE waits until the touch can absorb the
  whole remainder.
- A missing side of the book means no fill. The order stays pending or expires."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

D = Decimal


class PartialFillPolicy(StrEnum):
    DEPTH = "DEPTH"
    ALL_OR_NONE = "ALL_OR_NONE"


def to_dec(x: float | int | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else D(str(x))


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return (steps * tick).quantize(tick)


@dataclass(frozen=True, slots=True)
class FillModel:
    version: str = "fill-v1"
    latency_ms: int = 250
    slippage_ticks: int = 1
    slippage_bps: Decimal = D("5")  # of the touch price; the larger of ticks/bps applies
    participation: Decimal = D("1")  # share of displayed top-of-book quantity we may take
    partial_policy: PartialFillPolicy = PartialFillPolicy.DEPTH

    @property
    def latency(self) -> timedelta:
        return timedelta(milliseconds=self.latency_ms)

    def slippage(self, ref: Decimal, tick: Decimal) -> Decimal:
        return max(tick * self.slippage_ticks, ref * self.slippage_bps / D("10000"))

    def buy_price(self, ask: Decimal, tick: Decimal, limit: Decimal | None = None) -> Decimal | None:
        if limit is not None and ask > limit:
            return None
        px = round_to_tick(ask + self.slippage(ask, tick), tick, up=True)
        return min(px, limit) if limit is not None else px

    def sell_price(self, bid: Decimal, tick: Decimal, limit: Decimal | None = None) -> Decimal | None:
        if limit is not None and bid < limit:
            return None
        px = max(round_to_tick(bid - self.slippage(bid, tick), tick, up=False), tick)
        return max(px, limit) if limit is not None else px

    def fillable_qty(self, remaining: int, displayed: int | None, lot: int) -> int:
        if remaining <= 0 or not displayed or displayed <= 0:
            return 0
        avail = int(D(displayed) * self.participation)
        if self.partial_policy is PartialFillPolicy.ALL_OR_NONE:
            return remaining if avail >= remaining else 0
        return (min(remaining, avail) // lot) * lot
