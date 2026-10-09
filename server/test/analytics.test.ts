import { describe, it, expect } from 'vitest';
import {
  calculatePcr,
  calculateMaxPain,
  classifyBuildup,
  analyzePositioning,
  analyzeConcentration,
  analyzeChain,
} from '../src/analytics.js';
import {
  findSwingPoints,
  classifyStructure,
  measureConsolidation,
  candidateLevels,
  analyzeStructure,
} from '../src/structure.js';
import type { ChainStrike, ChainLeg } from '../src/types.js';
import type { Candle } from '../src/endpoints/historical.js';

function leg(over: Partial<ChainLeg> = {}): ChainLeg {
  return {
    securityId: '1',
    lastPrice: 100,
    oi: 1000,
    previousOi: 1000,
    volume: 500,
    previousVolume: 400,
    previousClosePrice: 100,
    averagePrice: 100,
    topBidPrice: 99.5,
    topBidQuantity: 40,
    topAskPrice: 100.5,
    topAskQuantity: 40,
    vendorQuarantined: { impliedVolatility: 0, greeks: null, reason: 'test' },
    ...over,
  };
}

function strike(k: number, ce: Partial<ChainLeg> | null, pe: Partial<ChainLeg> | null): ChainStrike {
  return {
    strike: k,
    ce: ce ? leg({ securityId: `${k}1`, ...ce }) : null,
    pe: pe ? leg({ securityId: `${k}2`, ...pe }) : null,
  };
}

describe('PCR', () => {
  const chain = [
    strike(74000, { oi: 100, volume: 10 }, { oi: 200, volume: 40 }),
    strike(74100, { oi: 300, volume: 30 }, { oi: 400, volume: 60 }),
  ];

  it('is total put OI over total call OI', () => {
    const r = calculatePcr(chain);
    expect(r.totalCallOi).toBe(400);
    expect(r.totalPutOi).toBe(600);
    expect(r.oiPcr).toBeCloseTo(1.5, 10);
  });

  it('computes a separate volume PCR', () => {
    expect(calculatePcr(chain).volumePcr).toBeCloseTo(100 / 40, 10);
  });

  it('returns null rather than Infinity when call OI is zero', () => {
    const r = calculatePcr([strike(74000, { oi: 0 }, { oi: 500 })]);
    expect(r.oiPcr).toBeNull();
  });
});

describe('max pain', () => {
  it('lands where writer payout is smallest', () => {
    // Huge put OI low, huge call OI high -> pain minimised between them.
    const chain = [
      strike(74000, { oi: 10 }, { oi: 1000 }),
      strike(74100, { oi: 100 }, { oi: 100 }),
      strike(74200, { oi: 1000 }, { oi: 10 }),
    ];
    const r = calculateMaxPain(chain, 74100);
    expect(r.maxPainStrike).toBe(74100);
    expect(r.distanceFromReference).toBe(0);
  });

  it('computes pain for every listed strike', () => {
    const chain = [
      strike(74000, { oi: 10 }, { oi: 1000 }),
      strike(74100, { oi: 100 }, { oi: 100 }),
    ];
    expect(calculateMaxPain(chain, 74050).painByStrike).toHaveLength(2);
  });

  it('is undefined — not zero — when there is no OI at all', () => {
    const r = calculateMaxPain([strike(74000, { oi: 0 }, { oi: 0 })], 74000);
    expect(r.maxPainStrike).toBeNull();
    expect(r.note).toMatch(/undefined/i);
  });

  it('records the reference it was computed against', () => {
    const r = calculateMaxPain([strike(74000, { oi: 5 }, { oi: 5 })], 74616.57);
    expect(r.reference).toBe(74616.57);
    expect(r.note).toMatch(/parity forward, never/i);
  });
});

