/**
 * Regression tests for defects found in the Phase-1 audit. Each block names
 * the defect it pins down; every test here FAILED against the pre-fix code.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { rmSync } from 'node:fs';

import { Transport } from '../src/transport.js';
import { RateLimiter } from '../src/ratelimit.js';
import { TokenProvider, loadSettings, createLogger, type Settings } from '../src/config.js';
import { ApiError, AuthRejectedError } from '../src/errors.js';
import { vwap, sessionVwap, computeIndicators } from '../src/indicators.js';
import { deriveLevels, buildTradePlan, type LevelResult } from '../src/levels.js';
import { selectStrike } from '../src/strikeSelect.js';
import type { PricedLeg } from '../src/pricingBridge.js';
import type { DepthAssessment } from '../src/liquidity.js';
import { PriceHistory } from '../src/priceHistory.js';
import { ExpiryCache } from '../src/instruments/expiries.js';
import { Bull50DhanClient } from '../src/client.js';
import type { Candle } from '../src/endpoints/historical.js';
import type { MarketSnapshot } from '../src/snapshot.js';

const settings: Settings = { ...loadSettings(), logLevel: 'silent' };
const log = createLogger(settings);

/** A 1-minute bar at IST wall-clock `day` `hh:mm`. */
function bar(day: string, hh: number, mm: number, o: number, h: number, l: number, c: number, v: number | null = null): Candle {
  const ms = Date.parse(`${day}T${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:00+05:30`);
  return { timestampMs: ms, open: o, high: h, low: l, close: c, volume: v, openInterest: null };
}

// ---------------------------------------------------------------------------
// Transport: network faults and Dhan error-code families
// ---------------------------------------------------------------------------

describe('transport error handling', () => {
  afterEach(() => vi.unstubAllGlobals());

  function makeTransport() {
    const tokens = new TokenProvider(log);
    tokens.setToken('test-token-0123456789abcdef', 'CLIENT');
    return new Transport(settings, tokens, new RateLimiter(10_000, log), log);
  }
  const ok = () =>
    new Response(JSON.stringify({ status: 'success', data: { x: 1 } }), { status: 200 });
  const dhanErr = (status: number, errorCode: string) =>
    new Response(JSON.stringify({ status: 'failure', errorCode, errorMessage: `code ${errorCode}` }), {
      status,
    });

  it('retries a network failure (fetch TypeError) instead of throwing on the first try', async () => {
    const fetchStub = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('fetch failed'))
      .mockResolvedValueOnce(ok());
    vi.stubGlobal('fetch', fetchStub);
    const r = await makeTransport().post<{ x: number }>({ path: '/charts/intraday', body: {}, limitClass: 'data' });
    expect(r.data.x).toBe(1);
    expect(fetchStub).toHaveBeenCalledTimes(2);
  });

  it('treats Data API 807 (token expired) as an auth failure', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(dhanErr(400, '807')));
    await expect(
      makeTransport().post({ path: '/charts/intraday', body: {}, limitClass: 'data' }),
    ).rejects.toBeInstanceOf(AuthRejectedError);
  });

  it('does NOT report 811 (invalid expiry date) as a credentials problem', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(dhanErr(400, '811')));
    const err = await makeTransport()
      .post({ path: '/optionchain', body: {}, limitClass: 'data' })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(AuthRejectedError);
  });

  it('reports 806 / DH-902 as a data-plan problem, not an expired token, and does not retry', async () => {
    const fetchStub = vi.fn().mockResolvedValue(dhanErr(403, '806'));
    vi.stubGlobal('fetch', fetchStub);
    const err = (await makeTransport()
      .post({ path: '/marketfeed/quote', body: {}, limitClass: 'data' })
      .catch((e: unknown) => e)) as Error;
    expect(err).toBeInstanceOf(ApiError);
    expect(err).not.toBeInstanceOf(AuthRejectedError);
    expect(err.message).toMatch(/Data API plan/);
    expect(fetchStub).toHaveBeenCalledTimes(1);
  });

  it('backs off and retries on Data API 805 (too many requests)', async () => {
    const fetchStub = vi
      .fn()
      .mockResolvedValueOnce(dhanErr(400, '805'))
      .mockResolvedValueOnce(ok());
    vi.stubGlobal('fetch', fetchStub);
    const r = await makeTransport().post<{ x: number }>({ path: '/charts/intraday', body: {}, limitClass: 'data' });
    expect(r.data.x).toBe(1);
    expect(fetchStub).toHaveBeenCalledTimes(2);
  });
});

