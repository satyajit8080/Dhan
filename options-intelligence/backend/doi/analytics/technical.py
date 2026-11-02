"""Pure technical-analysis series functions (blueprint §7, technical analysis).

Conventions:
- Inputs are oldest-first sequences. Outputs are lists of the same length,
  aligned to the input. `None` marks the warm-up period or missing data.
- RSI, ATR and ADX use Wilder smoothing. EMAs are seeded with the simple mean of
  the first n values.
- No function looks at data after the index it computes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from doi.core.models import Bar

Series = list[float | None]


def ema(values: Sequence[float], n: int) -> Series:
    if n < 1:
        raise ValueError("period must be >= 1")
    out: Series = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: Sequence[float], n: int, start: int = 0) -> Series:
    """Wilder smoothing of values[start:], seeded with their first-n mean."""
    out: Series = [None] * len(values)
    if len(values) - start < n:
        return out
    prev = sum(values[start : start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = _wilder(gains, n, 1), _wilder(losses, n, 1)
    for i in range(len(closes)):
        if ag[i] is None:
            continue
        g, lo = ag[i], al[i]
        out[i] = 100.0 if lo == 0 and g > 0 else 50.0 if lo == 0 else 100.0 - 100.0 / (1 + g / lo)  # type: ignore[operator]
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Series, Series, Series]:
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(ef, es, strict=True)]
    first = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if first is not None:
        s = ema([v for v in line[first:] if v is not None], signal)
        sig[first:] = s
    hist: Series = [None if a is None or b is None else a - b for a, b in zip(line, sig, strict=True)]
    return line, sig, hist


def roc(closes: Sequence[float], n: int = 10) -> Series:
    return [None if i < n or closes[i - n] == 0 else (closes[i] / closes[i - n] - 1) * 100 for i in range(len(closes))]


def true_range(bars: Sequence[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], n: int = 14) -> Series:
    return _wilder(true_range(bars), n)


def adx(bars: Sequence[Bar], n: int = 14) -> tuple[Series, Series, Series]:
    """(ADX, +DI, -DI). First DI at index n, first ADX at index 2n-1."""
    m = len(bars)
    pdi: Series = [None] * m
    mdi: Series = [None] * m
    out: Series = [None] * m
    if m <= n:
        return out, pdi, mdi
    tr = true_range(bars)
    pdm, mdm = [0.0], [0.0]
    for i in range(1, m):
        up, down = bars[i].high - bars[i - 1].high, bars[i - 1].low - bars[i].low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    s_tr, s_p, s_m = _wilder(tr, n, 1), _wilder(pdm, n, 1), _wilder(mdm, n, 1)
    dx: list[float] = []
    dx_idx: list[int] = []
    for i in range(m):
        if s_tr[i] is None or s_tr[i] == 0:
            continue
        p, q = 100 * s_p[i] / s_tr[i], 100 * s_m[i] / s_tr[i]  # type: ignore[operator]
        pdi[i], mdi[i] = p, q
        dx.append(0.0 if p + q == 0 else 100 * abs(p - q) / (p + q))
        dx_idx.append(i)
    sm = _wilder(dx, n)
    for j, v in enumerate(sm):
        if v is not None:
            out[dx_idx[j]] = v
    return out, pdi, mdi


def bollinger_width(closes: Sequence[float], n: int = 20, k: float = 2.0) -> Series:
    out: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1 : i + 1]
        mid = sum(w) / n
        sd = math.sqrt(sum((x - mid) ** 2 for x in w) / n)
        out[i] = None if mid == 0 else (2 * k * sd) / mid * 100
    return out


def realized_vol(closes: Sequence[float], n: int, periods_per_year: float) -> Series:
    """Annualised % stdev (ddof=1) of the last n log returns."""
    out: Series = [None] * len(closes)
    rets: list[float | None] = [None] + [
        math.log(closes[i] / closes[i - 1]) if closes[i] > 0 and closes[i - 1] > 0 else None for i in range(1, len(closes))
    ]
    for i in range(n, len(closes)):
        w = rets[i - n + 1 : i + 1]
        if any(r is None for r in w) or n < 2:
            continue
        mu = sum(w) / n  # type: ignore[arg-type]
        var = sum((r - mu) ** 2 for r in w) / (n - 1)  # type: ignore[operator]
        out[i] = math.sqrt(var * periods_per_year) * 100
    return out


def has_volume(bars: Sequence[Bar]) -> bool:
    return any((b.volume or 0) > 0 for b in bars)


def vwap(bars: Sequence[Bar]) -> float | None:
    """Session VWAP over `bars` (caller passes only the current session)."""
    pv = vol = 0.0
    for b in bars:
        v = b.volume or 0.0
        pv += (b.high + b.low + b.close) / 3 * v
        vol += v
    return pv / vol if vol > 0 else None


def relative_volume(bars: Sequence[Bar], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    base = [b.volume or 0.0 for b in bars[-n - 1 : -1]]
    avg = sum(base) / n
    return (bars[-1].volume or 0.0) / avg if avg > 0 else None


def volume_poc(bars: Sequence[Bar], bin_size: float) -> float | None:
    """Price bin (lower edge) holding the most volume, using bar typical prices."""
    if bin_size <= 0:
        raise ValueError("bin size must be positive")
    bins: dict[float, float] = {}
    for b in bars:
        v = b.volume or 0.0
        if v > 0:
            k = math.floor(((b.high + b.low + b.close) / 3) / bin_size) * bin_size
            bins[k] = bins.get(k, 0.0) + v
    return max(sorted(bins), key=lambda k: bins[k]) if bins else None


def swing_points(bars: Sequence[Bar], k: int = 2) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Confirmed fractal swing highs/lows. A pivot at i needs k bars on each side,
    so the last k bars can never be pivots (no look-ahead)."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k : i + k + 1]
        if bars[i].high == max(b.high for b in win) and all(bars[i].high > b.high for b in win if b is not bars[i]):
            highs.append((i, bars[i].high))
        if bars[i].low == min(b.low for b in win) and all(bars[i].low < b.low for b in win if b is not bars[i]):
            lows.append((i, bars[i].low))
    return highs, lows


def market_structure(bars: Sequence[Bar], k: int = 2) -> str | None:
    """HH_HL (up), LH_LL (down), MIXED, or None if fewer than two swings of each kind."""
    highs, lows = swing_points(bars, k)
    if len(highs) < 2 or len(lows) < 2:
        return None
    hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
    if hh and hl:
        return "HH_HL"
    if not hh and not hl:
        return "LH_LL"
    return "MIXED"


def candle_context(bar: Bar, atr_value: float | None) -> dict[str, float | None]:
    rng = bar.high - bar.low
    return {
        "range_expansion": None if not atr_value else rng / atr_value,
        "body_ratio": None if rng == 0 else abs(bar.close - bar.open) / rng,
        "close_location": None if rng == 0 else (bar.close - bar.low) / rng,  # 1 = closed at high
    }


def percentile_rank(history: Sequence[float], value: float) -> float:
    """Share (0-100) of history values <= value."""
    if not history:
        raise ValueError("empty history")
    return 100.0 * sum(1 for h in history if h <= value) / len(history)
