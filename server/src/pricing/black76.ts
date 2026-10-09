/**
 * Black-76: options on a FORWARD, not Black-Scholes on a spot.
 *
 * The distinction is the entire point of this module. Pricing SENSEX options
 * off the index LTP silently embeds a carry error (measured at 321 points /
 * 47.9% annualised on 18-Sep-2026), which shows up as call and put IV
 * disagreeing at the same strike. Black-76 on the parity forward makes them
 * agree by construction.
 *
 * PURE MODULE: imports only ./normal. No I/O, no clock, no config.
 */

import { normCdf, normPdf } from './normal.js';

export type OptionType = 'CE' | 'PE';

export interface Greeks {
  /** dV/dF, discounted. Dimensionless. */
  delta: number;
  /** d2V/dF2. Per point^2. */
  gamma: number;
  /** Per 1 IV POINT (i.e. per 0.01 of sigma), not per 1.00 of sigma. */
  vega: number;
  /** Per CALENDAR DAY. */
  theta: number;
  /** Per 1% rate move, Black-76 convention: -T*V. */
  rho: number;
  /** The Black-76 price at the supplied sigma, for cross-checking. */
  price: number;
  d1: number;
  d2: number;
}

/** Discount factor. Held FIXED by the caller; never fitted. */
export function discountFactor(r: number, T: number): number {
  return Math.exp(-r * T);
}

function intrinsic(F: number, K: number, type: OptionType): number {
  return type === 'CE' ? Math.max(0, F - K) : Math.max(0, K - F);
}

/**
 * No-arbitrage price bounds, both already discounted.
 *   call: DF*max(0, F-K) <= C <= DF*F
 *   put:  DF*max(0, K-F) <= P <= DF*K
 */
export function noArbBounds(
  F: number,
  K: number,
  T: number,
  r: number,
  type: OptionType,
): { lower: number; upper: number } {
  const DF = discountFactor(r, T);
  return {
    lower: DF * intrinsic(F, K, type),
    upper: type === 'CE' ? DF * F : DF * K,
  };
}

/** Black-76 price. */
export function b76Price(
  F: number,
  K: number,
  T: number,
  sigma: number,
  r: number,
  type: OptionType,
): number {
  const DF = discountFactor(r, T);
  if (!(T > 0) || !(sigma > 0)) return DF * intrinsic(F, K, type);

  const sqrtT = Math.sqrt(T);
  const sT = sigma * sqrtT;
  const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / sT;
  const d2 = d1 - sT;

  const raw =
    type === 'CE'
      ? DF * (F * normCdf(d1) - K * normCdf(d2))
      : DF * (K * normCdf(-d2) - F * normCdf(-d1));

  // Clamp at the discounted intrinsic.
  //
  // For a deep-OTM leg near expiry both terms collapse to ~1e-11 and cancel:
  // at F=74616.57, K=77000, T=6/365, sigma=0.03 the expression evaluates to
  // -2.6e-13. A negative premium is never a real price — it is the last bits
  // of two nearly-equal numbers. Returning it would feed a negative into IV
  // inversion and every Greek downstream. The true value is bounded below by
  // the discounted intrinsic, so clamping there is exact, not cosmetic.
  return Math.max(DF * intrinsic(F, K, type), raw);
}

export interface IVOptions {
  /** Lower bracket for sigma. */
  lo?: number;
  /** Upper bracket for sigma. */
  hi?: number;
  /** Bisection iterations. 200 takes a 5.0-wide bracket below 1e-60. */
  iterations?: number;
  /** Absolute price tolerance for the no-arb bound check. */
  tolerance?: number;
}

/**
 * Implied volatility by bisection on price.
 *
 * Returns null — never a number — when the observed price sits outside the
 * no-arbitrage bounds. A stale or crossed vendor quote must surface as "no IV",
 * not as a fabricated one that then poisons every Greek downstream.
 *
 * Bisection rather than Newton: monotone in sigma, cannot diverge, and needs no
 * vega guard near expiry where vega collapses and Newton becomes unstable.
 */
export function b76IV(
  price: number,
  F: number,
  K: number,
  T: number,
  r: number,
  type: OptionType,
  opts: IVOptions = {},
): number | null {
  const { lo = 1e-9, hi = 5, iterations = 200, tolerance = 1e-8 } = opts;

  if (!Number.isFinite(price) || !(T > 0) || !(F > 0) || !(K > 0)) return null;

  const { lower, upper } = noArbBounds(F, K, T, r, type);
  if (price < lower - tolerance) return null;
  if (price > upper + tolerance) return null;

  // At exactly intrinsic the only consistent vol is zero.
  if (price <= lower + tolerance) return 0;

  let a = lo;
  let b = hi;
  if (b76Price(F, K, T, b, r, type) < price) return null; // beyond bracket

  for (let i = 0; i < iterations; i++) {
    const m = (a + b) / 2;
    if (b76Price(F, K, T, m, r, type) > price) b = m;
    else a = m;
  }
  return (a + b) / 2;
}

/**
 * Analytic Greeks.
 *
 * theta_annual = r*V - DF*F*pdf(d1)*sigma/(2*sqrt(T))
 *
 * The second term — the decay term — is IDENTICAL for calls and puts in
 * Black-76. Total theta differs between a call and a put at the same strike
 * only through r*V, i.e. by r*(V_call - V_put)/365 per day. Any test that
 * asserts call and put theta are equal is asserting the wrong thing; assert on
 * the decay term.
 */
export function b76Greeks(
  F: number,
  K: number,
  T: number,
  sigma: number,
  r: number,
  type: OptionType,
): Greeks {
  const DF = discountFactor(r, T);
  const sqrtT = Math.sqrt(T);
  const sT = sigma * sqrtT;
  const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / sT;
  const d2 = d1 - sT;

  const price = b76Price(F, K, T, sigma, r, type);
  const pdfd1 = normPdf(d1);

  const delta = type === 'CE' ? DF * normCdf(d1) : -DF * normCdf(-d1);
  const gamma = (DF * pdfd1) / (F * sT);
  const vega = (DF * F * pdfd1 * sqrtT) / 100;
  const thetaAnnual = r * price - (DF * F * pdfd1 * sigma) / (2 * sqrtT);
  const rho = (-T * price) / 100;

  return { delta, gamma, vega, theta: thetaAnnual / 365, rho, price, d1, d2 };
}

/** The decay term alone, shared by calls and puts. Exposed for tests. */
export function b76DecayTermAnnual(
  F: number,
  K: number,
  T: number,
  sigma: number,
  r: number,
): number {
  const DF = discountFactor(r, T);
  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT);
  return -(DF * F * normPdf(d1) * sigma) / (2 * sqrtT);
}
