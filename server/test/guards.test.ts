import { describe, it, expect } from 'vitest';
import { assertReadOnlyPath } from '../src/transport.js';
import { normalizeChain, normalizeQuote, parseLastTradeTime } from '../src/normalize.js';
import { attachPricing } from '../src/pricingBridge.js';
import { checkSnapshotSkew } from '../src/integrity.js';
import { GateBlockedError, SnapshotSkewError, ValidationError } from '../src/errors.js';
import { redact, registerSecret } from '../src/config.js';
import { istToEpochMs } from '../src/pricing/index.js';
import { GM } from './fixtures.js';
import type { CanonicalQuote } from '../src/types.js';

describe('read-only guard', () => {
  it.each([
    '/orders',
    '/orders/123',
    '/super/orders',
    '/forever/orders',
    '/funds/get-fund-limits',
    '/positions',
    '/holdings',
    '/margincalculator',
    '/alerts',
  ])('refuses %s before the request can leave the process', (path) => {
    expect(() => assertReadOnlyPath(path)).toThrow(ValidationError);
  });

  it.each(['/marketfeed/quote', '/optionchain', '/optionchain/expirylist', '/charts/historical'])(
    'allows the read-only data path %s',
    (path) => {
      expect(() => assertReadOnlyPath(path)).not.toThrow();
    },
  );
});

describe('log redaction', () => {
  it('never lets a registered token reach a log line', () => {
    const token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijklmnop';
    registerSecret(token);
    const out = redact(`calling with access-token: ${token} now`);
    expect(out).not.toContain(token);
    expect(out).toContain('[REDACTED]');
  });

  it('redacts anything JWT-shaped even if never registered', () => {
    const stray = 'eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.zzzzzzzzzzzz';
    expect(redact(`token=${stray}`)).not.toContain(stray);
  });

  it('redacts through object serialisation too', () => {
    const token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvYmoifQ.qqqqqqqqqqqqqqqq';
    registerSecret(token);
    expect(redact({ headers: { 'access-token': token } })).not.toContain(token);
  });
});

describe('normalize — vendor quarantine', () => {
  const rawChain = {
    last_price: 74294.96,
    oc: {
      '74500.000000': {
        ce: {
          last_price: 461.55,
          oi: 100,
          security_id: 123456,
          top_bid_price: 461.0,
          top_bid_quantity: 20,
          top_ask_price: 462.0,
          top_ask_quantity: 20,
          implied_volatility: 16.0,
          greeks: { delta: 0.64, theta: -55.1, gamma: 0.0004, vega: 30.2 },
        },
        pe: {
          last_price: 345.1,
          oi: 90,
          security_id: 123457,
          implied_volatility: 8.6,
          greeks: { delta: -0.36, theta: -40.2, gamma: 0.0004, vega: 30.1 },
        },
      },
    },
  };

  const chain = normalizeChain(
    rawChain,
    { underlying: 'SENSEX', underlyingScrip: 51, underlyingSeg: 'IDX_I', expiry: '2026-09-24' },
    'fetch-1',
    1_758_000_000_000,
    '/optionchain',
  );

  it('moves vendor IV and Greeks into quarantine, off the usable surface', () => {
    const ce = chain.strikes[0]!.ce!;
    expect(ce.vendorQuarantined.impliedVolatility).toBe(16.0);
    expect(ce.vendorQuarantined.greeks?.delta).toBe(0.64);
    // They are NOT reachable as plain leg fields.
    expect((ce as Record<string, unknown>)['impliedVolatility']).toBeUndefined();
    expect((ce as Record<string, unknown>)['greeks']).toBeUndefined();
  });

  it('records WHY each vendor value is distrusted', () => {
    expect(chain.strikes[0]!.ce!.vendorQuarantined.reason).toMatch(/index LTP, not the forward/i);
  });

  it('names the index LTP so it cannot be mistaken for spot', () => {
    expect(chain.underlyingLtpDoNotUseAsSpot).toBe(74294.96);
  });

  it('keeps each leg security_id, which is what makes Stage-2 depth free', () => {
    expect(chain.strikes[0]!.ce!.securityId).toBe('123456');
    expect(chain.strikes[0]!.pe!.securityId).toBe('123457');
  });

  it('demonstrates the vendor defect: same strike, CE 16.0% vs PE 8.6%', () => {
    const ce = chain.strikes[0]!.ce!.vendorQuarantined.impliedVolatility!;
    const pe = chain.strikes[0]!.pe!.vendorQuarantined.impliedVolatility!;
    expect(Math.abs(ce - pe)).toBeGreaterThan(7);
  });
});

describe('normalize — timestamps and sentinels', () => {
  it('treats 01/01/1980 as "never traded", not as 1980', () => {
    expect(parseLastTradeTime('01/01/1980 00:00:00')).toBeNull();
  });

  it('parses an IST trade time to epoch ms', () => {
    // 18/09/2026 15:30:00 IST == 2026-09-18T10:00:00Z
    expect(parseLastTradeTime('18/09/2026 15:30:00')).toBe(Date.UTC(2026, 8, 18, 10, 0, 0));
  });

  it('quarantines an unparseable timestamp instead of coercing it', () => {
    const q = normalizeQuote(
      { last_price: 100, last_trade_time: 'garbage' },
      '1',
      'BSE_FNO',
      'f',
      1,
      '/marketfeed/quote',
    );
    expect(q.lastTradeTimeMs).toBeNull();
    expect(q.quarantined['last_trade_time']).toBe('garbage');
  });
});