// ---------------------------------------------------------------------------
// VWAP must be the SESSION VWAP, not a multi-day VWAP
// ---------------------------------------------------------------------------

describe('session VWAP', () => {
  // Yesterday traded around 74,000 on heavy volume; today around 75,000.
  const series: Candle[] = [
    bar('2026-09-17', 10, 0, 74000, 74010, 73990, 74000, 10_000),
    bar('2026-09-17', 10, 1, 74000, 74010, 73990, 74000, 10_000),
    bar('2026-09-18', 10, 0, 75000, 75010, 74990, 75000, 100),
    bar('2026-09-18', 10, 1, 75000, 75010, 74990, 75000, 100),
  ];

  it('uses only the most recent IST session', () => {
    expect(sessionVwap(series)).toBeCloseTo(75000, 6);
    // The plain helper over the whole series is the multi-day number.
    expect(vwap(series)!).toBeLessThan(74100);
  });

  it('computeIndicators reports the session VWAP', () => {
    expect(computeIndicators(series)!.vwap).toBeCloseTo(75000, 6);
  });

  it('the level engine places its VWAP level at the session VWAP', () => {
    const r = deriveLevels(series, 75005, { tolerance: 2 });
    const all = [...r.allResistance, ...r.allSupport];
    const v = all.find((l) => l.sources.includes('vwap'));
    expect(v).toBeDefined();
    expect(v!.price).toBeGreaterThan(74900);
  });
});

// ---------------------------------------------------------------------------
// Trade-plan premium projection must expand around SPOT
// ---------------------------------------------------------------------------

describe('trade-plan premium projection', () => {
  // Spot 74,756; resistance 74,795 -> trigger 74,800; support 74,690.
  const levels: LevelResult = {
    spot: 74756,
    atr14: 30,
    toleranceUsed: 5,
    confirmationBuffer: 5,
    resistance: { price: 74795, kind: 'resistance', touches: 3, sources: ['swing_high'], structural: false, tolerance: 5, distanceFromSpot: 39, distanceInAtr: 1.3, note: '' },
    support: { price: 74690, kind: 'support', touches: 3, sources: ['swing_low'], structural: false, tolerance: 5, distanceFromSpot: -66, distanceInAtr: -2.2, note: '' },
    breakoutAbove: 74800,
    breakdownBelow: 74685,
    nextResistance: { price: 74880, kind: 'resistance', touches: 2, sources: ['swing_high'], structural: false, tolerance: 5, distanceFromSpot: 124, distanceInAtr: 4.1, note: '' },
    nextSupport: null,
    allResistance: [],
    allSupport: [],
    rejected: [],
    barsAnalyzed: 100,
    timeframeMinutes: 1,
    diagnostics: [],
  };

  it('prices the CE stop at the premium the option will have when the index is AT the stop', () => {
    const plan = buildTradePlan('CE', levels, 125, 0.5, 0)!;
    // Index at the 74,690 support is 66 points below spot: 125 - 0.5*66 = 92.
    expect(plan.stopPremium).toBeCloseTo(92, 9);
    // Index at the 74,880 target is 124 points above spot: 125 + 0.5*124 = 187.
    expect(plan.targetPremium).toBeCloseTo(187, 9);
    // At the 74,800 trigger: 125 + 0.5*44 = 147.
    expect(plan.triggerPremium).toBeCloseTo(147, 9);
  });

  it('keeps the stop premium below, and the target premium above, the trigger premium', () => {
    const plan = buildTradePlan('CE', levels, 125, 0.5, 0.0004)!;
    expect(plan.stopPremium!).toBeLessThan(plan.triggerPremium!);
    expect(plan.targetPremium!).toBeGreaterThan(plan.triggerPremium!);
  });

  it('projects a PE from spot too', () => {
    const plan = buildTradePlan('PE', levels, 100, -0.5, 0)!;
    // trigger 74,685 is 71 below spot: 100 + 35.5; stop at resistance 74,795: 100 - 19.5.
    expect(plan.triggerPremium).toBeCloseTo(135.5, 9);
    expect(plan.stopPremium).toBeCloseTo(80.5, 9);
  });
});

