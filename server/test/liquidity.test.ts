import { describe, it, expect } from 'vitest';
import { walkBook, assessDepth, screenChain } from '../src/liquidity.js';
import type { CanonicalQuote, ChainStrike, DepthLevel } from '../src/types.js';

const prov = { fetchId: 'test', epochMs: 1_758_000_000_000, source: 'dhan-rest-v2' as const, endpoint: '/t' };

/**
 * A thin book: tight at level 1, much worse behind it. This is the shape that
 * makes spread-only screening dangerous.
 */
const asks: DepthLevel[] = [
  { price: 100.0, quantity: 20, orders: 1 },
  { price: 100.5, quantity: 40, orders: 2 },
  { price: 101.5, quantity: 60, orders: 3 },
  { price: 103.0, quantity: 80, orders: 4 },
  { price: 105.0, quantity: 100, orders: 5 },
];
const bids: DepthLevel[] = [
  { price: 99.8, quantity: 20, orders: 1 },
  { price: 99.3, quantity: 40, orders: 2 },
  { price: 98.3, quantity: 60, orders: 3 },
  { price: 96.8, quantity: 80, orders: 4 },
  { price: 94.8, quantity: 100, orders: 5 },
];

function quoteWithBook(): CanonicalQuote {
  return {
    securityId: '999',
    segment: 'BSE_FNO',
    ltp: 100,
    ohlc: null,
    volume: 1000,
    oi: 5000,
    oiDayHigh: null,
    oiDayLow: null,
    averagePrice: null,
    buyQuantity: null,
    sellQuantity: null,
    netChange: null,
    upperCircuit: null,
    lowerCircuit: null,
    lastTradeTimeMs: prov.epochMs,
    depth: { buy: bids, sell: asks },
    provenance: prov,
    quarantined: {},
  };
}

describe('walkBook', () => {
  it('fills from the best level first', () => {
    const w = walkBook(asks, 20);
    expect(w.filledQuantity).toBe(20);
    expect(w.vwap).toBeCloseTo(100.0, 10);
    expect(w.levelsConsumed).toBe(1);
    expect(w.incomplete).toBe(false);
  });

  it('walks into worse levels when size exceeds the top', () => {
    const w = walkBook(asks, 60);
    // 20 @ 100.0 + 40 @ 100.5 = 6020 / 60
    expect(w.vwap).toBeCloseTo(6020 / 60, 10);
    expect(w.levelsConsumed).toBe(2);
  });

  it('skips empty levels rather than treating a zero price as free', () => {
    const padded = [...asks, { price: 0, quantity: 0, orders: 0 }];
    expect(walkBook(padded, 20).vwap).toBeCloseTo(100.0, 10);
  });

  it('reports a shortfall when the visible book cannot cover the size', () => {
    const w = walkBook(asks, 1000);
    expect(w.incomplete).toBe(true);
    expect(w.shortfall).toBe(1000 - 300);
    expect(w.filledQuantity).toBe(300);
  });
});

describe('assessDepth — the defect spread-only screening cannot see', () => {
  const lotSize = 20;

  it('1 lot costs roughly the quoted spread', () => {
    const a = assessDepth(quoteWithBook(), 1, lotSize);
    expect(a.quotedSpreadPct).toBeCloseTo(((100.0 - 99.8) / 99.9) * 100, 6);
    expect(a.effectiveBuy).toBeCloseTo(100.0, 6);
    expect(a.effectiveSell).toBeCloseTo(99.8, 6);
    expect(a.roundtripPct).toBeCloseTo(((100.0 - 99.8) / 99.9) * 100, 6);
  });

  it('round-trip cost MORE THAN DOUBLES from 1 lot to 10 lots', () => {
    const one = assessDepth(quoteWithBook(), 1, lotSize);
    const ten = assessDepth(quoteWithBook(), 10, lotSize);
    expect(ten.roundtripPct!).toBeGreaterThan(2 * one.roundtripPct!);
    // And the quoted spread is identical in both cases — it cannot distinguish them.
    expect(ten.quotedSpreadPct).toBeCloseTo(one.quotedSpreadPct!, 10);
  });

  it('flags when real cost is a multiple of the quoted spread', () => {
    const ten = assessDepth(quoteWithBook(), 10, lotSize);
    expect(ten.slippageMultiple!).toBeGreaterThan(2);
    expect(ten.reasons.join(' ')).toMatch(/x the quoted spread/);
  });

  it('grades A for cheap size and degrades as size grows', () => {
    const one = assessDepth(quoteWithBook(), 1, lotSize);
    const ten = assessDepth(quoteWithBook(), 10, lotSize);
    expect(one.grade).toBe('A');
    expect(['B', 'C', 'F']).toContain(ten.grade);
  });

  it('grades F when the book cannot fill the size at all', () => {
    const huge = assessDepth(quoteWithBook(), 100, lotSize);
    expect(huge.grade).toBe('F');
    expect(huge.reasons.join(' ')).toMatch(/covers only/);
  });

  it('grades F with a reason when there is no depth block', () => {
    const q = quoteWithBook();
    q.depth = null;
    const a = assessDepth(q, 1, lotSize);
    expect(a.grade).toBe('F');
    expect(a.roundtripPct).toBeNull();
  });
});

describe('screenChain — Stage 1 costs nothing', () => {
  const leg = (bid: number, ask: number, oi: number, sid: string) => ({
    securityId: sid,
    lastPrice: (bid + ask) / 2,
    oi,
    previousOi: null,
    volume: 100,
    previousVolume: null,
    previousClosePrice: null,
    averagePrice: null,
    topBidPrice: bid,
    topBidQuantity: 50,
    topAskPrice: ask,
    topAskQuantity: 50,
    vendorQuarantined: { impliedVolatility: 0, greeks: null, reason: 'test' },
  });

  const strikes: ChainStrike[] = [
    { strike: 74400, ce: leg(99.8, 100.0, 5000, '1'), pe: leg(50.0, 50.2, 4000, '2') },
    { strike: 74500, ce: leg(80.0, 90.0, 10, '3'), pe: leg(60.0, 60.2, 6000, '4') }, // wide CE
    { strike: 74600, ce: leg(0, 0, 0, '5'), pe: leg(70.0, 70.3, 7000, '6') }, // no book
  ];

  it('accepts tight two-sided legs and rejects wide or empty ones', () => {
    const { all, candidates } = screenChain(strikes, { maxQuotedSpreadPct: 1.0 });
    const wide = all.find((c) => c.strike === 74500 && c.type === 'CE')!;
    const empty = all.find((c) => c.strike === 74600 && c.type === 'CE')!;
    expect(wide.candidate).toBe(false);
    expect(wide.rejectReasons.join(' ')).toMatch(/Quoted spread/);
    expect(empty.candidate).toBe(false);
    expect(candidates.every((c) => c.candidate)).toBe(true);
  });

  it('restricts to a band around the reference when asked', () => {
    const { all } = screenChain(strikes, { reference: 74400, bandPct: 0.001 });
    expect(all.every((c) => Math.abs(c.strike - 74400) <= 74400 * 0.001)).toBe(true);
  });

  it('caps how many candidates Stage 2 may cost', () => {
    const { candidates } = screenChain(strikes, { maxCandidates: 1 });
    expect(candidates.length).toBeLessThanOrEqual(1);
  });
});