describe('single-timestamp rule', () => {
  it('accepts the same fetch id unconditionally', () => {
    const r = checkSnapshotSkew({ fetchId: 'a', epochMs: 0 }, { fetchId: 'a', epochMs: 999_999 }, 10);
    expect(r.ok).toBe(true);
    expect(r.sameFetch).toBe(true);
  });

  it('accepts distinct fetches inside the window', () => {
    expect(checkSnapshotSkew({ fetchId: 'a', epochMs: 0 }, { fetchId: 'b', epochMs: 500 }, 3000).ok).toBe(true);
  });

  it('refuses distinct fetches beyond the window', () => {
    const r = checkSnapshotSkew({ fetchId: 'a', epochMs: 0 }, { fetchId: 'b', epochMs: 9000 }, 3000);
    expect(r.ok).toBe(false);
    expect(r.message).toMatch(/not one snapshot/);
  });
});

// ---------------------------------------------------------------------------
// attachPricing refusals, built from the golden-master fixture
// ---------------------------------------------------------------------------

const asOfMs = istToEpochMs(GM.asOfIst);

function buildChain(fetchId = 'chain-1', epochMs = asOfMs) {
  const oc: Record<string, unknown> = {};
  for (const l of GM.chain) {
    oc[`${l.strike}.000000`] = {
      ce: { last_price: l.callPrice, security_id: l.strike * 10 + 1, implied_volatility: 0 },
      pe: { last_price: l.putPrice, security_id: l.strike * 10 + 2, implied_volatility: 0 },
    };
  }
  return normalizeChain(
    { last_price: GM.indexLtp, oc },
    { underlying: 'SENSEX', underlyingScrip: 51, underlyingSeg: 'IDX_I', expiry: GM.expiry },
    fetchId,
    epochMs,
    '/optionchain',
  );
}

function buildFutures(ltp: number, fetchId = 'fut-1', epochMs = asOfMs): CanonicalQuote {
  return normalizeQuote(
    { last_price: ltp, last_trade_time: '18/09/2026 15:30:00' },
    '844615',
    'BSE_FNO',
    fetchId,
    epochMs,
    '/marketfeed/quote',
  );
}

const baseOpts = { riskFreeRate: GM.r, maxSnapshotSkewMs: 3000, nowMs: asOfMs };

describe('attachPricing', () => {
  it('reproduces the golden-master forward end to end', () => {
    const ctx = attachPricing(buildChain(), buildFutures(GM.futuresLtp), baseOpts);
    expect(ctx.forward).toBeCloseTo(GM.expected.parityForward, 3);
    expect(ctx.gate.blocked).toBe(false);
    expect(ctx.gate.divergenceVsFuture).toBeCloseTo(GM.expected.divergenceVsFuture, 3);
  });

  it('prices every leg against ONE forward, so CE and PE IV agree', () => {
    const ctx = attachPricing(buildChain(), buildFutures(GM.futuresLtp), baseOpts);
    const ce = ctx.legs.find((l) => l.strike === 74500 && l.type === 'CE')!;
    const pe = ctx.legs.find((l) => l.strike === 74500 && l.type === 'PE')!;
    expect(Math.abs(ce.ivPct! - pe.ivPct!)).toBeLessThan(1e-3);
    expect(ce.ivPct!).toBeCloseTo(GM.expected.ce74500.ivPct, 3);
  });

  it('carries the vendor IV for comparison but never uses it', () => {
    const ctx = attachPricing(buildChain(), buildFutures(GM.futuresLtp), baseOpts);
    const ce = ctx.legs.find((l) => l.strike === 74500 && l.type === 'CE')!;
    expect(ce.vendorIvPct).toBe(0); // vendor said 0 in this fixture
    expect(ce.ivPct).toBeCloseTo(GM.expected.ce74500.ivPct, 3); // ours is right anyway
  });

  it('REFUSES when the chain and futures are not one snapshot', () => {
    expect(() =>
      attachPricing(buildChain('c1', asOfMs), buildFutures(GM.futuresLtp, 'f1', asOfMs + 9000), baseOpts),
    ).toThrow(SnapshotSkewError);
  });

  it('BLOCKS when no futures quote is available, and never uses the index', () => {
    try {
      attachPricing(buildChain(), null, baseOpts);
      throw new Error('should have blocked');
    } catch (err) {
      expect(err).toBeInstanceOf(GateBlockedError);
      expect((err as GateBlockedError).reasons.join(' ')).toMatch(/No futures quote/);
    }
  });

  it('BLOCKS on an implausible futures cross-check', () => {
    expect(() => attachPricing(buildChain(), buildFutures(75000), baseOpts)).toThrow(
      GateBlockedError,
    );
  });

  it('refuses to price an already-expired contract', () => {
    const past = istToEpochMs('2026-09-25T10:00:00');
    expect(() =>
      attachPricing(buildChain('c', past), buildFutures(GM.futuresLtp, 'f', past), {
        ...baseOpts,
        nowMs: past,
      }),
    ).toThrow(ValidationError);
  });

  it('never centres the band on the index LTP', () => {
    // With no futures the gate blocks, so the only way the index could leak in
    // is as an ATM hint. Supply futures far from the index and confirm the
    // forward is unmoved: the hint only widens or shifts the band.
    const a = attachPricing(buildChain(), buildFutures(GM.futuresLtp), baseOpts);
    const b = attachPricing(buildChain(), buildFutures(GM.futuresLtp), {
      ...baseOpts,
      atmHint: 74300,
    });
    expect(a.forward).toBeCloseTo(b.forward, 9);
  });
});
