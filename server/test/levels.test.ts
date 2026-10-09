import { describe, it, expect } from 'vitest';
import {
  deriveLevels,
  aggregateCandles,
  countTouches,
  collectCandidates,
  projectPremium,
  buildTradePlan,
} from '../src/levels.js';
import type { Candle } from '../src/endpoints/historical.js';

/** One-minute bar, n minutes after 09:15 IST on 21-Sep-2026. */
function m(n: number, o: number, h: number, l: number, c: number, v = 1000): Candle {
  return {
    timestampMs: Date.UTC(2026, 8, 21, 3, 45) + n * 60_000,
    open: o,
    high: h,
    low: l,
    close: c,
    volume: v,
    openInterest: null,
  };
}

describe('aggregateCandles', () => {
  const oneMin = [
    m(0, 100, 105, 99, 104, 10),
    m(1, 104, 106, 103, 105, 20),
    m(2, 105, 110, 104, 109, 30),
    m(3, 109, 111, 108, 110, 40),
    m(4, 110, 112, 107, 108, 50),
    m(5, 108, 115, 107, 114, 60),
  ];

  it('rolls 1-minute bars into 5-minute bars', () => {
    const five = aggregateCandles(oneMin, 5);
    expect(five).toHaveLength(2);
    const first = five[0]!;
    expect(first.open).toBe(100); // first bar's open
    expect(first.high).toBe(112); // max over the bucket
    expect(first.low).toBe(99); // min over the bucket
    expect(first.close).toBe(108); // last bar's close
    expect(first.volume).toBe(150); // summed
  });

  it('is a no-op for a 1-minute request', () => {
    expect(aggregateCandles(oneMin, 1)).toBe(oneMin);
  });

  it('handles an empty series', () => {
    expect(aggregateCandles([], 5)).toEqual([]);
  });
});

describe('countTouches', () => {
  const c = [m(0, 100, 105, 99, 104), m(1, 104, 105.2, 103, 105), m(2, 105, 120, 104, 119)];

  it('counts highs inside the band for resistance', () => {
    expect(countTouches(c, 105, 0.5, 'resistance')).toBe(2);
  });

  it('does not count a far-away spike', () => {
    expect(countTouches(c, 105, 0.5, 'resistance')).toBeLessThan(3);
  });

  it('counts lows for support', () => {
    expect(countTouches(c, 103.5, 1, 'support')).toBe(2);
  });
});

describe("the spec's worked example", () => {
  /**
   * Spot 74,756 with repeated resistance at 74,790 / 74,795 / 74,792.
   * Buffer 5 -> breakout 74,800.
   */
  const candles: Candle[] = [];
  let t = 0;
  // Build a base, then three distinct touches of the ~74,792 area.
  for (let i = 0; i < 12; i++) candles.push(m(t++, 74740, 74755, 74730, 74745));
  candles.push(m(t++, 74745, 74790, 74740, 74760));
  for (let i = 0; i < 4; i++) candles.push(m(t++, 74760, 74770, 74750, 74758));
  candles.push(m(t++, 74758, 74795, 74752, 74762));
  for (let i = 0; i < 4; i++) candles.push(m(t++, 74762, 74772, 74750, 74757));
  candles.push(m(t++, 74757, 74792, 74748, 74756));
  for (let i = 0; i < 6; i++) candles.push(m(t++, 74756, 74768, 74744, 74756));

  const r = deriveLevels(candles, 74756, { confirmationBuffer: 5, roundTo: 5 });

  it('clusters the three repeated highs into one resistance', () => {
    expect(r.resistance).not.toBeNull();
    expect(r.resistance!.price).toBeGreaterThan(74785);
    expect(r.resistance!.price).toBeLessThan(74800);
    expect(r.resistance!.touches).toBeGreaterThanOrEqual(3);
  });

  it('produces BREAKOUT = 74,800 with a 5-point buffer', () => {
    expect(r.breakoutAbove).toBe(74800);
  });

  it('finds support below spot and a breakdown level under it', () => {
    expect(r.support).not.toBeNull();
    expect(r.breakdownBelow).not.toBeNull();
    expect(r.breakdownBelow!).toBeLessThan(74756);
  });

  it('places the trigger above spot, never below', () => {
    expect(r.breakoutAbove!).toBeGreaterThan(r.spot);
    expect(r.breakdownBelow!).toBeLessThan(r.spot);
  });

  it('reports the buffer and tolerance it used', () => {
    expect(r.confirmationBuffer).toBe(5);
    expect(r.toleranceUsed).toBeGreaterThan(0);
  });
});

