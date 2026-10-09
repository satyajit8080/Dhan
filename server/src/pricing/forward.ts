/**
 * The forward, recovered from put-call parity.
 *
 * Per strike:  F_K = K + (C - P) / DF,  with DF = exp(-r*T) HELD FIXED.
 * Aggregate:   MEDIAN across liquid near-ATM strikes.
 *
 * WHY MEDIAN, NOT REGRESSION: over a narrow strike band the regression of
 * (C - P) on K is ill-conditioned — the strikes span a fraction of a percent of
 * K, so the fitted slope is dominated by quote noise on any single leg. The
 * median of per-strike forwards is robust to one stale leg in a way the slope
 * is not. The slope is computed here anyway, but ONLY as a diagnostic: it must
 * never set the forward.
 *
 * WHY DF IS NOT FITTED: fitting both the forward and the discount factor to the
 * same narrow band is under-determined. r is an input; DF follows from it.
 *
 * PURE MODULE: no I/O, no clock, no config.
 */

import { discountFactor } from './black76.js';

export interface ParityLeg {
  strike: number;
  callPrice: number;
  putPrice: number;
}

export interface PerStrikeForward {
  strike: number;
  forward: number;
  callPrice: number;
  putPrice: number;
  /** True when the strike fell inside the ATM band and fed the median. */
  used: boolean;
}

export interface SlopeDiagnostic {
  /** Least-squares slope of (C - P) on K. Theory says exactly -DF. */
  slope: number;
  expectedSlope: number;
  /** |slope / expectedSlope - 1|. */
  relativeError: number;
  withinTolerance: boolean;
  tolerance: number;
  /** Null when fewer than 2 usable strikes, or K has no spread. */
  computable: boolean;
}

export interface ForwardResult {
  forward: number;
  discountFactor: number;
  perStrike: PerStrikeForward[];
  /** max - min across the USED per-strike forwards. The dispersion gate input. */
  spread: number;
  usedStrikes: number[];
  band: { atmHint: number; pct: number; lower: number; upper: number };
  slopeDiagnostic: SlopeDiagnostic;
}

export interface ForwardOptions {
  /** Half-width of the strike band as a fraction of the ATM hint. */
  bandPct?: number;
  /** Tolerance on the slope DIAGNOSTIC only. */
  slopeTolerance?: number;
}

/** F_K = K + (C - P) / DF. */
export function perStrikeForward(
  strike: number,
  callPrice: number,
  putPrice: number,
  df: number,
): number {
  return strike + (callPrice - putPrice) / df;
}

/** Median. Even counts average the two central values. */
export function median(values: number[]): number {
  if (values.length === 0) throw new Error('median of empty set');
  const s = [...values].sort((a, b) => a - b);
  const mid = s.length >> 1;
  if (s.length % 2 === 1) return s[mid]!;
  return (s[mid - 1]! + s[mid]!) / 2;
}

function leastSquaresSlope(xs: number[], ys: number[]): number | null {
  const n = xs.length;
  if (n < 2) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0;
  let den = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i]! - mx;
    num += dx * (ys[i]! - my);
    den += dx * dx;
  }
  if (den === 0) return null;
  return num / den;
}

/**
 * Recover the forward from a set of call/put pairs.
 *
 * @param legs     Call and put prices by strike, from ONE snapshot.
 * @param atmHint  Rough ATM level used only to centre the band. A poor hint
 *                 widens or shifts the band; it does not bias the forward,
 *                 because the forward is a median of parity relations that do
 *                 not reference the hint at all.
 * @param r        Risk-free rate.
 * @param T        Year fraction to expiry.
 */
export function parityForward(
  legs: ParityLeg[],
  atmHint: number,
  r: number,
  T: number,
  opts: ForwardOptions = {},
): ForwardResult {
  const { bandPct = 0.015, slopeTolerance = 0.02 } = opts;
  const df = discountFactor(r, T);

  const lower = atmHint * (1 - bandPct);
  const upper = atmHint * (1 + bandPct);

  const perStrike: PerStrikeForward[] = legs
    .map((l) => ({
      strike: l.strike,
      callPrice: l.callPrice,
      putPrice: l.putPrice,
      forward: perStrikeForward(l.strike, l.callPrice, l.putPrice, df),
      used: l.strike >= lower && l.strike <= upper,
    }))
    .sort((a, b) => a.strike - b.strike);

  const used = perStrike.filter((p) => p.used);
  if (used.length === 0) {
    throw new Error(
      `No strikes inside the ATM band [${lower.toFixed(2)}, ${upper.toFixed(2)}] ` +
        `around hint ${atmHint}. Cannot recover a forward.`,
    );
  }

  const forwards = used.map((p) => p.forward);
  const forward = median(forwards);
  const spread = Math.max(...forwards) - Math.min(...forwards);

  const slope = leastSquaresSlope(
    used.map((p) => p.strike),
    used.map((p) => p.callPrice - p.putPrice),
  );
  const expectedSlope = -df;
  const relativeError =
    slope === null ? NaN : Math.abs(slope / expectedSlope - 1);

  return {
    forward,
    discountFactor: df,
    perStrike,
    spread,
    usedStrikes: used.map((p) => p.strike),
    band: { atmHint, pct: bandPct, lower, upper },
    slopeDiagnostic: {
      slope: slope ?? NaN,
      expectedSlope,
      relativeError,
      withinTolerance: slope === null ? false : relativeError <= slopeTolerance,
      tolerance: slopeTolerance,
      computable: slope !== null,
    },
  };
}