// ---------------------------------------------------------------------------
// Strike selection: unknown liquidity cannot pass the grade floor
// ---------------------------------------------------------------------------

describe('strike selection liquidity floor', () => {
  const leg = (strike: number, id: string): PricedLeg => ({
    strike,
    type: 'CE',
    securityId: id,
    marketPrice: 200,
    ivPct: 12,
    delta: 0.5,
    gamma: 0.0004,
    vega: 30,
    theta: -40,
    rho: -0.1,
    moneyness: 1,
    vendorIvPct: null,
    vendorIvDeltaPct: null,
  });
  const graded = (id: string, grade: DepthAssessment['grade']): DepthAssessment => ({
    securityId: id,
    lots: 1,
    lotSize: 20,
    quantity: 20,
    bestBid: 199,
    bestAsk: 201,
    midPrice: 200,
    quotedSpreadPct: 1,
    effectiveBuy: 210,
    effectiveSell: 190,
    roundtripPct: 10,
    slippageMultiple: 10,
    grade,
    reasons: [],
    buyWalk: null,
    sellWalk: null,
  });

  it('does not pick an unassessed leg when every assessed leg failed the floor', () => {
    const depth = new Map([['1', graded('1', 'F')]]);
    const r = selectStrike([leg(74700, '1'), leg(74800, '2')], 'CE', 74750, depth, new Map());
    expect(r.best).toBeNull();
    const unassessed = r.ranked.find((c) => c.securityId === '2')!;
    expect(unassessed.eligible).toBe(false);
    expect(unassessed.rejectReasons.join(' ')).toMatch(/unverified/);
  });
});

// ---------------------------------------------------------------------------
// Rolling history: futures day range is futures-space and session-wide
// ---------------------------------------------------------------------------

describe('rolling history and the futures day range', () => {
  const file = join(tmpdir(), `bull50-regress-${process.pid}.json`);
  const fresh = () => {
    rmSync(file, { force: true });
    return new PriceHistory(90, file);
  };

  it('does not stamp an unchanged futures day high onto every index bar', () => {
    const h = fresh();
    const base = Date.now() - 20 * 60_000;
    // Index oscillates 74,290..74,310; futures trade 300 above; the futures
    // day high (74,700 => 74,400 in index space) was made before we started.
    for (let i = 0; i < 12; i++) {
      const idx = 74300 + (i % 2 === 0 ? -10 : 10);
      h.record({ t: base + i * 60_000, index: idx, forward: null, futures: idx + 300, dayHigh: 74700, dayLow: 74200 });
    }
    const bars = h.toSyntheticCandles(1);
    expect(Math.max(...bars.map((b) => b.high))).toBe(74310);
    expect(Math.min(...bars.map((b) => b.low))).toBe(74290);
    // Pre-fix, every bar's high was 74,700 and this "level" had 12 touches.
    const r = deriveLevels(bars, 74300, { minTouches: 2 });
    expect(r.allResistance.some((l) => l.price > 74500)).toBe(false);
  });

  it('widens a bar by a NEW day extreme, converted into index space', () => {
    const h = fresh();
    const t0 = Date.now() - 5 * 60_000;
    h.record({ t: t0, index: 74300, forward: null, futures: 74600, dayHigh: 74650, dayLow: 74500 });
    // Between scans the futures made a new high at 74,680 (= 74,380 index).
    h.record({ t: t0 + 60_000, index: 74320, forward: null, futures: 74620, dayHigh: 74680, dayLow: 74500 });
    const bars = h.toSyntheticCandles(1);
    expect(bars[1]!.high).toBe(74380);
    expect(bars[0]!.high).toBe(74300);
  });

  it('returns reference levels in index space', () => {
    const h = fresh();
    h.record({ t: Date.now(), index: 74300, forward: null, futures: 74600, dayHigh: 74700, dayLow: 74450 });
    const refs = h.referenceLevels();
    expect(refs.dayHigh).toBe(74400);
    expect(refs.dayLow).toBe(74150);
  });
});

