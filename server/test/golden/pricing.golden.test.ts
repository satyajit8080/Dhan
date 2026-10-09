import { describe, it, expect } from 'vitest';
import {
  parityForward,
  checkGate,
  b76Price,
  b76IV,
  b76Greeks,
  b76DecayTermAnnual,
  noArbBounds,
  discountFactor,
  yearFractionToExpiry,
  daysToExpiry,
  istToEpochMs,
} from '../../src/pricing/index.js';
import { GM } from '../fixtures.js';

const nowMs = istToEpochMs(GM.asOfIst);
const T = yearFractionToExpiry(nowMs, GM.expiry);
const DF = discountFactor(GM.r, T);

describe('golden master — time', () => {
  it('is exactly 6 calendar days to the 15:30 IST stamp', () => {
    expect(daysToExpiry(nowMs, GM.expiry)).toBe(GM.expected.calendarDays);
  });

  it('reproduces T to the last bit', () => {
    expect(T).toBe(GM.expected.T);
  });
});

describe('golden master — parity forward', () => {
  const fwd = parityForward([...GM.chain], GM.atmHint, GM.r, T);

  it('recovers the forward as the median of per-strike parity', () => {
    expect(fwd.forward).toBeCloseTo(GM.expected.parityForward, 3);
  });

  it('reports the per-strike dispersion', () => {
    expect(fwd.spread).toBeCloseTo(GM.expected.perStrikeSpread, 3);
  });

  it('uses every strike — all five are inside the 1.5% band', () => {
    expect(fwd.usedStrikes).toEqual([74100, 74200, 74300, 74400, 74500]);
  });

  it('keeps the regression slope as a diagnostic only', () => {
    // Theory: d(C-P)/dK = -DF exactly.
    expect(fwd.slopeDiagnostic.expectedSlope).toBeCloseTo(-DF, 12);
    expect(fwd.slopeDiagnostic.computable).toBe(true);
    // Whatever the slope says, the forward came from the median.
    expect(fwd.forward).toBeCloseTo(GM.expected.parityForward, 3);
  });
});

describe('golden master — integrity gate', () => {
  const fwd = parityForward([...GM.chain], GM.atmHint, GM.r, T);
  const gate = checkGate({
    parityForward: fwd.forward,
    listedFuture: GM.futuresLtp,
    perStrikeSpread: fwd.spread,
    indexLtp: GM.indexLtp,
    slopeWithinTolerance: fwd.slopeDiagnostic.withinTolerance,
    slopeRelativeError: fwd.slopeDiagnostic.relativeError,
  });

  it('PASSES — 40.18 points is inside the 75-point limit', () => {
    expect(gate.blocked).toBe(false);
    expect(gate.reasons).toEqual([]);
    expect(gate.divergenceVsFuture).toBeCloseTo(GM.expected.divergenceVsFuture, 3);
  });

  it('warns loudly that the index LTP is 321 points low', () => {
    expect(gate.indexDivergence).toBeCloseTo(GM.expected.indexDivergence, 3);
    expect(gate.warnings.join(' ')).toMatch(/do NOT use the index LTP as spot/i);
  });

  it('blocks when the futures cross-check is missing', () => {
    const g = checkGate({
      parityForward: fwd.forward,
      listedFuture: null,
      perStrikeSpread: fwd.spread,
    });
    expect(g.blocked).toBe(true);
    expect(g.reasons.join(' ')).toMatch(/No futures quote/i);
  });

  it('blocks on a >75 point divergence and never falls back to the index', () => {
    const g = checkGate({
      parityForward: fwd.forward,
      listedFuture: fwd.forward + 120,
      perStrikeSpread: fwd.spread,
      indexLtp: GM.indexLtp,
    });
    expect(g.blocked).toBe(true);
    expect(g.reasons.join(' ')).toMatch(/diverges from listed/i);
  });

  it('blocks on a >40 point parity spread', () => {
    const g = checkGate({
      parityForward: fwd.forward,
      listedFuture: GM.futuresLtp,
      perStrikeSpread: 55,
    });
    expect(g.blocked).toBe(true);
    expect(g.reasons.join(' ')).toMatch(/parity spread/i);
  });
});

