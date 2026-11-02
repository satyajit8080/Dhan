"""Black-76 pricing on the forward, implied volatility and Greeks.

This is the platform's own pricing core, independent of the vendor's (Dhan)
IV/Greeks, which `RULES.md` §2 quarantines. Units match the Bull50 server:
  delta : dV/dF, discounted
  gamma : per point²
  vega  : per 1 vol point (0.01)
  theta : per calendar day
`sigma` is a decimal (0.15 = 15%), and `t` is in years (ACT/365).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2PI = math.sqrt(2 * math.pi)


def ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def _d1d2(f: float, k: float, t: float, sigma: float) -> tuple[float, float]:
    v = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * v * v) / v
    return d1, d1 - v


def price(f: float, k: float, t: float, sigma: float, df: float, is_call: bool) -> float:
    if f <= 0 or k <= 0 or df <= 0:
        raise ValueError("forward, strike and discount factor must be positive")
    if t <= 0 or sigma <= 0:
        intrinsic = max(f - k, 0.0) if is_call else max(k - f, 0.0)
        return df * intrinsic
    d1, d2 = _d1d2(f, k, t, sigma)
    if is_call:
        return df * (f * ncdf(d1) - k * ncdf(d2))
    return df * (k * ncdf(-d2) - f * ncdf(-d1))


def implied_vol(
    premium: float, f: float, k: float, t: float, df: float, is_call: bool, lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-7
) -> float | None:
    """Bisection (monotone in sigma). None if the premium is outside no-arbitrage bounds."""
    if premium is None or premium <= 0 or t <= 0:
        return None
    intrinsic = df * (max(f - k, 0.0) if is_call else max(k - f, 0.0))
    upper = df * (f if is_call else k)
    if premium < intrinsic - 1e-9 or premium >= upper:
        return None
    p_lo, p_hi = price(f, k, t, lo, df, is_call), price(f, k, t, hi, df, is_call)
    if premium <= p_lo:
        return lo
    if premium >= p_hi:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if price(f, k, t, mid, df, is_call) < premium:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


@dataclass(frozen=True, slots=True)
class Greeks:
    delta: float
    gamma: float
    vega: float
    theta: float


def greeks(f: float, k: float, t: float, sigma: float, df: float, is_call: bool, rate: float) -> Greeks:
    if t <= 0 or sigma <= 0:
        raise ValueError("greeks need positive time and volatility")
    d1, _ = _d1d2(f, k, t, sigma)
    sq = math.sqrt(t)
    delta = df * ncdf(d1) if is_call else -df * ncdf(-d1)
    gamma = df * npdf(d1) / (f * sigma * sq)
    vega = df * f * npdf(d1) * sq * 0.01
    v = price(f, k, t, sigma, df, is_call)
    theta_year = -df * f * npdf(d1) * sigma / (2 * sq) + rate * v
    return Greeks(delta, gamma, vega, theta_year / 365.0)
