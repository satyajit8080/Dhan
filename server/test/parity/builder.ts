/**
 * Parity fixture builder (Phase 4).
 *
 * Runs the EXISTING TypeScript implementation on fixed, sanitized inputs and
 * records { input, expected } for every case. The Python port is tested
 * against these files (engine/tests/test_parity_*.py). Expected values come
 * from TypeScript only — never from the Python implementation.
 *
 * Inputs are deterministic: fixed clocks, fixed fetch ids, no network. Market
 * data comes from the repo's own fixtures (GM 18-Sep-2026 close, the 21-Sep
 * refresh sample, the 18/21-Sep 5-minute SENSEX candles). Anything else is
 * labelled SYNTHETIC. Security ids are placeholders (9xxxxx), not real ids.
 *
 * Encoding: non-finite numbers -> {"$num": "NaN" | "Infinity" | "-Infinity"};
 * undefined -> {"$undefined": true}; a thrown error -> {"$error": {...}}.
 */

import { readFileSync } from 'node:fs';
import { GM } from '../fixtures.js';
import {
  normCdf, normPdf, b76Price, b76IV, b76Greeks, noArbBounds, discountFactor,
  expiryStampMs, yearFractionToExpiry, daysToExpiry, istToEpochMs,
  parityForward, median, perStrikeForward, checkGate,
} from '../../src/pricing/index.js';
import { normalizeChain, normalizeQuote, parseLastTradeTime } from '../../src/normalize.js';
import { checkChain, checkQuote, checkSnapshotSkew } from '../../src/integrity.js';
import { attachPricing, fairPrice } from '../../src/pricingBridge.js';
import { walkBook, assessDepth, screenChain } from '../../src/liquidity.js';
import {
  aggregateCandles, collectCandidates, countTouches, deriveLevels, buildTradePlan, projectPremium,
} from '../../src/levels.js';
import {
  atr, trueRanges, wilderSmooth, vwap, sessionVwap, sessions, openingRange, istDateOf, istTimeOf,
} from '../../src/indicators.js';
import { findSwingPoints } from '../../src/structure.js';
import { toCandles, type Candle } from '../../src/endpoints/historical.js';
import { ExpiryCache, isIsoDate } from '../../src/instruments/expiries.js';
import { createLogger, loadSettings } from '../../src/config.js';
import type { Transport } from '../../src/transport.js';
import type { CanonicalChain, CanonicalQuote } from '../../src/types.js';

// ------------------------------------------------------------------ encoding

export function enc(v: unknown): unknown {
  if (v === undefined) return { $undefined: true };
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return { $num: 'NaN' };
    if (v === Infinity) return { $num: 'Infinity' };
    if (v === -Infinity) return { $num: '-Infinity' };
    return Object.is(v, -0) ? 0 : v;
  }
  if (Array.isArray(v)) return v.map(enc);
  if (v instanceof Set) return [...v].map(enc);
  if (v && typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(v)) out[k] = enc((v as Record<string, unknown>)[k]);
    return out;
  }
  return v;
}

function run(fn: () => unknown): unknown {
  try {
    return enc(fn());
  } catch (e) {
    const err = e as Error & { reasons?: string[] };
    return {
      $error: {
        name: err.name,
        message: err.message,
        ...(err.reasons ? { reasons: err.reasons } : {}),
      },
    };
  }
}

async function runAsync(fn: () => Promise<unknown>): Promise<unknown> {
  try {
    return enc(await fn());
  } catch (e) {
    const err = e as Error;
    return { $error: { name: err.name, message: err.message } };
  }
}

type Case = { id: string; note?: string; input: unknown; expected: unknown };

// ------------------------------------------------------------- shared inputs

const candlesRaw = JSON.parse(
  readFileSync(new URL('../../../engine/samples/candles_raw.json', import.meta.url), 'utf8'),
) as unknown;

const GM_AS_OF = istToEpochMs(GM.asOfIst);

/** Dhan-shaped /optionchain `data` for the GM close. SYNTHETIC: ids, OI, depth. */
export function gmRawChain(spreadAbs = 0.05) {
  const oc: Record<string, unknown> = {};
  for (const l of GM.chain) {
    const leg = (price: number, id: number, vendorIv: number) => ({
      security_id: id,
      last_price: price,
      top_bid_price: Number((price - spreadAbs).toFixed(2)),
      top_ask_price: Number((price + spreadAbs).toFixed(2)),
      top_bid_quantity: 40,
      top_ask_quantity: 60,
      oi: 500000 + l.strike,
      previous_oi: 480000,
      volume: 2000000,
      previous_volume: 1000000,
      previous_close_price: price * 0.98,
      average_price: price,
      implied_volatility: vendorIv,
      greeks: { delta: 0.5, gamma: 0.0004, theta: -40, vega: 30 },
    });
    oc[`${l.strike}.000000`] = {
      ce: leg(l.callPrice, 900000 + l.strike / 100, 16.0),
      pe: leg(l.putPrice, 910000 + l.strike / 100, 8.6),
    };
  }
  return { last_price: GM.indexLtp, oc };
}

/** 21-Sep-2026 10:33 IST refresh sample (engine/run_refresh.py legs). */
const S21 = {
  asOfMs: 1789967033611,
  expiry: '2026-09-24',
  indexLtp: 74667.55,
  legs: [
    [74500, 445, 445.5, 445.7, 287.35, 286.7, 287.3],
    [74600, 387.75, 388.35, 388.35, 330, 329.35, 329.75],
    [74700, 335, 335.05, 335, 376.9, 376.3, 376.75],
    [74800, 286.35, 286.75, 286.75, 428.55, 428, 428.5],
  ] as const,
};
// leg tuple: strike, ceBid, ceAsk, ceLtp, peAsk, peBid, peLtp
export function s21RawChain() {
  const oc: Record<string, unknown> = {};
  for (const [k, cb, ca, cl, pa, pb, pl] of S21.legs) {
    oc[`${k}.000000`] = {
      ce: { security_id: 920000 + k / 100, last_price: cl, top_bid_price: cb, top_ask_price: ca,
            top_bid_quantity: 20, top_ask_quantity: 20, oi: 100000, previous_oi: 90000, volume: 50000 },
      pe: { security_id: 930000 + k / 100, last_price: pl, top_bid_price: pb, top_ask_price: pa,
            top_bid_quantity: 20, top_ask_quantity: 20, oi: 100000, previous_oi: 110000, volume: 50000 },
    };
  }
  return { last_price: S21.indexLtp, oc };
}