describe('golden master — 74500 CE Greeks', () => {
  const fwd = parityForward([...GM.chain], GM.atmHint, GM.r, T);
  const F = fwd.forward;
  const K = 74500;
  const iv = b76IV(461.55, F, K, T, GM.r, 'CE');

  it('inverts to 10.5167% IV', () => {
    expect(iv).not.toBeNull();
    expect(iv! * 100).toBeCloseTo(GM.expected.ce74500.ivPct, 3);
  });

  it('reproduces delta, theta and vega', () => {
    const g = b76Greeks(F, K, T, iv!, GM.r, 'CE');
    expect(g.delta).toBeCloseTo(GM.expected.ce74500.delta, 4);
    expect(g.theta).toBeCloseTo(GM.expected.ce74500.thetaPerDay, 3);
    // Brief quotes 37.838; the exact value is 37.8390. Last-digit rounding in
    // the brief, not a model difference — assert to 1e-2.
    expect(g.vega).toBeCloseTo(GM.expected.ce74500.vega, 2);
  });

  it('prices back to the observed premium', () => {
    expect(b76Price(F, K, T, iv!, GM.r, 'CE')).toBeCloseTo(461.55, 6);
  });
});

describe('golden master — call/put IV agreement', () => {
  const fwd = parityForward([...GM.chain], GM.atmHint, GM.r, T);
  const F = fwd.forward;

  it.each(GM.chain.map((l) => [l.strike, l.callPrice, l.putPrice] as const))(
    'strike %i: CE and PE IV agree within 0.5 IV points',
    (K, C, P) => {
      const ivC = b76IV(C, F, K, T, GM.r, 'CE');
      const ivP = b76IV(P, F, K, T, GM.r, 'PE');
      expect(ivC).not.toBeNull();
      expect(ivP).not.toBeNull();
      expect(Math.abs(ivC! - ivP!) * 100).toBeLessThanOrEqual(
        GM.expected.maxCePeIvGapPct,
      );
    },
  );

  it('agrees to machine precision at the strike nearest the forward', () => {
    const ivC = b76IV(461.55, F, 74500, T, GM.r, 'CE')!;
    const ivP = b76IV(345.1, F, 74500, T, GM.r, 'PE')!;
    expect(Math.abs(ivC - ivP) * 100).toBeLessThan(1e-3);
  });
});

describe('put-call parity holds on COMPUTED prices', () => {
  it('C - P == DF*(F-K) across strikes and vols', () => {
    const F = 74616.5745;
    for (const K of [73000, 74000, 74500, 75000, 76000]) {
      for (const sigma of [0.05, 0.1055, 0.25, 0.6]) {
        const C = b76Price(F, K, T, sigma, GM.r, 'CE');
        const P = b76Price(F, K, T, sigma, GM.r, 'PE');
        expect(C - P).toBeCloseTo(DF * (F - K), 8);
      }
    }
  });
});

describe('b76IV inverts b76Price', () => {
  it('round-trips over a wide vol and moneyness grid', () => {
    const F = 74616.5745;
    let checked = 0;
    for (const K of [72000, 74000, 74600, 75200, 77000]) {
      for (const sigma of [0.03, 0.08, 0.1055, 0.3, 0.9]) {
        for (const type of ['CE', 'PE'] as const) {
          const px = b76Price(F, K, T, sigma, GM.r, type);

          // Volatility lives in the TIME VALUE, not the price. Where time
          // value collapses below a tick, sigma is not recoverable from the
          // premium by any method, and that shows up in both tails:
          //   deep OTM  K=77000 sigma=0.03 -> price 2.6e-13 BELOW ZERO, as
          //             F*N(d1) and K*N(d2) are both ~1e-11 and cancel;
          //   deep ITM  K=72000 sigma=0.03 -> price ~2613.78, large but
          //             sitting exactly at discounted intrinsic.
          // Filtering on price alone catches the first and misses the second.
          const { lower } = noArbBounds(F, K, T, GM.r, type);
          if (px - lower < 0.05) continue;

          const back = b76IV(px, F, K, T, GM.r, type);
          expect(back).not.toBeNull();
          expect(back!).toBeCloseTo(sigma, 6);
          checked++;
        }
      }
    }
    expect(checked).toBeGreaterThan(30);
  });

  it('degrades safely on sub-tick and cancellation-negative premia', () => {
    const F = 74616.5745;
    // Deep OTM, low vol, days from expiry: the analytic price underflows.
    const px = b76Price(F, 77000, T, 0.03, GM.r, 'CE');
    expect(px).toBeLessThan(0.05);
    // Whatever comes back must be a harmless near-zero or an explicit null —
    // never a plausible-looking fabricated volatility.
    const iv = b76IV(px, F, 77000, T, GM.r, 'CE');
    expect(iv === null || iv < 1e-6).toBe(true);
  });

  it('returns null outside the no-arbitrage bounds', () => {
    const F = 74616.5745;
    const K = 74500;
    expect(b76IV(DF * F + 10, F, K, T, GM.r, 'CE')).toBeNull();
    expect(b76IV(-1, F, K, T, GM.r, 'CE')).toBeNull();
    expect(b76IV(DF * K + 10, F, K, T, GM.r, 'PE')).toBeNull();
  });
});

