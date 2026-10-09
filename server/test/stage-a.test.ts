/**
 * Stage A references (parity/stage_a/*) must be genuine TypeScript output.
 * 1. Deterministic: recompute every expected value from the committed HTTP
 *    bodies at the committed receipt times using the TS functions.
 * 2. Live TS client path: re-run Bull50DhanClient on the same bodies and check
 *    the time-independent results (status, call order, chosen expiry).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { normalizeChain, normalizeQuote } from '../src/normalize.js';
import { attachPricing } from '../src/pricingBridge.js';
import { deriveLevels } from '../src/levels.js';
import { toCandles } from '../src/endpoints/historical.js';
import { scenarios, runScenario } from './parity/stageA.js';
import { enc } from './parity/builder.js';

const read = (name: string, f: string) =>
  JSON.parse(readFileSync(new URL(`../../parity/stage_a/${name}/${f}`, import.meta.url), 'utf8'));

describe('Stage A references are TypeScript output', () => {
  for (const name of Object.keys(scenarios())) {
    it(`${name}: expected values recompute exactly from bodies + receipt times`, () => {
      const { bodies } = read(name, 'bodies.json');
      const ref = read(name, 'reference.json');
      const chain = normalizeChain(bodies['/optionchain'].data, { underlying: 'SENSEX', underlyingScrip: 51, underlyingSeg: 'IDX_I', expiry: ref.expiry }, 'c', ref.receipts.chainMs, '/optionchain');
      const sid = Object.keys(bodies['/marketfeed/quote'].data.BSE_FNO)[0]!;
      const fut = normalizeQuote(bodies['/marketfeed/quote'].data.BSE_FNO[sid], sid, 'BSE_FNO', 'f', ref.receipts.futuresMs, '/marketfeed/quote');
      let p;
      try {
        p = attachPricing(chain, fut, { riskFreeRate: 0.065, maxSnapshotSkewMs: 3000, futuresExpiry: '2026-09-24' });
      } catch (e) {
        expect(ref.expected).toEqual({ status: 'BLOCKED', reasons: (e as { reasons: string[] }).reasons });
        return;
      }
      const candles = toCandles(bodies['/charts/intraday'].data);
      const spot = chain.underlyingLtpDoNotUseAsSpot ?? NaN;
      const lv = deriveLevels(candles, spot, { confirmationBuffer: 5, minTouches: 2, roundTo: 5 });
      expect(ref.expected.forward).toBe(p.forward);
      expect(ref.expected.T).toBe(p.T);
      expect(ref.expected.resistances).toEqual(enc(lv.allResistance.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] }))));
      expect(ref.expected.supports).toEqual(enc(lv.allSupport.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] }))));
      for (const leg of ref.expected.legs as { strike: number; isCall: boolean; ivPct: number }[]) {
        const priced = p.legs.find((l) => l.strike === leg.strike && l.type === (leg.isCall ? 'CE' : 'PE'))!;
        expect(leg.ivPct).toBe(priced.ivPct);
      }
    });

    it(`${name}: the live TS client path still yields the same status, calls and expiry`, async () => {
      const ref = read(name, 'reference.json');
      const r = await runScenario(scenarios()[name]!);
      expect(r.calls).toEqual(ref.calls);
      expect(r.expiry).toBe(ref.expiry);
      expect((r.expected as { status: string }).status).toBe(ref.expected.status);
    }, 60_000);
  }
});