describe('buildup classification', () => {
  it.each([
    [10, 500, 'long_buildup'],
    [-10, 500, 'short_buildup'],
    [10, -500, 'short_covering'],
    [-10, -500, 'long_unwinding'],
    [0, 0, 'neutral'],
  ])('price %s, OI %s -> %s', (p, o, expected) => {
    expect(classifyBuildup(p, o)).toBe(expected);
  });

  it('is unknown when either input is missing, never guessed', () => {
    expect(classifyBuildup(null, 500)).toBe('unknown');
    expect(classifyBuildup(10, null)).toBe('unknown');
  });

  it('derives price and OI change from the chain fields', () => {
    const chain = [
      strike(74000, { lastPrice: 120, previousClosePrice: 100, oi: 1500, previousOi: 1000 }, null),
    ];
    const p = analyzePositioning(chain);
    expect(p).toHaveLength(1);
    expect(p[0]!.priceChange).toBe(20);
    expect(p[0]!.oiChange).toBe(500);
    expect(p[0]!.buildup).toBe('long_buildup');
    expect(p[0]!.priceChangePct).toBeCloseTo(20, 9);
    expect(p[0]!.oiChangePct).toBeCloseTo(50, 9);
  });
});

describe('OI concentration', () => {
  const chain = [
    strike(74000, { oi: 100 }, { oi: 9000, previousOi: 1000 }),
    strike(74100, { oi: 200 }, { oi: 300 }),
    strike(74500, { oi: 8000, previousOi: 500 }, { oi: 50 }),
  ];

  it('finds the call and put OI peaks', () => {
    const r = analyzeConcentration(chain);
    expect(r.callOiPeakStrike).toBe(74500);
    expect(r.putOiPeakStrike).toBe(74000);
  });

  it('finds the largest OI additions separately from raw OI', () => {
    const r = analyzeConcentration(chain);
    expect(r.topCallOiAdds[0]!.strike).toBe(74500);
    expect(r.topCallOiAdds[0]!.oiChange).toBe(7500);
    expect(r.topPutOiAdds[0]!.strike).toBe(74000);
  });

  it('labels the support/resistance reading as convention, not derivation', () => {
    expect(analyzeConcentration(chain).note).toMatch(/convention, not a derivation/i);
  });
});

describe('analyzeChain emits no verdict', () => {
  const chain = [
    strike(74000, { oi: 100, lastPrice: 110, previousClosePrice: 100, previousOi: 50 }, { oi: 900 }),
    strike(74100, { oi: 200 }, { oi: 300 }),
  ];

  it('tallies buildups across all legs', () => {
    const r = analyzeChain(chain, 74050);
    const total = Object.values(r.buildupTally).reduce((a, b) => a + b, 0);
    expect(total).toBe(r.positioning.length);
    expect(r.buildupTally.long_buildup).toBeGreaterThanOrEqual(1);
  });

  it('contains no bullish/bearish/signal language', () => {
    const json = JSON.stringify(analyzeChain(chain, 74050));
    expect(json).not.toMatch(/bullish|bearish|\bsignal\b|breakout/i);
  });
});

// ---------------------------------------------------------------------------
// structure
// ---------------------------------------------------------------------------

function bar(min: number, o: number, h: number, l: number, c: number, v = 1000): Candle {
  const base = Date.UTC(2026, 8, 21, 3, 45) + min * 60_000; // 09:15 IST
  return { timestampMs: base, open: o, high: h, low: l, close: c, volume: v, openInterest: null };
}

describe('swing points', () => {
  it('finds a peak surrounded by lower bars', () => {
    const c = [
      bar(0, 100, 101, 99, 100),
      bar(5, 100, 102, 99, 101),
      bar(10, 101, 110, 100, 108), // the peak
      bar(15, 108, 105, 100, 102),
      bar(20, 102, 103, 98, 99),
    ];
    const s = findSwingPoints(c, 2);
    expect(s).toHaveLength(1);
    expect(s[0]!.kind).toBe('high');
    expect(s[0]!.price).toBe(110);
  });

  it('cannot confirm a swing in the last lookback bars — by design', () => {
    const c = [
      bar(0, 100, 101, 99, 100),
      bar(5, 100, 102, 99, 101),
      bar(10, 101, 120, 100, 118), // would be a peak, but sits at the edge
      bar(15, 118, 119, 117, 118),
    ];
    // Only 4 bars with lookback 2: index 2 has no right-hand confirmation.
    expect(findSwingPoints(c, 2)).toHaveLength(0);
  });

  it('returns nothing on a series too short to have swings', () => {
    expect(findSwingPoints([bar(0, 1, 1, 1, 1)], 2)).toHaveLength(0);
  });
});