describe('analytic Greeks match central differences', () => {
  const F = 74616.5745;
  const K = 74500;
  const sigma = 0.105167;

  it.each(['CE', 'PE'] as const)('%s delta', (type) => {
    const h = 1;
    const up = b76Price(F + h, K, T, sigma, GM.r, type);
    const dn = b76Price(F - h, K, T, sigma, GM.r, type);
    expect(b76Greeks(F, K, T, sigma, GM.r, type).delta).toBeCloseTo(
      (up - dn) / (2 * h),
      6,
    );
  });

  it.each(['CE', 'PE'] as const)('%s gamma', (type) => {
    const h = 1;
    const up = b76Price(F + h, K, T, sigma, GM.r, type);
    const mid = b76Price(F, K, T, sigma, GM.r, type);
    const dn = b76Price(F - h, K, T, sigma, GM.r, type);
    expect(b76Greeks(F, K, T, sigma, GM.r, type).gamma).toBeCloseTo(
      (up - 2 * mid + dn) / (h * h),
      8,
    );
  });

  it.each(['CE', 'PE'] as const)('%s vega, per 1 IV point', (type) => {
    const h = 1e-5;
    const up = b76Price(F, K, T, sigma + h, GM.r, type);
    const dn = b76Price(F, K, T, sigma - h, GM.r, type);
    expect(b76Greeks(F, K, T, sigma, GM.r, type).vega).toBeCloseTo(
      (up - dn) / (2 * h) / 100,
      5,
    );
  });

  it.each(['CE', 'PE'] as const)('%s theta, bumped by ONE HOUR', (type) => {
    // Theta is strongly convex in T this close to expiry. A 1-day bump straddles
    // that curvature and makes a correct analytic theta look wrong; 1 hour stays
    // inside the locally-linear region.
    const hourInYears = 1 / (365 * 24);
    const up = b76Price(F, K, T + hourInYears, sigma, GM.r, type);
    const dn = b76Price(F, K, T - hourInYears, sigma, GM.r, type);
    const fdThetaPerYear = -(up - dn) / (2 * hourInYears);
    expect(b76Greeks(F, K, T, sigma, GM.r, type).theta).toBeCloseTo(
      fdThetaPerYear / 365,
      2,
    );
  });

  it.each(['CE', 'PE'] as const)('%s rho, Black-76 convention -T*V', (type) => {
    const g = b76Greeks(F, K, T, sigma, GM.r, type);
    expect(g.rho).toBeCloseTo((-T * g.price) / 100, 12);
  });

  it('decay term is IDENTICAL for call and put', () => {
    const c = b76Greeks(F, K, T, sigma, GM.r, 'CE');
    const p = b76Greeks(F, K, T, sigma, GM.r, 'PE');
    const decay = b76DecayTermAnnual(F, K, T, sigma, GM.r);
    expect(c.theta * 365 - GM.r * c.price).toBeCloseTo(decay, 8);
    expect(p.theta * 365 - GM.r * p.price).toBeCloseTo(decay, 8);
    // Total theta differs ONLY by r*(V_C - V_P)/365.
    expect(c.theta - p.theta).toBeCloseTo((GM.r * (c.price - p.price)) / 365, 10);
  });
});
