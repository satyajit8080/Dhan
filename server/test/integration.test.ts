/**
 * Full-stack integration, entirely offline.
 *
 * Stubs global fetch with realistically-shaped Dhan v2 responses and drives the
 * real client: transport envelope parsing, rate limiter, normalization, vendor
 * quarantine, integrity checks, the single-timestamp rule, the parity forward,
 * the gate, two-stage liquidity, and the response digest.
 *
 * The expiry is generated 6 days ahead of the actual clock rather than pinned to
 * a fixed date, because freezing Date would deadlock the token-bucket limiter
 * (it computes refill from elapsed wall time). The parity forward is almost
 * completely insensitive to T — a day either way moves it by ~0.01 points — so
 * the golden-master forward still holds to well inside 0.5.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { Bull50DhanClient } from '../src/client.js';
import { digestSnapshot } from '../src/digest.js';
import { GM } from './fixtures.js';

// --- fixture construction --------------------------------------------------

/** YYYY-MM-DD, n days ahead in IST. */
function istDatePlus(days: number): string {
  return new Date(Date.now() + 5.5 * 3600_000 + days * 86_400_000)
    .toISOString()
    .slice(0, 10);
}

const EXPIRY = istDatePlus(6);
// The client picks whichever registered SENSEX future is live for EXPIRY, which
// moves with the real clock, so the stub answers for every registered contract.
const FUT_IDS = new Set(['844615', '864571', '1100929']);

const ceId = (k: number) => String(k * 10 + 1);
const peId = (k: number) => String(k * 10 + 2);

/** A book that is tight on top and thin behind it. */
function book(mid: number) {
  const tick = Math.max(0.05, mid * 0.001);
  return {
    buy: [0, 1, 2, 3, 4].map((i) => ({
      price: Number((mid - tick * (i + 1)).toFixed(2)),
      quantity: 20 * (i + 1),
      orders: i + 1,
    })),
    sell: [0, 1, 2, 3, 4].map((i) => ({
      price: Number((mid + tick * (i + 1)).toFixed(2)),
      quantity: 20 * (i + 1),
      orders: i + 1,
    })),
  };
}

function chainPayload() {
  const oc: Record<string, unknown> = {};
  for (const l of GM.chain) {
    const cTick = l.callPrice * 0.001;
    const pTick = l.putPrice * 0.001;
    oc[`${l.strike}.000000`] = {
      ce: {
        average_price: l.callPrice,
        // Vendor analytics, deliberately wrong — computed off the index LTP.
        greeks: { delta: 0.64, theta: -55.1, gamma: 0.0004, vega: 30.2 },
        implied_volatility: 16.0,
        last_price: l.callPrice,
        oi: 500_000,
        previous_close_price: l.callPrice * 0.98,
        previous_oi: 480_000,
        previous_volume: 1_000_000,
        security_id: Number(ceId(l.strike)),
        top_ask_price: Number((l.callPrice + cTick).toFixed(2)),
        top_ask_quantity: 40,
        top_bid_price: Number((l.callPrice - cTick).toFixed(2)),
        top_bid_quantity: 40,
        volume: 2_000_000,
      },
      pe: {
        average_price: l.putPrice,
        greeks: { delta: -0.36, theta: -40.2, gamma: 0.0004, vega: 30.1 },
        implied_volatility: 8.6,
        last_price: l.putPrice,
        oi: 450_000,
        previous_close_price: l.putPrice * 0.98,
        previous_oi: 440_000,
        previous_volume: 900_000,
        security_id: Number(peId(l.strike)),
        top_ask_price: Number((l.putPrice + pTick).toFixed(2)),
        top_ask_quantity: 40,
        top_bid_price: Number((l.putPrice - pTick).toFixed(2)),
        top_bid_quantity: 40,
        volume: 1_800_000,
      },
    };
  }
  return { last_price: GM.indexLtp, oc };
}