/** SYNTHETIC futures quote (Dhan-shaped). */
export function rawFut(ltp: number, extra: Record<string, unknown> = {}) {
  return {
    last_price: ltp,
    last_trade_time: '18/09/2026 15:29:58',
    ohlc: { open: ltp - 150, high: ltp + 120, low: ltp - 200, close: ltp - 30 },
    volume: 50000,
    oi: 120000,
    depth: {
      buy: [1, 2, 3, 4, 5].map((i) => ({ price: ltp - i * 0.5, quantity: 20 * i, orders: i })),
      sell: [1, 2, 3, 4, 5].map((i) => ({ price: ltp + i * 0.5, quantity: 20 * i, orders: i })),
    },
    ...extra,
  };
}

/** The repo's real 5-minute SENSEX candles, as Dhan /charts arrays (epoch seconds). */
export function rawCandles(filter: (iso: string) => boolean = () => true) {
  const rows = (candlesRaw as { candles: { datetime_ist: string; open: number; high: number; low: number; close: number; volume: number }[] }).candles
    .filter((c) => filter(c.datetime_ist));
  return {
    timestamp: rows.map((c) => istToEpochMs(c.datetime_ist) / 1000),
    open: rows.map((c) => c.open),
    high: rows.map((c) => c.high),
    low: rows.map((c) => c.low),
    close: rows.map((c) => c.close),
    volume: rows.map((c) => c.volume),
  };
}

const norm = (raw: unknown, expiry: string, epochMs: number, fetchId = 'chain') =>
  normalizeChain(raw, { underlying: 'SENSEX', underlyingScrip: 51, underlyingSeg: 'IDX_I', expiry },
    fetchId, epochMs, '/optionchain');
const normQ = (raw: unknown, epochMs: number, fetchId = 'futures', sid = 'FUT') =>
  normalizeQuote(raw, sid, 'BSE_FNO', fetchId, epochMs, '/marketfeed/quote');

/** 1-minute SYNTHETIC bar at IST `day hh:mm`. */
function bar(day: string, hh: number, mm: number, o: number, h: number, l: number, c: number, v: number | null = null): Candle {
  return { timestampMs: istToEpochMs(`${day}T${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:00`),
    open: o, high: h, low: l, close: c, volume: v, openInterest: null };
}

// ----------------------------------------------------------------- pricing

