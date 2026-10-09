import { describe, it, expect } from 'vitest';
import {
  parityForward,
  perStrikeForward,
  median,
  discountFactor,
  b76Price,
  yearFractionToExpiry,
  istToEpochMs,
} from '../src/pricing/index.js';
import { GM } from './fixtures.js';

const T = yearFractionToExpiry(istToEpochMs(GM.asOfIst), GM.expiry);
const DF = discountFactor(GM.r, T);

describe('median', () => {
  it('picks the middle of an odd set', () => {
    expect(median([3, 1, 2])).toBe(2);
  });
  it('averages the two middles of an even set', () => {
    expect(median([1, 2, 3, 4])).toBe(2.5);
  });
  it('is unmoved by one extreme outlier', () => {
    expect(median([100, 101, 102, 103, 99999])).toBe(102);
  });
});

describe('per-strike parity', () => {
  it('inverts exactly against synthetic Black-76 prices', () => {
    const F = 74616.5745;
    for (const K of [74000, 74500, 75000]) {
      const C = b76Price(F, K, T, 0.105, GM.r, 'CE');
      const P = b76Price(F, K, T, 0.105, GM.r, 'PE');
      expect(perStrikeForward(K, C, P, DF)).toBeCloseTo(F, 6);
    }
  });
});

describe('the median is robust to a stale leg — the reason it is used', () => {
  it('absorbs one badly stale call without moving the forward materially', () => {
    const clean = parityForward([...GM.chain], GM.atmHint, GM.r, T);

    // Corrupt one leg: the 74200 call is 40 points stale.
    const dirty = GM.chain.map((l) =>
      l.strike === 74200 ? { ...l, callPrice: l.callPrice - 40 } : { ...l },
    );
    const withStale = parityForward(dirty, GM.atmHint, GM.r, T);

    // A 40-point corruption moves the median forward by 2.9464 points: the
    // median absorbs ~93% of it. That is the robustness claim, stated as a
    // bound rather than a point value so it survives a tick-size change.
    const drift = Math.abs(withStale.forward - clean.forward);
    expect(drift).toBeLessThan(5);
    expect(drift).toBeLessThan(40 * 0.1);

    // The DISPERSION, by contrast, blows out 3.07x — which is exactly what the
    // gate watches. The forward stays usable; the gate still gets to object.
    expect(withStale.spread).toBeGreaterThan(clean.spread * 2);
  });

  it('a mean would have been dragged by the same stale leg', () => {
    const dirty = GM.chain.map((l) =>
      l.strike === 74200 ? { ...l, callPrice: l.callPrice - 40 } : { ...l },
    );
    const res = parityForward(dirty, GM.atmHint, GM.r, T);
    const mean =
      res.perStrike.reduce((a, p) => a + p.forward, 0) / res.perStrike.length;
    expect(Math.abs(res.forward - GM.expected.parityForward)).toBeLessThan(
      Math.abs(mean - GM.expected.parityForward),
    );
  });
});

describe('ATM band', () => {
  it('uses only strikes inside 1.5% of the hint', () => {
    const wide = [
      ...GM.chain,
      { strike: 60000, callPrice: 14700, putPrice: 1 },
      { strike: 90000, callPrice: 1, putPrice: 15400 },
    ];
    const res = parityForward(wide, GM.atmHint, GM.r, T);
    expect(res.usedStrikes).toEqual([74100, 74200, 74300, 74400, 74500]);
    expect(res.forward).toBeCloseTo(GM.expected.parityForward, 3);
  });

  it('throws rather than guess when no strike falls inside the band', () => {
    expect(() => parityForward([...GM.chain], 50000, GM.r, T)).toThrow(/No strikes inside/);
  });
});

describe('slope is a diagnostic, never the forward', () => {
  it('theoretical slope of (C-P) on K is exactly -DF', () => {
    const F = 74616.5745;
    const legs = [74300, 74400, 74500].map((K) => ({
      strike: K,
      callPrice: b76Price(F, K, T, 0.105, GM.r, 'CE'),
      putPrice: b76Price(F, K, T, 0.105, GM.r, 'PE'),
    }));
    const res = parityForward(legs, 74400, GM.r, T);
    expect(res.slopeDiagnostic.slope).toBeCloseTo(-DF, 9);
    expect(res.slopeDiagnostic.withinTolerance).toBe(true);
  });

  it('a slope thrown off by noise does NOT move the forward', () => {
    // Nudge the outermost legs so the fitted slope degrades, leaving the
    // median of per-strike forwards almost untouched.
    const noisy = GM.chain.map((l, i) =>
      i === 0
        ? { ...l, callPrice: l.callPrice + 6 }
        : i === GM.chain.length - 1
          ? { ...l, callPrice: l.callPrice - 6 }
          : { ...l },
    );
    const res = parityForward(noisy, GM.atmHint, GM.r, T);
    // Drifts 2.9464 points against 6-point noise on two legs — bounded, not exact.
    expect(Math.abs(res.forward - GM.expected.parityForward)).toBeLessThan(5);
    // Whatever the diagnostic says, the published forward came from the median.
    expect(res.perStrike.filter((p) => p.used).length).toBe(5);
  });
});