const midFor = new Map<string, number>();
for (const l of GM.chain) {
  midFor.set(ceId(l.strike), l.callPrice);
  midFor.set(peId(l.strike), l.putPrice);
}

function nowIstStamp(): string {
  const d = new Date(Date.now() + 5.5 * 3600_000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)}/${d.getUTCFullYear()} ${p(
    d.getUTCHours(),
  )}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

interface CallLog {
  path: string;
  body: Record<string, unknown>;
  atMs: number;
}

const calls: CallLog[] = [];

function makeFetchStub(overrides: { futuresLtp?: number; omitFutures?: boolean } = {}) {
  return vi.fn(async (url: string | URL, init?: RequestInit) => {
    const u = String(url);
    const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    const path = u.replace('https://api.dhan.co/v2', '');
    calls.push({ path, body, atMs: Date.now() });

    const json = (payload: unknown) =>
      new Response(JSON.stringify({ data: payload, status: 'success' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });

    if (path === '/optionchain/expirylist') {
      return json([EXPIRY, istDatePlus(13), istDatePlus(20)]);
    }

    if (path === '/optionchain') {
      return json(chainPayload());
    }

    if (path === '/marketfeed/quote') {
      const out: Record<string, Record<string, unknown>> = {};
      for (const [segment, ids] of Object.entries(body)) {
        out[segment] = {};
        for (const id of ids as number[]) {
          const sid = String(id);
          if (FUT_IDS.has(sid)) {
            if (overrides.omitFutures) continue;
            out[segment][sid] = {
              last_price: overrides.futuresLtp ?? GM.futuresLtp,
              oi: 120_000,
              volume: 50_000,
              last_trade_time: nowIstStamp(),
              ohlc: { open: 74500, high: 74800, low: 74400, close: 74600 },
              depth: book(overrides.futuresLtp ?? GM.futuresLtp),
              buy_quantity: 500,
              sell_quantity: 400,
              average_price: 74600,
              net_change: 60,
              upper_circuit: 80000,
              lower_circuit: 70000,
              oi_day_high: 130_000,
              oi_day_low: 110_000,
            };
          } else {
            const mid = midFor.get(sid) ?? 100;
            out[segment][sid] = {
              last_price: mid,
              oi: 500_000,
              volume: 2_000_000,
              last_trade_time: nowIstStamp(),
              ohlc: { open: mid, high: mid * 1.05, low: mid * 0.95, close: mid },
              depth: book(mid),
              buy_quantity: 900,
              sell_quantity: 900,
              average_price: mid,
              net_change: 1,
              upper_circuit: mid * 3,
              lower_circuit: mid / 3,
              oi_day_high: 520_000,
              oi_day_low: 480_000,
            };
          }
        }
      }
      return json(out);
    }

    return new Response(JSON.stringify({ status: 'failed', errorMessage: 'no route' }), {
      status: 404,
    });
  });
}

const realFetch = globalThis.fetch;

beforeAll(() => {
  process.env['DHAN_CLIENT_ID'] = 'TEST_CLIENT';
  delete process.env['DHAN_ACCESS_TOKEN'];
  delete process.env['DHAN_TOKEN_FILE'];
});

afterAll(() => {
  globalThis.fetch = realFetch;
});

function freshClient() {
  calls.length = 0;
  const c = new Bull50DhanClient();
  // Non-JWT token: valid shape, no exp claim, so no expiry gate in tests.
  c.setToken('offline-integration-token-0001', 'TEST_CLIENT');
  return c;
}

// --- tests -----------------------------------------------------------------

describe('full stack, offline', () => {
  it(
    'builds a gated snapshot and reproduces the golden-master forward',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();

      const snap = await client.getMarketSnapshot('SENSEX', {
        includeDepth: true,
        lots: 5,
      });

      expect(snap.blocked).toBeNull();
      expect(snap.pricing).not.toBeNull();
      const p = snap.pricing!;

      // The forward survives the round trip through HTTP shapes untouched.
      expect(p.forward).toBeCloseTo(GM.expected.parityForward, 0);
      expect(p.forwardDetail.spread).toBeCloseTo(GM.expected.perStrikeSpread, 0);
      expect(p.gate.blocked).toBe(false);
      expect(p.listedFuture).toBe(GM.futuresLtp);

      // The index LTP arrived, is reported, and is not the forward.
      expect(p.indexLtpDoNotUseAsSpot).toBe(GM.indexLtp);
      expect(p.gate.indexDivergence).toBeCloseTo(GM.expected.indexDivergence, 0);
      expect(p.gate.warnings.join(' ')).toMatch(/do NOT use the index LTP as spot/i);
    },
    30_000,
  );

  it(
    'fetches the CHAIN FIRST, then futures — ordering is not incidental',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();
      await client.getMarketSnapshot('SENSEX', { includeDepth: false });

      const paths = calls.map((c) => c.path);
      expect(paths).toContain('/optionchain');
      expect(paths.indexOf('/optionchain')).toBeLessThan(
        paths.indexOf('/marketfeed/quote'),
      );
    },
    30_000,
  );

  it(
    'respects the option-chain rate limit of 1 unique request per 3 seconds',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();
      await client.getMarketSnapshot('SENSEX', { includeDepth: false });

      const chainCalls = calls.filter((c) => c.path.startsWith('/optionchain'));
      expect(chainCalls.length).toBeGreaterThanOrEqual(2);
      const gap = chainCalls[1]!.atMs - chainCalls[0]!.atMs;
      expect(gap).toBeGreaterThanOrEqual(2_900);
    },
    30_000,
  );

  it(
    'computes our own IV, and it disagrees with the vendor by design',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', { includeDepth: false });
      const p = snap.pricing!;

      const ce = p.legs.find((l) => l.strike === 74500 && l.type === 'CE')!;
      const pe = p.legs.find((l) => l.strike === 74500 && l.type === 'PE')!;

      // Ours agree across the strike, as parity requires.
      expect(Math.abs(ce.ivPct! - pe.ivPct!)).toBeLessThan(1e-3);

      // IV itself is NOT comparable to the golden master here: this fixture
      // expires 6 days from the live clock, not the golden's exact 6.00, so T
      // differs and a fixed premium implies a different sigma. What IS
      // invariant is total variance — near the money, premium ~ DF*F*0.4*sigma
      // *sqrt(T), so sigma*sqrt(T) is pinned by the price regardless of T.
      const goldenTotalVar = (GM.expected.ce74500.ivPct / 100) * Math.sqrt(6 / 365);
      const ourTotalVar = (ce.ivPct! / 100) * Math.sqrt(p.T);
      expect(ourTotalVar).toBeCloseTo(goldenTotalVar, 4);

      // And sanity: still a plausible index-option vol, not a runaway root.
      expect(ce.ivPct!).toBeGreaterThan(8);
      expect(ce.ivPct!).toBeLessThan(13);

      // The vendor's do not: 16.0 vs 8.6 on the same strike and expiry.
      expect(ce.vendorIvPct).toBe(16.0);
      expect(pe.vendorIvPct).toBe(8.6);
      expect(Math.abs(ce.vendorIvPct! - pe.vendorIvPct!)).toBeGreaterThan(7);

      // And the gap between ours and theirs is surfaced, not hidden.
      expect(ce.vendorIvDeltaPct).toBeCloseTo(ce.ivPct! - 16.0, 6);
    },
    30_000,
  );

  it(
    'runs both liquidity stages and costs exactly ONE extra quote call',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', {
        includeDepth: true,
        lots: 5,
      });

      expect(snap.liquidity).not.toBeNull();
      const liq = snap.liquidity!;
      expect(liq.screened.length).toBeGreaterThan(0);
      expect(liq.candidates.length).toBeGreaterThan(0);
      expect(liq.depth.length).toBeGreaterThan(0);

      // Stage 2 batches every candidate into a single request.
      const quoteCalls = calls.filter((c) => c.path === '/marketfeed/quote');
      expect(quoteCalls.length).toBe(2); // futures + one batched depth call

      for (const d of liq.depth) {
        expect(d.quantity).toBe(5 * 20); // lots * SENSEX lot size
        expect(['A', 'B', 'C', 'F']).toContain(d.grade);
        expect(d.roundtripPct).not.toBeNull();
      }
    },
    30_000,
  );

  it(
    'BLOCKS and publishes nothing when the futures cross-check is missing',
    async () => {
      globalThis.fetch = makeFetchStub({ omitFutures: true }) as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', { includeDepth: true });

      expect(snap.pricing).toBeNull();
      expect(snap.liquidity).toBeNull();
      expect(snap.blocked).not.toBeNull();
      expect(snap.blocked!.reasons.join(' ')).toMatch(/No futures quote/i);

      // The index LTP is present in the chain and still is not used.
      expect(snap.chain.underlyingLtpDoNotUseAsSpot).toBe(GM.indexLtp);
    },
    30_000,
  );

  it(
    'BLOCKS when the listed future disagrees with parity by more than 75 points',
    async () => {
      globalThis.fetch = makeFetchStub({ futuresLtp: 75200 }) as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', { includeDepth: false });

      expect(snap.pricing).toBeNull();
      expect(snap.blocked!.reasons.join(' ')).toMatch(/diverges from listed future|annualised carry/i);
    },
    30_000,
  );

  it(
    'digests a published snapshot without losing any integrity signal',
    async () => {
      globalThis.fetch = makeFetchStub() as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', {
        includeDepth: true,
        lots: 5,
      });

      const d = digestSnapshot(snap, 500, client.rateLimitStats()) as Record<string, any>;

      expect(d['published']).toBe(true);
      expect(d['fetch_id']).toBe(snap.fetchId);
      expect(d['pricing'].forward).toBeCloseTo(GM.expected.parityForward, 0);
      expect(d['pricing'].gate.blocked).toBe(false);
      expect(d['pricing'].legs_near_atm.length).toBeGreaterThan(0);
      expect(d['pricing'].greeks_conventions.vega).toMatch(/per 1 IV POINT/);
      expect(d['liquidity'].depth.length).toBeGreaterThan(0);

      // Must survive JSON serialisation — this is what crosses the wire.
      const json = JSON.stringify(d);
      expect(json).not.toContain('undefined');
      expect(JSON.parse(json)['pricing'].forward).toBeCloseTo(
        GM.expected.parityForward,
        0,
      );
    },
    30_000,
  );

  it(
    'digests a BLOCKED snapshot into an explicit refusal',
    async () => {
      globalThis.fetch = makeFetchStub({ omitFutures: true }) as unknown as typeof fetch;
      const client = freshClient();
      const snap = await client.getMarketSnapshot('SENSEX', { includeDepth: false });

      const d = digestSnapshot(snap, 500, client.rateLimitStats()) as Record<string, any>;
      expect(d['published']).toBe(false);
      expect(d['note']).toMatch(/NOT used as a fallback/i);
      expect(d['pricing']).toBeUndefined();
    },
    30_000,
  );

  it(
    'never sends a token in a URL and always sends the auth headers',
    async () => {
      const stub = makeFetchStub();
      globalThis.fetch = stub as unknown as typeof fetch;
      const client = freshClient();
      await client.getMarketSnapshot('SENSEX', { includeDepth: false });

      for (const call of stub.mock.calls) {
        const url = String(call[0]);
        expect(url).not.toMatch(/token/i);
        const headers = (call[1]?.headers ?? {}) as Record<string, string>;
        expect(headers['access-token']).toBe('offline-integration-token-0001');
        expect(headers['client-id']).toBe('TEST_CLIENT');
      }
    },
    30_000,
  );
});