function pricingCases(): Case[] {
  const c: Case[] = [];
  const xs = [-40, -37.5, -10, -7.071, -7.07, -3, -1, -0.5, -1e-12, 0, 1e-12, 0.5, 1, 2.5, 7.0710678118, 7.1, 10, 37, 38, NaN];
  c.push({ id: 'normCdf/grid', input: enc(xs), expected: enc(xs.map(normCdf)) });
  c.push({ id: 'normPdf/grid', input: enc(xs), expected: enc(xs.map(normPdf)) });

  for (const d of ['2026-09-24', ' 2026-09-24 ', '2026-02-30', '2026-12-31', '24-09-2026', '2026-9-24']) {
    c.push({ id: `expiryStampMs/${d}`, input: d, expected: run(() => expiryStampMs(d)) });
  }
  for (const [now, exp] of [[GM_AS_OF, '2026-09-24'], [GM_AS_OF, '2026-09-18'], [S21.asOfMs, '2026-09-24'],
    [istToEpochMs('2026-09-24T15:29:00'), '2026-09-24'], [istToEpochMs('2026-09-24T15:31:00'), '2026-09-24']] as const) {
    c.push({ id: `time/${now}/${exp}`, input: { nowMs: now, expiry: exp },
      expected: run(() => ({ T: yearFractionToExpiry(now, exp), days: daysToExpiry(now, exp) })) });
  }
  for (const s of ['2026-09-18T15:30:00', '2026-09-18 15:30', '2026-09-18T09:15:07', '2026-13-01T00:00:00', 'bad']) {
    c.push({ id: `istToEpochMs/${s}`, input: s, expected: run(() => istToEpochMs(s)) });
  }

  const F = GM.expected.parityForward;
  const strikes = [60000, 72000, 74100, 74600, 74616.5745, 75000, 77000, 90000];
  const Ts = [-0.01, 0, 1e-6, 6 / 365, 0.5];
  const sigmas = [0, 0.03, 0.12, 0.5, 3];
  const grid: unknown[] = [];
  for (const K of strikes) for (const T of Ts) for (const s of sigmas) for (const t of ['CE', 'PE'] as const) {
    grid.push({ in: [F, K, T, s, GM.r, t], out: enc(b76Price(F, K, T, s, GM.r, t)) });
  }
  c.push({ id: 'b76Price/grid', input: null, expected: grid });

  const T6 = 6 / 365;
  const ivCases: unknown[] = [];
  for (const K of [74100, 74500, 74600, 75000, 77000]) for (const t of ['CE', 'PE'] as const) {
    const [lo, hi] = Object.values(noArbBounds(F, K, T6, GM.r, t)) as [number, number];
    for (const p of [lo - 1, lo, lo + 1e-9, lo + 0.5, b76Price(F, K, T6, 0.12, GM.r, t), hi, hi + 1, NaN, 0.05]) {
      ivCases.push({ in: enc([p, F, K, T6, GM.r, t]), out: enc(b76IV(p, F, K, T6, GM.r, t)) });
    }
  }
  for (const args of [[100, F, 74500, 0, GM.r, 'CE'], [100, -1, 74500, T6, GM.r, 'CE'], [100, F, 0, T6, GM.r, 'PE']] as const) {
    ivCases.push({ in: enc(args), out: enc(b76IV(...(args as unknown as Parameters<typeof b76IV>))) });
  }
  c.push({ id: 'b76IV/grid', input: null, expected: ivCases });

  const gk: unknown[] = [];
  for (const K of [72000, 74500, 74616.5745, 77000]) for (const s of [0.03, 0.12, 0.5]) for (const T of [1e-4, T6, 0.5]) for (const t of ['CE', 'PE'] as const) {
    gk.push({ in: [F, K, T, s, GM.r, t], out: enc(b76Greeks(F, K, T, s, GM.r, t)) });
  }
  c.push({ id: 'b76Greeks/grid', input: null, expected: gk });
  c.push({ id: 'discountFactor', input: [[GM.r, T6], [0, 1], [0.065, -1]],
    expected: [[GM.r, T6], [0, 1], [0.065, -1]].map(([r, T]) => discountFactor(r!, T!)) });

  c.push({ id: 'median', input: [[3, 1, 2], [4, 1, 3, 2], [5]], expected: [[3, 1, 2], [4, 1, 3, 2], [5]].map(median) });
  c.push({ id: 'median/empty', input: [], expected: run(() => median([])) });
  c.push({ id: 'perStrikeForward', input: [74500, 461.55, 345.1, 0.99893], expected: perStrikeForward(74500, 461.55, 345.1, 0.99893) });

  const T = yearFractionToExpiry(GM_AS_OF, GM.expiry);
  const pfCases: [string, unknown[], number, Record<string, number>][] = [
    ['gm', [...GM.chain], GM.atmHint, {}],
    ['gm-narrow-band', [...GM.chain], GM.atmHint, { bandPct: 0.0005 }],
    ['gm-far-hint', [...GM.chain], 80000, {}],
    ['single-strike', [GM.chain[2]], 74300, {}],
    ['even-count', GM.chain.slice(0, 4), 74300, {}],
    ['flat-strikes', [GM.chain[0], { ...GM.chain[0] }], 74100, {}],
  ];
  for (const [id, legs, hint, opts] of pfCases) {
    c.push({ id: `parityForward/${id}`, input: enc({ legs, atmHint: hint, r: GM.r, T, opts }),
      expected: run(() => parityForward(legs as never, hint, GM.r, T, opts)) });
  }

  const gateInputs: Record<string, Parameters<typeof checkGate>[0]> = {
    pass: { parityForward: 74616.5745, listedFuture: 74656.75, perStrikeSpread: 12.907, indexLtp: 74294.96, slopeWithinTolerance: true, slopeRelativeError: 0.01 },
    noFuture: { parityForward: 74616.5745, listedFuture: null, perStrikeSpread: 12.9 },
    nanFuture: { parityForward: 74616.5745, listedFuture: NaN, perStrikeSpread: 12.9 },
    divergence: { parityForward: 74616.5745, listedFuture: 75200, perStrikeSpread: 12.9 },
    divergenceExactly75: { parityForward: 74600, listedFuture: 74675, perStrikeSpread: 40 },
    spread: { parityForward: 74616.5745, listedFuture: 74650, perStrikeSpread: 40.0001 },
    calendarPass: { parityForward: 74616.57, listedFuture: 75000, perStrikeSpread: 5, futureExpiryGapYears: 35 / 365 },
    calendarFail: { parityForward: 74616.57, listedFuture: 75200, perStrikeSpread: 5, futureExpiryGapYears: 14 / 365 },
    calendarNegative: { parityForward: 74616.57, listedFuture: 73000, perStrikeSpread: 5, futureExpiryGapYears: 35 / 365 },
    calendarBoundaryGap: { parityForward: 74616.57, listedFuture: 74800, perStrikeSpread: 5, futureExpiryGapYears: 1.5 / 365 },
    slopeWarn: { parityForward: 74616.57, listedFuture: 74650, perStrikeSpread: 5, slopeWithinTolerance: false, slopeRelativeError: 0.0375 },
    slopeWarnNoErr: { parityForward: 74616.57, listedFuture: 74650, perStrikeSpread: 5, slopeWithinTolerance: false },
    indexNegativeLtp: { parityForward: 74616.57, listedFuture: 74650, perStrikeSpread: 5, indexLtp: -5 },
    thresholdOverride: { parityForward: 74616.57, listedFuture: 74700, perStrikeSpread: 5, thresholds: { maxFutureDivergence: 50 } },
    allBlocks: { parityForward: 74616.57, listedFuture: 76000, perStrikeSpread: 100, indexLtp: 74000, slopeWithinTolerance: false, slopeRelativeError: 0.5 },
  };
  for (const [id, gi] of Object.entries(gateInputs)) {
    c.push({ id: `checkGate/${id}`, input: enc(gi), expected: run(() => checkGate(gi)) });
  }
  return c;
}

// ------------------------------------------------------- normalize/integrity

const RAW_CHAINS: Record<string, unknown> = {
  gm: gmRawChain(),
  s21: s21RawChain(),
  nullRaw: null,
  emptyObject: {},
  emptyOc: { last_price: 74300, oc: {} },
  ocArray: { last_price: 74300, oc: [{ ce: { last_price: 10 } }] },
  ocNotObject: { oc: 'oops' },
  lastPriceString: { last_price: '74294.96', oc: {} },
  oddKeys: {
    last_price: 74300,
    oc: {
      '74200.000000': { ce: { last_price: 5 }, pe: { last_price: 6 } },
      abc: { ce: { last_price: 1 } },
      '': { ce: { last_price: 2 } },
      '74100': { ce: { last_price: 3 } },
      '74100.000000': { pe: { last_price: 4 } },
      ' 74300 ': { ce: { last_price: 7 } },
      '1e5': { ce: { last_price: 8 } },
      '0x10': { ce: { last_price: 9 } },
      Infinity: { ce: { last_price: 10 } },
      nan: { ce: { last_price: 11 } },
      '1_000': { ce: { last_price: 12 } },
    },
  },
  malformedLegs: {
    last_price: null,
    oc: {
      '74000': {
        ce: { security_id: 900001.0, last_price: '123.5', oi: 'abc', top_bid_price: true, top_ask_price: null,
              implied_volatility: 0, greeks: {} },
        pe: { security_id: '900002', last_price: -5, top_bid_price: 10, top_ask_price: 9,
              implied_volatility: 75, greeks: 0 },
      },
      '74100': { ce: [], pe: 'not-a-leg' },
      '74200': { ce: { last_price: 0, implied_volatility: '12.5', greeks: { delta: '0.5', gamma: null } }, pe: null },
      '74300': null,
      '74400': 5,
    },
  },
};