// ---------------------------------------------------------------------------
// Expiry day: the scalping scope is the NEXT weekly
// ---------------------------------------------------------------------------

describe('expiry resolution', () => {
  const fakeTransport = {
    post: vi.fn(async () => ({ data: ['2026-10-08', '2026-10-15', '2026-10-22'], receivedAtMs: Date.now() })),
  } as unknown as Transport;

  it('nextAfter skips the contract expiring today', async () => {
    const c = new ExpiryCache(fakeTransport, 60_000, log);
    expect(await c.nextAfter(51, 'IDX_I', '2026-10-08')).toBe('2026-10-15');
    expect(await c.nearest(51, 'IDX_I', '2026-10-08')).toBe('2026-10-08');
  });
});

// ---------------------------------------------------------------------------
// get_analysis with series="futures": levels in futures space
// ---------------------------------------------------------------------------

describe('get_analysis on the futures series', () => {
  it('places levels against the FUTURES LTP, not the index LTP', async () => {
    process.env['DHAN_HISTORY_FILE'] = join(tmpdir(), `bull50-regress-client-${process.pid}.json`);
    rmSync(process.env['DHAN_HISTORY_FILE'], { force: true });
    const client = new Bull50DhanClient(settings);

    // Futures oscillate between 74,550 and 74,650 (index ~300 lower).
    const candles: Candle[] = [];
    for (let i = 0; i < 40; i++) {
      const up = i % 4 < 2;
      candles.push(bar('2026-10-09', 10, i, 74600, up ? 74650 : 74610, up ? 74590 : 74550, 74600, 1000));
    }

    const snap = {
      underlying: 'SENSEX',
      expiry: '2026-10-15',
      fetchId: 'f',
      epochMs: Date.now(),
      source: 'dhan-rest-v2',
      chain: { strikes: [], underlyingLtpDoNotUseAsSpot: 74300 },
      futures: { ltp: 74600, ohlc: null },
      pricing: {
        forward: 74580,
        T: 0.01,
        calendarDaysToExpiry: 6,
        listedFuture: 74600,
        indexLtpDoNotUseAsSpot: 74300,
        gate: { divergenceVsFuture: 20, indexDivergence: 280, warnings: [] },
        forwardDetail: { spread: 5 },
        atmStrike: 74600,
        legs: [],
      },
      blocked: null,
      liquidity: { screened: [], candidates: [], depth: [], lots: 1, note: '' },
      integrity: { ok: true, findings: [], checkedAtMs: Date.now() },
      warnings: [],
    } as unknown as MarketSnapshot;

    vi.spyOn(client, 'getMarketSnapshot').mockResolvedValue(snap);
    const getCandles = vi
      .spyOn(client, 'getCandles')
      .mockResolvedValue({ candles, indicators: null } as unknown as Awaited<ReturnType<Bull50DhanClient['getCandles']>>);

    const a = (await client.getAnalysis({ underlying: 'SENSEX', series: 'futures' })) as {
      levels: LevelResult;
      levelPriceSpace: string;
    };

    // The candles requested are the snapshot's own futures contract.
    expect(getCandles.mock.calls[0]![0].futuresMonth).toBe('OCT');
    expect(a.levelPriceSpace).toBe('futures');
    expect(a.levels.spot).toBe(74600);
    // Pre-fix: spot 74,300 put every level ABOVE spot, so there was no support.
    expect(a.levels.breakdownBelow).not.toBeNull();
    expect(a.levels.breakoutAbove!).toBeGreaterThan(74600);
    expect(a.levels.breakdownBelow!).toBeLessThan(74600);
  });
});