describe('structure classification', () => {
  it('is indeterminate without two highs and two lows', () => {
    expect(classifyStructure([]).pattern).toBe('indeterminate');
  });

  it('labels higher highs and higher lows', () => {
    const swings = [
      { index: 0, timestampMs: 0, istTime: '09:15', price: 100, kind: 'low' as const },
      { index: 1, timestampMs: 1, istTime: '09:20', price: 110, kind: 'high' as const },
      { index: 2, timestampMs: 2, istTime: '09:25', price: 105, kind: 'low' as const },
      { index: 3, timestampMs: 3, istTime: '09:30', price: 120, kind: 'high' as const },
    ];
    const r = classifyStructure(swings);
    expect(r.pattern).toBe('higher_highs_higher_lows');
    expect(r.highsRising).toBe(true);
    expect(r.lowsRising).toBe(true);
  });

  it('labels a contracting range', () => {
    const swings = [
      { index: 0, timestampMs: 0, istTime: '09:15', price: 100, kind: 'low' as const },
      { index: 1, timestampMs: 1, istTime: '09:20', price: 130, kind: 'high' as const },
      { index: 2, timestampMs: 2, istTime: '09:25', price: 110, kind: 'low' as const },
      { index: 3, timestampMs: 3, istTime: '09:30', price: 120, kind: 'high' as const },
    ];
    expect(classifyStructure(swings).pattern).toBe('lower_highs_higher_lows');
  });
});

describe('consolidation is measured, not declared', () => {
  it('reports the range in points, percent and ATR', () => {
    const c = Array.from({ length: 30 }, (_, i) => bar(i * 5, 100, 102, 98, 100));
    const r = measureConsolidation(c, 20)!;
    expect(r.high).toBe(102);
    expect(r.low).toBe(98);
    expect(r.rangePoints).toBe(4);
    expect(r.rangeInAtr).toBeCloseTo(1, 6); // ATR is 4 on this series
    expect(r.note).toMatch(/No threshold is applied/i);
  });
});

describe('candidate levels', () => {
  const c = [
    bar(0, 100, 105, 95, 102),
    bar(5, 102, 108, 101, 107),
    bar(10, 107, 112, 106, 110),
    bar(15, 110, 111, 104, 106),
    bar(20, 106, 109, 103, 108),
  ];

  it('enumerates levels with price position relative to each', () => {
    const r = candidateLevels(c)!;
    expect(r.lastClose).toBe(108);
    const labels = r.candidates.map((x) => x.label);
    expect(labels).toContain('session_high');
    expect(labels).toContain('session_low');
    expect(labels).toContain('opening_range_high');
    for (const cand of r.candidates) {
      expect(['above', 'below', 'at']).toContain(cand.side);
    }
  });

  it('sorts by proximity so the nearest level is first', () => {
    const r = candidateLevels(c)!;
    const d = r.candidates.map((x) => Math.abs(x.distancePoints));
    expect(d).toEqual([...d].sort((a, b) => a - b));
  });

  it('never nominates a trigger or declares a breakout', () => {
    const r = candidateLevels(c)!;
    expect(r.note).toMatch(/does not nominate a trigger/i);
    expect(JSON.stringify(r)).not.toMatch(/bullish|bearish|\bsignal\b|trigger_level/i);
  });

  it('returns null on an empty series rather than inventing levels', () => {
    expect(candidateLevels([])).toBeNull();
    expect(analyzeStructure([])).toBeNull();
  });
});
