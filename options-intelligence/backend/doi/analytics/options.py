"""Pure option-chain analytics (blueprint §7, options analytics).

Interpretation guardrail (blueprint): OI/PCR do not reveal intent, max pain is
not a price magnet, and the gamma exposure figure is a proxy, not observed
dealer positioning. These are explanatory features, never triggers.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from doi.analytics import black76
from doi.core.models import ChainRow, OptionLeg


def leg_price(leg: OptionLeg | None) -> float | None:
    """Mid when two-sided, else None. The LTP is never used for valuation."""
    if leg is None or not leg.two_sided:
        return None
    return (leg.bid + leg.ask) / 2  # type: ignore[operator]


def strike_step(rows: Sequence[ChainRow]) -> float | None:
    ks = sorted({r.strike for r in rows})
    diffs = Counter(round(b - a, 6) for a, b in pairwise(ks) if b > a)
    return diffs.most_common(1)[0][0] if diffs else None


def implied_forward(rows: Sequence[ChainRow], ref: float, df: float, band_pct: float) -> tuple[float | None, int]:
    """Put-call parity forward F = K + (C - P)/DF, median over strikes within ±band of ref."""
    vals = []
    for r in rows:
        if abs(r.strike - ref) > ref * band_pct / 100:
            continue
        c, p = leg_price(r.ce), leg_price(r.pe)
        if c is not None and p is not None:
            vals.append(r.strike + (c - p) / df)
    return (statistics.median(vals), len(vals)) if vals else (None, 0)


def atm_strike(rows: Sequence[ChainRow], f: float) -> float | None:
    return min((r.strike for r in rows), key=lambda k: (abs(k - f), k), default=None)


def window(rows: Sequence[ChainRow], center: float, n: int) -> list[ChainRow]:
    """ATM ± n strikes."""
    srt = sorted(rows, key=lambda r: r.strike)
    idx = min(range(len(srt)), key=lambda i: (abs(srt[i].strike - center), srt[i].strike)) if srt else 0
    return srt[max(0, idx - n) : idx + n + 1]


def _sum(rows: Sequence[ChainRow], side: str, field: str) -> int:
    total = 0
    for r in rows:
        leg = r.ce if side == "ce" else r.pe
        v = getattr(leg, field) if leg is not None else None
        total += v or 0
    return total


def pcr(rows: Sequence[ChainRow], field: str = "oi") -> float | None:
    ce = _sum(rows, "ce", field)
    return _sum(rows, "pe", field) / ce if ce > 0 else None


@dataclass(frozen=True, slots=True)
class Wall:
    strike: float
    oi: int
    share: float  # wall OI / total OI on that side


def wall(rows: Sequence[ChainRow], side: str) -> Wall | None:
    best: tuple[int, float] | None = None
    total = 0
    for r in rows:
        leg = r.ce if side == "ce" else r.pe
        oi = (leg.oi or 0) if leg is not None else 0
        total += oi
        if oi > 0 and (best is None or oi > best[0] or (oi == best[0] and r.strike < best[1])):
            best = (oi, r.strike)
    if best is None or total == 0:
        return None
    return Wall(best[1], best[0], best[0] / total)


def max_pain(rows: Sequence[ChainRow]) -> float | None:
    """Expiry price (tested at each strike) minimising total intrinsic paid to holders."""
    strikes = sorted({r.strike for r in rows})
    if not strikes:
        return None
    pain = []
    for s in strikes:
        total = 0.0
        for r in rows:
            total += ((r.ce.oi or 0) if r.ce else 0) * max(0.0, s - r.strike)
            total += ((r.pe.oi or 0) if r.pe else 0) * max(0.0, r.strike - s)
        pain.append((total, s))
    if all(p == 0 for p, _ in pain):
        return None
    return min(pain)[1]


def gamma_exposure_proxy(
    rows: Sequence[ChainRow], f: float, t: float, df: float, rate: float, oi_multiplier: float
) -> float | None:
    """Σ gamma_B76(K, IV_K) * (OI_call - OI_put) * multiplier * F² * 0.01.

    Sign convention (documented assumption, not observation): calls +, puts -,
    as if dealers were short calls and long puts. The result is premium-currency
    exposure per 1% move in F."""
    total, used = 0.0, 0
    for r in rows:
        for leg, is_call in ((r.ce, True), (r.pe, False)):
            p = leg_price(leg)
            if p is None or not leg.oi:  # type: ignore[union-attr]
                continue
            iv = black76.implied_vol(p, f, r.strike, t, df, is_call)
            if iv is None:
                continue
            g = black76.greeks(f, r.strike, t, iv, df, is_call, rate).gamma
            total += g * leg.oi * oi_multiplier * f * f * 0.01 * (1 if is_call else -1)  # type: ignore[union-attr]
            used += 1
    return total if used else None


@dataclass(frozen=True, slots=True)
class LiquidityStats:
    median_spread_pct: float | None
    min_top_qty: int | None
    two_sided_share: float
    legs: int


def liquidity(rows: Sequence[ChainRow]) -> LiquidityStats:
    spreads, qtys, two = [], [], 0
    legs = [leg for r in rows for leg in (r.ce, r.pe) if leg is not None]
    for leg in legs:
        if leg.two_sided:
            two += 1
            mid = (leg.bid + leg.ask) / 2  # type: ignore[operator]
            spreads.append((leg.ask - leg.bid) / mid * 100)  # type: ignore[operator]
            qtys.append(min(leg.bid_qty or 0, leg.ask_qty or 0))
    return LiquidityStats(
        statistics.median(spreads) if spreads else None,
        min(qtys) if qtys else None,
        two / len(legs) if legs else 0.0,
        len(legs),
    )


def discount_factor(rate: float, t: float) -> float:
    return math.exp(-rate * max(t, 0.0))