const RAW_QUOTES: Record<string, unknown> = {
  normal: rawFut(74656.75),
  missingLtp: (() => { const q = rawFut(74656.75) as Record<string, unknown>; delete q['last_price']; return q; })(),
  nullLtp: rawFut(74656.75, { last_price: null }),
  stringLtp: rawFut(74656.75, { last_price: '74656.75' }),
  neverTraded: rawFut(74656.75, { last_trade_time: '01/01/1980 00:00:00' }),
  badLtt: rawFut(74656.75, { last_trade_time: '2026-09-18 15:29:58' }),
  nullLtt: rawFut(74656.75, { last_trade_time: null }),
  overflowLtt: rawFut(74656.75, { last_trade_time: '31/02/2026 10:00:00' }),
  stale: rawFut(74656.75, { last_trade_time: '18/09/2026 15:00:00' }),
  crossed: rawFut(100, { depth: { buy: [{ price: 101, quantity: 1, orders: 1 }], sell: [{ price: 100.5, quantity: 1, orders: 1 }] } }),
  oneSided: rawFut(100, { depth: { buy: [{ price: 99, quantity: 5, orders: 1 }], sell: [{ price: 0, quantity: 0, orders: 0 }] } }),
  depthArray: rawFut(100, { depth: [1, 2] }),
  depthMalformed: rawFut(100, { depth: { buy: [null, 7, { price: '99.5', quantity: '10' }], sell: 'x' } }),
  ohlcPartial: rawFut(100, { ohlc: { high: 105 } }),
  ohlcEmpty: rawFut(100, { ohlc: {} }),
  nullRaw: null,
  emptyObject: {},
};

function normalizeCases(): Case[] {
  const c: Case[] = [];
  for (const [id, raw] of Object.entries(RAW_CHAINS)) {
    c.push({ id: `normalizeChain/${id}`, input: enc(raw), expected: run(() => norm(raw, '2026-09-24', GM_AS_OF)) });
  }
  for (const [id, raw] of Object.entries(RAW_QUOTES)) {
    c.push({ id: `normalizeQuote/${id}`, input: enc(raw), expected: run(() => normQ(raw, GM_AS_OF)) });
  }
  const ltts = ['18/09/2026 15:29:58', '01/01/1980 00:00:00', ' 18/09/2026 15:29:58 ', '18/09/2026  15:29:58',
    '31/02/2026 10:00:00', '18/09/2026 25:61:61', '18-09-2026 15:29:58', '', null, 12345, '1/9/2026 09:15:00',
    '01/01/0050 10:00:00', '29/02/2024 09:15:00'];
  c.push({ id: 'parseLastTradeTime', input: enc(ltts), expected: enc(ltts.map(parseLastTradeTime)) });

  const candleInputs: Record<string, unknown> = {
    real18Sep: rawCandles((s) => s.startsWith('2026-09-18')),
    ragged: { timestamp: [3, 1, 2], open: [1, 2, 3], high: [1, 2], low: [1, 2, 3], close: [1, 2, 3], volume: [5] },
    nonFiniteTs: { timestamp: [1, null, 'x', 4], open: [1, 2, 3, 4], high: [1, 2, 3, 4], low: [1, 2, 3, 4], close: [1, 2, 3, 4], open_interest: [9, 9, 9, 9] },
    empty: {},
    missingClose: { timestamp: [1], open: [1], high: [1], low: [1] },
  };
  for (const [id, raw] of Object.entries(candleInputs)) {
    c.push({ id: `toCandles/${id}`, input: enc(raw), expected: run(() => toCandles(raw as never)) });
  }
  return c;
}

function integrityCases(): Case[] {
  const c: Case[] = [];
  for (const [id, raw] of Object.entries(RAW_QUOTES)) {
    const q = normQ(raw, GM_AS_OF);
    c.push({ id: `checkQuote/${id}`, input: enc(raw), expected: run(() => checkQuote(q, GM_AS_OF + 1000, 15000)) });
  }
  // Freshness boundary: the 'normal' quote last traded 2 s before GM_AS_OF.
  // age === 15000 ms is NOT stale (rule is age > max); 15000.5 ms is.
  const ltt = GM_AS_OF - 2000;
  for (const [id, nowMs] of [['exactly15s', ltt + 15000], ['just-over-15s', ltt + 15000.5], ['future-ltt', ltt - 5000]] as const) {
    const q = normQ(RAW_QUOTES['normal'], GM_AS_OF);
    c.push({ id: `checkQuote/boundary-${id}`, input: enc({ raw: RAW_QUOTES['normal'], nowMs }), expected: run(() => checkQuote(q, nowMs, 15000)) });
  }
  for (const [id, raw] of Object.entries(RAW_CHAINS)) {
    const ch = norm(raw, '2026-09-24', GM_AS_OF);
    c.push({ id: `checkChain/${id}`, input: enc(raw), expected: run(() => checkChain(ch, GM_AS_OF)) });
  }
  const skews: [string, { fetchId: string; epochMs: number }, { fetchId: string; epochMs: number }, number][] = [
    ['same', { fetchId: 'a', epochMs: 1000 }, { fetchId: 'a', epochMs: 99999 }, 3000],
    ['inside', { fetchId: 'a', epochMs: 1000 }, { fetchId: 'b', epochMs: 4000 }, 3000],
    ['outside', { fetchId: 'a', epochMs: 1000 }, { fetchId: 'b', epochMs: 4001 }, 3000],
    ['negative', { fetchId: 'a', epochMs: 9000 }, { fetchId: 'b', epochMs: 1000 }, 3000],
  ];
  for (const [id, a, b, m] of skews) {
    c.push({ id: `checkSnapshotSkew/${id}`, input: { a, b, max: m }, expected: run(() => checkSnapshotSkew(a, b, m)) });
  }
  return c;
}

// ------------------------------------------------------------ pricing bridge

type BridgeCase = {
  chain: unknown; chainMs: number; expiry: string;
  fut: unknown | null; futMs: number; chainFetchId?: string; futFetchId?: string;
  opts: Record<string, unknown>;
};