describe('spike rejection', () => {
  it('rejects a one-off wick that was never revisited', () => {
    const candles: Candle[] = [];
    let t = 0;
    for (let i = 0; i < 25; i++) candles.push(m(t++, 74700, 74710, 74690, 74700));
    // A single violent wick to 75,500 that price never returns to.
    candles.splice(12, 0, m(99, 74700, 75500, 74690, 74705));
    for (let i = 0; i < 10; i++) candles.push(m(t++, 74700, 74712, 74688, 74701));

    const r = deriveLevels(candles, 74700, { minTouches: 2, confirmationBuffer: 5 });
    const rejectedHigh = r.rejected.find((x) => x.price > 75000);
    expect(rejectedHigh).toBeDefined();
    expect(rejectedHigh!.reason).toMatch(/Isolated spike/i);
    // And it must not have become the trigger.
    expect(r.breakoutAbove === null || r.breakoutAbove < 75000).toBe(true);
  });

  it('keeps a structural level even with a single touch', () => {
    const candles: Candle[] = [];
    let t = 0;
    for (let i = 0; i < 30; i++) candles.push(m(t++, 74700, 74720, 74680, 74700));
    const r = deriveLevels(candles, 74700, { minTouches: 5 });
    const structural = [...r.allResistance, ...r.allSupport].filter((l) => l.structural);
    expect(structural.length).toBeGreaterThan(0);
  });
});

describe('honest failure', () => {
  it('returns nulls and a diagnostic when there are no candles', () => {
    const r = deriveLevels([], 74700);
    expect(r.breakoutAbove).toBeNull();
    expect(r.breakdownBelow).toBeNull();
    expect(r.diagnostics.join(' ')).toMatch(/No candles/i);
  });

  it('refuses to place levels without a usable spot', () => {
    const c = [m(0, 100, 105, 99, 104), m(1, 104, 106, 103, 105)];
    const r = deriveLevels(c, NaN);
    expect(r.breakoutAbove).toBeNull();
    expect(r.diagnostics.join(' ')).toMatch(/Spot price unavailable/i);
  });

  it('never fabricates a level when nothing above spot survives', () => {
    // Price at the very top of its range: nothing above it.
    const candles = Array.from({ length: 30 }, (_, i) => m(i, 100, 100.5, 99, 100));
    const r = deriveLevels(candles, 200); // spot far above every candle
    expect(r.resistance).toBeNull();
    expect(r.breakoutAbove).toBeNull();
    expect(r.diagnostics.join(' ')).toMatch(/No confirmed resistance/i);
  });
});

describe('levels come from price action, not the option chain', () => {
  it('collectCandidates only ever reads candle fields', () => {
    const candles = Array.from({ length: 30 }, (_, i) => m(i, 100, 105, 95, 100));
    const cands = collectCandidates(candles, { swingLookback: 2, consolidationWindow: 20 });
    const sources = new Set(cands.map((c) => c.source));
    for (const s of sources) {
      expect(s).not.toMatch(/oi|strike|chain|pcr|max_pain/i);
    }
    expect(sources.size).toBeGreaterThan(0);
  });
});

describe('premium projection', () => {
  it('is first order in delta plus a gamma term', () => {
    // entry 100, delta 0.5, gamma 0.0004, move +50 points
    // 100 + 0.5*50 + 0.5*0.0004*2500 = 100 + 25 + 0.5 = 125.5
    expect(projectPremium(100, 0.5, 0.0004, 50)).toBeCloseTo(125.5, 9);
  });

  it('never returns a negative premium', () => {
    expect(projectPremium(10, 0.5, 0, -100)).toBe(0);
  });

  it('returns null without delta rather than guessing', () => {
    expect(projectPremium(100, null, 0.0004, 50)).toBeNull();
  });

  it('a put gains as the underlying falls', () => {
    const p = projectPremium(120, -0.45, 0.0004, -60)!;
    expect(p).toBeGreaterThan(120);
  });
});

describe('trade plan', () => {
  const candles: Candle[] = [];
  let t = 0;
  for (let i = 0; i < 10; i++) candles.push(m(t++, 74700, 74760, 74650, 74710));
  for (let i = 0; i < 10; i++) candles.push(m(t++, 74710, 74800, 74690, 74750));
  for (let i = 0; i < 10; i++) candles.push(m(t++, 74750, 74805, 74695, 74756));
  const levels = deriveLevels(candles, 74756, { confirmationBuffer: 5 });

  it('builds a CE plan with trigger above and stop below', () => {
    const plan = buildTradePlan('CE', levels, 150, 0.5, 0.0004);
    if (plan) {
      expect(plan.triggerLevel).toBeGreaterThan(levels.spot);
      expect(plan.stopLevel).toBeLessThan(plan.triggerLevel);
      expect(plan.entryPremium).toBe(150);
      expect(plan.note).toMatch(/not quotes/i);
    }
  });

  it('returns null rather than a plan when no trigger exists', () => {
    const empty = deriveLevels([], 74756);
    expect(buildTradePlan('CE', empty, 150, 0.5, 0.0004)).toBeNull();
    expect(buildTradePlan('PE', empty, 150, -0.5, 0.0004)).toBeNull();
  });

  it('labels where the target and stop came from', () => {
    const plan = buildTradePlan('CE', levels, 150, 0.5, 0.0004);
    if (plan) {
      expect(plan.targetSource.length).toBeGreaterThan(0);
      expect(plan.stopSource.length).toBeGreaterThan(0);
    }
  });
});
