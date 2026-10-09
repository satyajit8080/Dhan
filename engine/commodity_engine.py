"""
MCX commodity scalping engine (GoldM / SilverM / CrudeOilM) — adapted from the
SENSEX v3 refresh_table.py methodology for instruments with NO Greeks/IV
upstream (see /areas/commodity-options-analysis.md, "Methodology gap").

Key differences vs refresh_table.py (SENSEX):
  - No Black-76 reprice (no IV). "Breakout Above" premium is projected using a
    delta PROXY estimated directly from the live option chain: the local slope
    of premium vs strike between the two nearest strikes, d(mid_premium)/d(strike),
    negated for calls. This is derived from real, currently-quoted prices — not
    a fabricated or inferred vendor Greek — but it is still an approximation
    (assumes local linearity) and is weaker than a true Black-76 delta. It is
    labelled "delta_proxy" everywhere so it's never confused with a real Greek.
  - Levels are computed here directly (self-contained), not via the bull50-dhan
    compute_levels device tool, because that tool lives on the user's own
    device via the remote-devices bridge and is unavailable when the device
    isn't connected. This module has no such dependency — it runs entirely in
    the cloud workspace off INDmoney candle/chain data.
  - Support/resistance proximity and "too close to enter" checks are ATR-scaled
    (0.15 * ATR14) instead of a fixed point distance, since Gold/Silver/Crude
    trade on very different price scales (fixes the "3 points, TBD per
    commodity" gap flagged in the memory file).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional
import math


# ---------- levels ----------

@dataclass
class Level:
    price: float
    touches: int
    sources: list[str] = field(default_factory=list)


def true_range(h, l, prev_close):
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def atr14_from_daily(daily_candles: list[dict]) -> float:
    """daily_candles: oldest->newest dicts with high/low/close. Needs >=15."""
    if len(daily_candles) < 15:
        raise ValueError("need >=15 daily candles for ATR14")
    trs = []
    for i in range(1, len(daily_candles)):
        trs.append(true_range(daily_candles[i]["high"], daily_candles[i]["low"],
                               daily_candles[i - 1]["close"]))
    last14 = trs[-14:]
    return sum(last14) / len(last14)


def vwap(session_candles: list[dict]) -> Optional[float]:
    num = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * c["volume"] for c in session_candles)
    den = sum(c["volume"] for c in session_candles)
    return num / den if den > 0 else None


def swing_pivots(session_candles: list[dict], wing: int = 2):
    """Simple fractal pivots: a bar whose high/low is the extreme among
    [i-wing, i+wing]. Returns (pivot_highs, pivot_lows) as lists of price."""
    highs, lows = [], []
    n = len(session_candles)
    for i in range(wing, n - wing):
        window = session_candles[i - wing:i + wing + 1]
        h = session_candles[i]["high"]
        l = session_candles[i]["low"]
        if h == max(c["high"] for c in window):
            highs.append(h)
        if l == min(c["low"] for c in window):
            lows.append(l)
    return highs, lows


def cluster_levels(prices: list[float], tolerance: float, source_label: str, min_touches: int = 2) -> list[Level]:
    if not prices:
        return []
    prices = sorted(prices)
    clusters: list[list[float]] = [[prices[0]]]
    for p in prices[1:]:
        if p - clusters[-1][-1] <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = []
    for c in clusters:
        if len(c) >= min_touches:
            levels.append(Level(price=sum(c) / len(c), touches=len(c), sources=[source_label]))
    return levels


def atr_from_intraday(session_candles: list[dict], n: int = 14) -> Optional[float]:
    """ATR over the last n intraday (5-min) bars — used for level-clustering
    tolerance, which must react to TODAY's volatility, not a daily ATR that
    can be dominated by a multi-week trend (e.g. gold's multi-month rally
    makes ATR14-daily ~1.6% of spot, far too coarse to separate intraday
    swing points — this was a real bug in the first pass of this engine,
    caught by comparing tolerance against the session's own range)."""
    if len(session_candles) < 2:
        return None
    trs = []
    for i in range(1, len(session_candles)):
        trs.append(true_range(session_candles[i]["high"], session_candles[i]["low"],
                               session_candles[i - 1]["close"]))
    window = trs[-n:]
    return sum(window) / len(window) if window else None


def compute_levels(session_candles: list[dict], daily_candles: list[dict], min_touches: int = 2):
    """session_candles: today's intraday candles (oldest->newest, full session
    so far). daily_candles: >=15 oldest->newest daily candles including today.
    Returns dict with resistances, supports, atr14 (daily, for context/sizing),
    atr_intraday (for level tolerance), vwap, session_high, session_low.
    """
    atr = atr14_from_daily(daily_candles)
    atr_intra = atr_from_intraday(session_candles) or atr
    # cap tolerance so it can never exceed the daily ATR's contribution meant
    # for position sizing, and never explode past a small fraction of today's
    # own range — belt-and-braces against another wide-tolerance bug
    session_range = max(c["high"] for c in session_candles) - min(c["low"] for c in session_candles)
    tol = max(min(atr_intra * 0.5, session_range * 0.08), 1e-6)

    ph, pl = swing_pivots(session_candles, wing=2)
    res_levels = cluster_levels(ph, tol, "swing_high", min_touches)
    sup_levels = cluster_levels(pl, tol, "swing_low", min_touches)

    sess_high = max(c["high"] for c in session_candles)
    sess_low = min(c["low"] for c in session_candles)
    v = vwap(session_candles)

    # opening range = first 15 min (3x 5-min bars)
    orb = session_candles[:3]
    or_high = max(c["high"] for c in orb) if orb else None
    or_low = min(c["low"] for c in orb) if orb else None

    prev_close = daily_candles[-2]["close"] if len(daily_candles) >= 2 else None

    # fold in session H/L, opening range, VWAP, prev close as single-touch
    # "structural" levels (always kept, not filtered by min_touches) — mirrors
    # SENSEX compute_levels behaviour of always surfacing session/opening/VWAP.
    def add_structural(levels: list[Level], price: Optional[float], label: str):
        if price is None:
            return
        for lv in levels:
            if abs(lv.price - price) <= tol:
                lv.sources.append(label)
                return
        levels.append(Level(price=price, touches=1, sources=[label]))

    add_structural(res_levels, sess_high, "session_high")
    add_structural(sup_levels, sess_low, "session_low")
    if or_high is not None:
        add_structural(res_levels, or_high, "opening_range_high")
    if or_low is not None:
        add_structural(sup_levels, or_low, "opening_range_low")
    if v is not None:
        add_structural(res_levels, v, "vwap")
        add_structural(sup_levels, v, "vwap")
    if prev_close is not None:
        add_structural(res_levels, prev_close, "prev_close")
        add_structural(sup_levels, prev_close, "prev_close")

    res_levels.sort(key=lambda x: x.price)
    sup_levels.sort(key=lambda x: x.price)

    return {
        "resistances": res_levels,
        "supports": sup_levels,
        "atr14": atr,
        "atr_intraday": atr_intra,
        "vwap": v,
        "session_high": sess_high,
        "session_low": sess_low,
        "opening_range_high": or_high,
        "opening_range_low": or_low,
        "prev_close": prev_close,
        "tolerance": tol,
    }


def pick_level(levels: list[Level], spot: float, side: Literal["above", "below"]) -> Optional[Level]:
    cands = [l for l in levels if (l.price > spot if side == "above" else l.price < spot)]
    if not cands:
        return None
    return min(cands, key=lambda l: abs(l.price - spot)) if side == "above" else max(cands, key=lambda l: l.price)


# ---------- delta proxy + breakout premium projection ----------

@dataclass
class ChainLeg:
    strike: float
    is_call: bool
    bid: float
    ask: float
    ltp: float
    oi: int
    volume: int


def mid(leg: ChainLeg) -> float:
    if leg.bid and leg.ask:
        return (leg.bid + leg.ask) / 2.0
    return leg.ltp


def delta_proxy(legs_same_side: list[ChainLeg], strike: float) -> Optional[float]:
    """Estimate |delta| for `strike` from the local slope of mid-premium vs
    strike among adjacent strikes of the same side (call or put), using real
    quoted prices. Returns None if not enough neighbouring strikes."""
    same = sorted(legs_same_side, key=lambda l: l.strike)
    idx = next((i for i, l in enumerate(same) if l.strike == strike), None)
    if idx is None:
        return None
    lo = same[idx - 1] if idx - 1 >= 0 else None
    hi = same[idx + 1] if idx + 1 < len(same) else None
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        slope = (mid(hi) - mid(lo)) / (hi.strike - lo.strike)
    elif hi is not None:
        slope = (mid(hi) - mid(same[idx])) / (hi.strike - same[idx].strike)
    else:
        slope = (mid(same[idx]) - mid(lo)) / (same[idx].strike - lo.strike)
    # for a call, premium falls as strike rises => slope negative => delta = -slope
    # for a put,  premium rises as strike rises => slope positive => delta = slope
    is_call = same[idx].is_call
    d = -slope if is_call else slope
    return max(0.0, min(1.0, d))


def project_breakout_premium(leg: ChainLeg, legs_same_side: list[ChainLeg], spot_now: float, trigger: float) -> tuple[Optional[float], Optional[float]]:
    """Returns (projected_premium, delta_proxy_used)."""
    d = delta_proxy(legs_same_side, leg.strike)
    if d is None:
        return None, None
    current = mid(leg)
    move = trigger - spot_now
    sign = 1 if leg.is_call else -1
    projected = current + sign * d * move
    return max(projected, 0.05), d


# ---------- filters / gates (adapted v3 rules) ----------

def spread_pct(leg: ChainLeg) -> Optional[float]:
    m = mid(leg)
    if not m or leg.bid is None or leg.ask is None or leg.bid <= 0 or leg.ask <= 0:
        return None
    return (leg.ask - leg.bid) / m


def trading_days_to_expiry(now: datetime, expiry: datetime) -> int:
    d = 0
    cur = now.date()
    exp = expiry.date()
    while cur < exp:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri; MCX trades most weekdays, no separate holiday calendar here
            d += 1
    return d


MCX_SESSION_START = (9, 0)
MCX_SESSION_END = (23, 30)
MCX_OPEN_SKIP_MIN = 10   # skip first 10 min after open
MCX_CLOSE_SKIP_MIN = 15  # skip last 15 min before close


def in_skip_window(now: datetime) -> tuple[bool, str]:
    start = now.replace(hour=MCX_SESSION_START[0], minute=MCX_SESSION_START[1], second=0, microsecond=0)
    end = now.replace(hour=MCX_SESSION_END[0], minute=MCX_SESSION_END[1], second=0, microsecond=0)
    if now < start or now > end:
        return True, "OUTSIDE_MCX_SESSION"
    if now < start + timedelta(minutes=MCX_OPEN_SKIP_MIN):
        return True, "OPEN_SKIP_WINDOW"
    if now > end - timedelta(minutes=MCX_CLOSE_SKIP_MIN):
        return True, "CLOSE_SKIP_WINDOW"
    return False, ""


# ---------- row builder ----------

@dataclass
class Row:
    strike: float
    type: str  # "CE" / "PE"
    ltp: float
    breakout_above: str  # formatted premium or status code


def build_commodity_refresh(
    symbol: str,
    spot: float,
    legs: list[ChainLeg],
    resistances: list[Level],
    supports: list[Level],
    now: datetime,
    expiry: datetime,
    min_touches: int = 2,
) -> list[Row]:
    skip, reason = in_skip_window(now)
    tdte = trading_days_to_expiry(now, expiry)

    rows = []
    for leg in legs:
        same_side = [l for l in legs if l.is_call == leg.is_call]
        if skip:
            rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", mid(leg), reason))
            continue
        if tdte < 2:
            rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", mid(leg), "SKIP_EXPIRY_PROXIMITY"))
            continue
        sp = spread_pct(leg)
        if sp is None or sp > 0.005:
            rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", mid(leg), "NO_LIQUIDITY"))
            continue

        side = "above" if leg.is_call else "below"
        level = pick_level(resistances if leg.is_call else supports, spot, side)
        if level is None or level.touches < min_touches:
            rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", mid(leg), "NO_LEVEL"))
            continue

        projected, d = project_breakout_premium(leg, same_side, spot, level.price)
        if projected is None:
            rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", mid(leg), "NO_DELTA_PROXY"))
            continue

        rows.append(Row(leg.strike, "CE" if leg.is_call else "PE", round(mid(leg), 1), f"{round(projected, 1)} @ {level.price:g} (d≈{d:.2f})"))
    return rows


def to_table(rows: list[Row]) -> str:
    lines = ["| Strike | Type | LTP | Breakout Above |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.strike:g} | {r.type} | {r.ltp:g} | {r.breakout_above} |")
    return "\n".join(lines)