function bridgeInputs(): Record<string, BridgeCase> {
  const base = { chain: gmRawChain(), chainMs: GM_AS_OF, expiry: GM.expiry, fut: rawFut(GM.futuresLtp), futMs: GM_AS_OF + 400, opts: {} };
  const staleLeg = gmRawChain();
  (staleLeg.oc['74300.000000'] as { ce: Record<string, unknown> }).ce['last_price'] = 400;
  (staleLeg.oc['74300.000000'] as { ce: Record<string, unknown> }).ce['top_bid_price'] = 300;
  (staleLeg.oc['74300.000000'] as { ce: Record<string, unknown> }).ce['top_ask_price'] = 500;
  const belowIntrinsic = gmRawChain();
  (belowIntrinsic.oc['74100.000000'] as { ce: Record<string, unknown> }).ce['last_price'] = 400;
  const wide = gmRawChain(40);
  const onePair = { last_price: 74300, oc: { '74300.000000': (gmRawChain().oc as Record<string, unknown>)['74300.000000'] } };
  return {
    gm: base,
    gmSameFetchId: { ...base, chainFetchId: 'one', futFetchId: 'one', futMs: GM_AS_OF + 60000 },
    s21: { chain: s21RawChain(), chainMs: S21.asOfMs, expiry: S21.expiry, fut: rawFut(74680), futMs: S21.asOfMs + 300, opts: {} },
    noFutures: { ...base, fut: null },
    futuresNoLtp: { ...base, fut: rawFut(1, { last_price: null }) },
    futuresDiverge: { ...base, fut: rawFut(75200) },
    calendarPass: { ...base, fut: rawFut(75000), opts: { futuresExpiry: '2026-10-29' } },
    calendarFail: { ...base, fut: rawFut(76500), opts: { futuresExpiry: '2026-10-29' } },
    skewTooLarge: { ...base, futMs: GM_AS_OF + 5000 },
    expiryDayBeforeClose: { ...base, chainMs: istToEpochMs('2026-09-24T15:29:00'), futMs: istToEpochMs('2026-09-24T15:29:01') },
    expiryDayAfterClose: { ...base, chainMs: istToEpochMs('2026-09-24T15:31:00'), futMs: istToEpochMs('2026-09-24T15:31:01') },
    onePair: { ...base, chain: onePair },
    emptyChain: { ...base, chain: { oc: {} } },
    staleLegSpread: { ...base, chain: staleLeg },
    belowIntrinsicLeg: { ...base, chain: belowIntrinsic },
    wideSpreadsUseLtp: { ...base, chain: wide },
    farAtmHint: { ...base, opts: { atmHint: 80000 } },
    customBand: { ...base, opts: { bandPct: 0.002 } },
    nowOverride: { ...base, opts: { nowMs: GM_AS_OF - 3 * 86_400_000 } },
    malformedChain: { ...base, chain: RAW_CHAINS['malformedLegs'] },
  };
}

function bridgeCases(): Case[] {
  const c: Case[] = [];
  for (const [id, b] of Object.entries(bridgeInputs())) {
    const ch = norm(b.chain, b.expiry, b.chainMs, b.chainFetchId ?? 'chain');
    const fq = b.fut === null ? null : normQ(b.fut, b.futMs, b.futFetchId ?? 'futures');
    c.push({
      id: `attachPricing/${id}`,
      input: enc(b),
      expected: run(() => attachPricing(ch as CanonicalChain, fq as CanonicalQuote | null,
        { riskFreeRate: 0.065, maxSnapshotSkewMs: 3000, ...b.opts } as never)),
    });
  }
  const legs: Record<string, unknown> = {
    tight: { topBidPrice: 99, topAskPrice: 101, lastPrice: 50 },
    exactly5pct: { topBidPrice: 97.5, topAskPrice: 102.5, lastPrice: 50 },
    wide: { topBidPrice: 90, topAskPrice: 110, lastPrice: 50 },
    crossed: { topBidPrice: 101, topAskPrice: 99, lastPrice: 50 },
    noBid: { topBidPrice: null, topAskPrice: 101, lastPrice: 50 },
    zeroLtpWide: { topBidPrice: 90, topAskPrice: 110, lastPrice: 0 },
    nullLeg: null,
  };
  for (const [id, leg] of Object.entries(legs)) {
    c.push({ id: `fairPrice/${id}`, input: enc(leg), expected: run(() => fairPrice(leg as never)) });
  }
  return c;
}

// ---------------------------------------------------------------- liquidity

function liquidityCases(): Case[] {
  const c: Case[] = [];
  const book = (bids: [number, number][], asks: [number, number][]) => ({
    last_price: 100, depth: { buy: bids.map(([p, q]) => ({ price: p, quantity: q, orders: 1 })),
      sell: asks.map(([p, q]) => ({ price: p, quantity: q, orders: 1 })) },
  });
  const quotes: Record<string, unknown> = {
    tightDeep: book([[99.9, 200], [99.8, 200]], [[100.1, 200], [100.2, 200]]),
    thin: book([[99.8, 20], [99, 20], [98, 20], [97, 20], [96, 20]], [[100.2, 20], [101, 20], [102, 20], [103, 20], [104, 20]]),
    unsorted: book([[98, 20], [99.8, 20]], [[101, 20], [100.2, 20]]),
    oneSided: book([[99, 100]], []),
    noDepth: { last_price: 100 },
    zerosInBook: book([[0, 10], [99, 0], [98.5, 50]], [[101.5, 50], [0, 0]]),
  };
  for (const [id, raw] of Object.entries(quotes)) {
    const q = normQ(raw, GM_AS_OF, 'd', '900001');
    for (const lots of [1, 2, 5, 10]) {
      c.push({ id: `assessDepth/${id}/${lots}`, input: enc({ raw, lots, lotSize: 20 }), expected: run(() => assessDepth(q, lots, 20)) });
    }
  }
  c.push({ id: 'walkBook/basic', input: null,
    expected: run(() => [walkBook([{ price: 10, quantity: 5, orders: 1 }, { price: 0, quantity: 5, orders: 1 }, { price: 11, quantity: 10, orders: 1 }], 12),
      walkBook([], 5), walkBook([{ price: 10, quantity: 5, orders: 1 }], 0)]) });

  const screens: [string, unknown, Record<string, unknown>][] = [
    ['gm-default', gmRawChain(), {}],
    ['gm-band', gmRawChain(), { reference: 74616.5745, bandPct: 0.002 }],
    ['gm-wide', gmRawChain(40), { maxQuotedSpreadPct: 1.5 }],
    ['gm-minOi', gmRawChain(), { minOi: 574300 }],
    ['gm-cap2', gmRawChain(), { maxCandidates: 2 }],
    ['malformed', RAW_CHAINS['malformedLegs'], {}],
    ['s21', s21RawChain(), { reference: 74658.89, bandPct: 0.02, maxCandidates: 12 }],
  ];
  for (const [id, raw, opts] of screens) {
    const ch = norm(raw, '2026-09-24', GM_AS_OF);
    c.push({ id: `screenChain/${id}`, input: enc({ raw, opts }), expected: run(() => screenChain(ch.strikes, opts)) });
  }
  return c;
}

// ------------------------------------------------------------------- levels

function candleSets(): Record<string, Candle[]> {
  const real18 = toCandles(rawCandles((s) => s.startsWith('2026-09-18')) as never);
  const real = toCandles(rawCandles((s) => istToEpochMs(s) <= S21.asOfMs) as never);
  const twoDay: Candle[] = [
    bar('2026-09-17', 10, 0, 74000, 74010, 73990, 74000, 10000), bar('2026-09-17', 10, 1, 74000, 74010, 73990, 74000, 10000),
    bar('2026-09-18', 10, 0, 75000, 75010, 74990, 75000, 100), bar('2026-09-18', 10, 1, 75000, 75010, 74990, 75000, 100),
  ];
  const oneMin: Candle[] = [];
  for (let i = 0; i < 40; i++) {
    const up = i % 4 < 2;
    oneMin.push(bar('2026-10-09', 10, i, 74600, up ? 74650 : 74610, up ? 74590 : 74550, 74600, 1000));
  }
  const spike: Candle[] = [];
  for (let i = 0; i < 30; i++) spike.push(bar('2026-09-18', 11, i, 74700, i === 15 ? 75500 : 74760, 74650, 74710, null));
  const flat: Candle[] = [];
  for (let i = 0; i < 20; i++) flat.push(bar('2026-09-18', 12, i, 100, 100, 100, 100, 0));
  return {
    real18Sep: real18,
    realTo21Sep1033: real,
    twoDayVwap: twoDay,
    oneMinOscillating: oneMin,
    spike,
    flat,
    single: [bar('2026-09-18', 9, 15, 1, 2, 0.5, 1.5)],
    two: [bar('2026-09-18', 9, 15, 1, 2, 0.5, 1.5), bar('2026-09-18', 9, 16, 1.5, 2.5, 1, 2)],
    empty: [],
  };
}

function levelsCases(): Case[] {
  const c: Case[] = [];
  const sets = candleSets();
  const spots: Record<string, number[]> = {
    real18Sep: [74294.96, 74600, 73000, 76000],
    realTo21Sep1033: [74667.55],
    twoDayVwap: [75005],
    oneMinOscillating: [74600, 74300],
    spike: [74756],
    flat: [100, 99, 101],
    single: [1.2],
    two: [1.7],
    empty: [74000],
  };
  for (const [name, candles] of Object.entries(sets)) {
    c.push({ id: `indicators/${name}`, input: enc(candles), expected: run(() => ({
      trueRanges: trueRanges(candles), wilder14: wilderSmooth(trueRanges(candles), 14), atr14: atr(candles, 14),
      atr3: atr(candles, 3), vwap: vwap(candles), sessionVwap: sessionVwap(candles), sessions: sessions(candles),
      openingRange15: openingRange(candles, 15), swings2: findSwingPoints(candles, 2), swings1: findSwingPoints(candles, 1),
      candidates: collectCandidates(candles, { swingLookback: 2, consolidationWindow: 20 }),
      istDates: candles.slice(0, 3).map((k) => [istDateOf(k.timestampMs), istTimeOf(k.timestampMs)]),
      agg5: aggregateCandles(candles, 5), agg15: aggregateCandles(candles, 15), agg1: aggregateCandles(candles, 1),
    })) });
    for (const spot of spots[name] ?? []) {
      c.push({ id: `deriveLevels/${name}/${spot}`, input: enc({ candles, spot, cfg: {} }), expected: run(() => deriveLevels(candles, spot, {})) });
    }
  }
  const cfgs: Record<string, Record<string, number>> = {
    computeLevelsDefaults: { confirmationBuffer: 5, minTouches: 2, roundTo: 5 },
    tol3: { tolerance: 3 },
    minTouches3: { minTouches: 3 },
    noRound: { roundTo: 0, confirmationBuffer: 0 },
    lookback1Window5: { swingLookback: 1, consolidationWindow: 5 },
    window0: { consolidationWindow: 0 },
    halfRounding: { roundTo: 10, confirmationBuffer: 2.5 },
  };
  for (const [id, cfg] of Object.entries(cfgs)) {
    c.push({ id: `deriveLevels/real18Sep/cfg-${id}`, input: enc({ candles: sets['real18Sep'], spot: 74294.96, cfg }),
      expected: run(() => deriveLevels(sets['real18Sep']!, 74294.96, cfg)) });
  }
  for (const spot of [NaN, 0, -1, Infinity]) {
    c.push({ id: `deriveLevels/badSpot/${spot}`, input: enc({ candles: sets['real18Sep'], spot, cfg: {} }),
      expected: run(() => deriveLevels(sets['real18Sep']!, spot, {})) });
  }
  c.push({ id: 'countTouches', input: null, expected: run(() => [
    countTouches(sets['real18Sep']!, 74600, 5, 'resistance'), countTouches(sets['real18Sep']!, 74500, 5, 'support'),
    countTouches([], 1, 1, 'support')]) });

  const lv = deriveLevels(sets['real18Sep']!, 74294.96, { confirmationBuffer: 5, minTouches: 2, roundTo: 5 });
  const lvEmpty = deriveLevels([], 74294.96);
  const lvNoAtr = deriveLevels(sets['single']!, 1.2);
  for (const [lid, levels] of Object.entries({ real: lv, empty: lvEmpty, noAtr: lvNoAtr })) {
    for (const [side, d, g] of [['CE', 0.52, 0.0004], ['PE', -0.48, 0.0004], ['CE', null, null], ['PE', -0.5, null]] as const) {
      c.push({ id: `buildTradePlan/${lid}/${side}/${d}/${g}`, input: enc({ levels, side, entry: 125, delta: d, gamma: g }),
        expected: run(() => buildTradePlan(side, levels, 125, d, g)) });
    }
  }
  c.push({ id: 'projectPremium', input: null, expected: run(() => [
    projectPremium(100, 0.5, 0.0004, 50), projectPremium(10, 0.5, 0, -100), projectPremium(100, null, 0.0004, 50),
    projectPremium(120, -0.45, 0.0004, -60), projectPremium(NaN, 0.5, null, 1)]) });
  return c;
}

// ----------------------------------------------------------------- expiries

async function expiryCases(): Promise<Case[]> {
  const c: Case[] = [];
  const isoInputs = ['2026-10-15', '2028-02-29', '2026-02-29', '2026-02-30', '2026-13-01', '15-10-2026',
    '2026-10-15 15:30:00', '', 'N/A', 20261015, null, '0000-01-01', '9999-12-31'];
  c.push({ id: 'isIsoDate', input: enc(isoInputs), expected: isoInputs.map(isIsoDate) });
  const log = createLogger({ ...loadSettings(), logLevel: 'silent' });
  const lists: [string, unknown, string][] = [
    ['expiryDay', ['2026-10-15', '2026-10-08', '2026-10-22'], '2026-10-08'],
    ['normalDay', ['2026-10-15', '2026-10-08', '2026-10-22'], '2026-10-09'],
    ['afterAll', ['2026-10-01', '2026-10-08'], '2026-10-09'],
    ['malformed', ['2026-10-01', 'N/A', '15-10-2026', 42, null, '2026-02-30', '2026-10-15'], '2026-10-09'],
    ['allMalformed', ['N/A'], '2026-10-09'],
    ['notList', { not: 'a list' }, '2026-10-09'],
    ['futureYears', ['2027-01-28', '2026-12-31', '2026-10-15'], '2026-12-01'],
  ];
  for (const [id, data, today] of lists) {
    const make = () => new ExpiryCache({ post: async () => ({ data, receivedAtMs: 1 }) } as unknown as Transport, 60_000, log);
    c.push({
      id: `expiries/${id}`,
      input: enc({ data, today }),
      expected: {
        list: await runAsync(async () => (await make().get(51, 'IDX_I')).expiries),
        nearest: await runAsync(() => make().nearest(51, 'IDX_I', today)),
        nextAfter: await runAsync(() => make().nextAfter(51, 'IDX_I', today)),
      },
    });
  }
  return c;
}

// --------------------------------------------------------------------- scan

/**
 * TypeScript composition of the refresh scan, mirroring the SKILL.md pipeline
 * (snapshot pricing + compute_levels defaults on 5-minute index candles).
 * Every number comes from the TypeScript functions above.
 */
function tsScan(s: {
  chain: unknown; fut: unknown | null; candles: unknown; expiry: string; strikes: number[];
  chainMs: number; futMs: number; futuresExpiry?: string;
}) {
  const ch = norm(s.chain, s.expiry, s.chainMs);
  const fq = s.fut === null ? null : normQ(s.fut, s.futMs);
  let p;
  try {
    p = attachPricing(ch, fq, { riskFreeRate: 0.065, maxSnapshotSkewMs: 3000,
      ...(s.futuresExpiry ? { futuresExpiry: s.futuresExpiry } : {}) });
  } catch (e) {
    const err = e as Error & { reasons?: string[] };
    if (err.name === 'GateBlockedError') return { status: 'BLOCKED', reasons: err.reasons };
    if (err.name === 'SnapshotSkewError') return { status: 'BLOCKED', reasons: [err.message] };
    if (err.name === 'ValidationError') return { status: 'ERROR', reasons: [err.message] };
    throw e;
  }
  const candles = toCandles(s.candles as never);
  const spot = ch.underlyingLtpDoNotUseAsSpot ?? NaN;
  const lv = deriveLevels(candles, spot, { confirmationBuffer: 5, minTouches: 2, roundTo: 5 });
  const legs: unknown[] = [];
  const excluded: unknown[] = [];
  for (const k of s.strikes) {
    for (const type of ['CE', 'PE'] as const) {
      const priced = p.legs.find((l) => l.strike === k && l.type === type);
      const rawLeg = ch.strikes.find((x) => x.strike === k)?.[type === 'CE' ? 'ce' : 'pe'] ?? null;
      if (!priced || priced.ivPct === null || !rawLeg) {
        excluded.push({ strike: k, type, reason: !priced ? 'no priced leg' : priced.ivPct === null ? 'no IV (outside no-arbitrage bounds)' : 'no chain leg' });
        continue;
      }
      legs.push({ strike: k, isCall: type === 'CE', ivPct: priced.ivPct,
        bid: rawLeg.topBidPrice ?? 0, ask: rawLeg.topAskPrice ?? 0, ltp: rawLeg.lastPrice });
    }
  }
  return {
    status: 'OK', forward: p.forward, T: p.T, df: p.discountFactor, atmStrike: p.atmStrike, candleRefPrice: spot,
    resistances: lv.allResistance.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
    supports: lv.allSupport.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
    atr: lv.atr14, barMinutes: lv.timeframeMinutes, legs, excludedLegs: excluded,
    snapshotMs: p.asOfMs, candlesMs: candles.length ? candles[candles.length - 1]!.timestampMs : null,
    gateWarnings: p.gate.warnings,
  };
}

function scanCases(): Case[] {
  const gm18 = rawCandles((x) => x.startsWith('2026-09-18'));
  const to21 = rawCandles((x) => istToEpochMs(x) <= S21.asOfMs);
  const gmStrikes = GM.chain.map((l) => l.strike);
  const s21Strikes = S21.legs.map((l) => l[0]);
  // SYNTHETIC deep-ITM strike outside the ±1.5% forward band: its call trades
  // BELOW intrinsic (no IV) but cannot move the forward, so the gate passes.
  const below = gmRawChain() as { last_price: number; oc: Record<string, unknown> };
  below.oc['70000.000000'] = {
    ce: { security_id: 970000, last_price: 4000, top_bid_price: 3990, top_ask_price: 4010, top_bid_quantity: 20, top_ask_quantity: 20 },
    pe: { security_id: 970001, last_price: 1.5, top_bid_price: 1.45, top_ask_price: 1.55, top_bid_quantity: 20, top_ask_quantity: 20 },
  };
  const scans: Record<string, Parameters<typeof tsScan>[0]> = {
    gm18Sep: { chain: gmRawChain(), fut: rawFut(GM.futuresLtp), candles: gm18, expiry: GM.expiry, strikes: gmStrikes, chainMs: GM_AS_OF, futMs: GM_AS_OF + 400 },
    s21Sep: { chain: s21RawChain(), fut: rawFut(74680), candles: to21, expiry: S21.expiry, strikes: s21Strikes, chainMs: S21.asOfMs, futMs: S21.asOfMs + 300 },
    futuresMissing: { chain: gmRawChain(), fut: null, candles: gm18, expiry: GM.expiry, strikes: gmStrikes, chainMs: GM_AS_OF, futMs: GM_AS_OF },
    futuresDiverge: { chain: gmRawChain(), fut: rawFut(75200), candles: gm18, expiry: GM.expiry, strikes: gmStrikes, chainMs: GM_AS_OF, futMs: GM_AS_OF + 400 },
    expired: { chain: gmRawChain(), fut: rawFut(GM.futuresLtp), candles: gm18, expiry: '2026-09-18', strikes: gmStrikes, chainMs: GM_AS_OF + 60000, futMs: GM_AS_OF + 60100 },
    noCandles: { chain: gmRawChain(), fut: rawFut(GM.futuresLtp), candles: {}, expiry: GM.expiry, strikes: gmStrikes, chainMs: GM_AS_OF, futMs: GM_AS_OF + 400 },
    staleCandles: { chain: s21RawChain(), fut: rawFut(74680), candles: gm18, expiry: S21.expiry, strikes: s21Strikes, chainMs: S21.asOfMs, futMs: S21.asOfMs + 300 },
    missingStrikeAndNoIv: { chain: below, fut: rawFut(GM.futuresLtp), candles: gm18, expiry: GM.expiry, strikes: [70000, 74000, 74300], chainMs: GM_AS_OF, futMs: GM_AS_OF + 400 },
    emptyChain: { chain: { oc: {} }, fut: rawFut(GM.futuresLtp), candles: gm18, expiry: GM.expiry, strikes: gmStrikes, chainMs: GM_AS_OF, futMs: GM_AS_OF + 400 },
  };
  return Object.entries(scans).map(([id, s]) => ({ id: `scan/${id}`, input: enc(s), expected: run(() => tsScan(s)) }));
}

// ----------------------------------------------------------------- jscompat

/** Raw JavaScript semantics the Python port emulates (engine/sensex/jscompat.py). */
function jsCompatCases(): Case[] {
  const nums = [-1.902587519025875e-6, 1e-7, 1.5e-7, 1e-6, 123.456, 1e21, 1.5e21, 2, 74100, 0.1, 100,
    1234567890123456789012, -0, 5e-324, -42.5, 0.000123, 1e300, 74616.57454418391, NaN, Infinity, -Infinity];
  const fixed: [number, number][] = [[0.125, 2], [0.135, 2], [-0.125, 2], [2.5, 0], [-2.5, 0], [-0.001, 2], [-0, 2],
    [74616.5745, 4], [12.90699999, 4], [1.005, 2], [40.0001, 4], [0.5, 0], [1.5, 0], [NaN, 2], [Infinity, 1], [1e-10, 4]];
  const rounds = [0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 14899.5, -14899.5, 2.4999999999999996, 0.49999999999999994, 1e16 + 1];
  const strs = ['', ' 12 ', '74100.000000', '1e5', '0x10', '0X1f', '0b101', '0o17', 'Infinity', '-Infinity', '+Infinity',
    'infinity', 'nan', 'NaN', '1_000', 'abc', '.5', '5.', '-0', '\t7\n', '1e', '+-1', '00012', '\u00a08'];
  const keyObj = JSON.parse('{"b":1,"10":2,"2":3,"a":4,"01":5,"4294967295":6,"4294967294":7,"-1":8,"1.5":9}');
  const dates: [number, number, number, number, number, number][] = [[2026, 1, 31, 10, 0, 0], [2026, 12, 1, 0, 0, 0],
    [2026, -1, 1, 0, 0, 0], [2026, 8, 18, 25, 61, 61], [2024, 1, 29, 0, 0, 0], [1970, 0, 1, 0, 0, 0], [1969, 11, 31, 23, 59, 59], [50, 0, 1, 0, 0, 0], [0, 0, 1, 0, 0, 0], [99, 11, 31, 0, 0, 0], [100, 0, 1, 0, 0, 0]];
  return [
    { id: 'String', input: enc(nums), expected: nums.map((x) => String(x)) },
    { id: 'toFixed', input: enc(fixed), expected: fixed.map(([x, d]) => x.toFixed(d)) },
    { id: 'Math.round', input: rounds, expected: enc(rounds.map(Math.round)) },
    { id: 'Number', input: strs, expected: enc(strs.map((x) => Number(x))) },
    { id: 'Object.keys', input: null, expected: Object.keys(keyObj) },
    { id: 'Date.UTC', input: dates, expected: dates.map((d) => Date.UTC(...d)) },
    { id: 'toISOString', input: [0, 1789727400000, -1, 253402300799999, -62167219200000], expected: [0, 1789727400000, -1, 253402300799999, -62167219200000].map((x) => new Date(x).toISOString()) },
    { id: 'Math.max/min', input: null, expected: enc([Math.max(0, NaN), Math.min(1, NaN), Math.max(), Math.min(), Math.max(-0, 0)]) },
  ];
}

// ------------------------------------------------------------------- all

export async function buildAll(): Promise<Record<string, Case[]>> {
  return {
    pricing: pricingCases(),
    normalize: normalizeCases(),
    integrity: integrityCases(),
    bridge: bridgeCases(),
    liquidity: liquidityCases(),
    levels: levelsCases(),
    expiries: await expiryCases(),
    scan: scanCases(),
    jscompat: jsCompatCases(),
  };
}
