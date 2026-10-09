/**
 * Stage A reference: the EXISTING TypeScript plugin client end to end.
 *
 * Serves fixed Dhan-shaped HTTP bodies (MOCK: built from the repo's GM 18-Sep
 * and 21-Sep fixtures; futures quotes and ids synthetic except the registry's
 * SEP contract id) to Bull50DhanClient through a stubbed fetch, with Date.now
 * shifted to the scenario's market time. Records:
 *   - the exact HTTP bodies (parity/stage_a/<scenario>/*.json)
 *   - the receipt times the TS client stamped
 *   - the refresh-table inputs the plugin pipeline yields
 *     (get_market_snapshot + get_candles + compute_levels defaults).
 * The Python scanner replays the SAME bodies at the SAME receipt times.
 */
import { Bull50DhanClient } from '../../src/client.js';
import { deriveLevels } from '../../src/levels.js';
import { loadSettings } from '../../src/config.js';
import { GM } from '../fixtures.js';
import { GM_AS_OF, S21, enc, gmRawChain, rawCandles, rawFut, s21RawChain } from './builder.js';
import { istToEpochMs } from '../../src/pricing/index.js';

const SEP_FUT_ID = 844615; // registry.ts SEP contract (expiry 2026-09-24)

type Scenario = { asOfMs: number; strikes: number[]; bodies: Record<string, unknown> };

const wrap = (data: unknown) => ({ status: 'success', data });

export function scenarios(): Record<string, Scenario> {
  const expiries = wrap(['2026-09-24', '2026-10-01', '2026-10-29']);
  const day = (d: string) => (x: string) => x.startsWith(d);
  return {
    gm18Sep: {
      asOfMs: GM_AS_OF, strikes: GM.chain.map((l) => l.strike),
      bodies: {
        '/optionchain/expirylist': expiries, '/optionchain': wrap(gmRawChain()),
        '/marketfeed/quote': wrap({ BSE_FNO: { [SEP_FUT_ID]: rawFut(GM.futuresLtp) } }),
        '/charts/intraday': wrap(rawCandles(day('2026-09-18'))),
      },
    },
    s21Sep: {
      asOfMs: S21.asOfMs, strikes: S21.legs.map((l) => l[0]),
      bodies: {
        '/optionchain/expirylist': expiries, '/optionchain': wrap(s21RawChain()),
        '/marketfeed/quote': wrap({ BSE_FNO: { [SEP_FUT_ID]: rawFut(74680, { last_trade_time: '21/09/2026 10:33:50' }) } }),
        '/charts/intraday': wrap(rawCandles((x) => x.startsWith('2026-09-21') && istToEpochMs(x) <= S21.asOfMs)),
      },
    },
    gmFuturesDiverge: {
      asOfMs: GM_AS_OF, strikes: GM.chain.map((l) => l.strike),
      bodies: {
        '/optionchain/expirylist': expiries, '/optionchain': wrap(gmRawChain()),
        '/marketfeed/quote': wrap({ BSE_FNO: { [SEP_FUT_ID]: rawFut(75200) } }),
        '/charts/intraday': wrap(rawCandles(day('2026-09-18'))),
      },
    },
  };
}

export async function runScenario(s: Scenario) {
  const realNow = Date.now.bind(Date);
  const start = realNow();
  const realFetch = globalThis.fetch;
  const calls: string[] = [];
  Date.now = () => s.asOfMs + (realNow() - start);
  globalThis.fetch = (async (url: string | URL) => {
    const path = String(url).replace('https://api.dhan.co/v2', '');
    calls.push(path);
    const body = s.bodies[path];
    return new Response(JSON.stringify(body ?? { status: 'failure', errorMessage: 'no route' }), { status: body ? 200 : 404 });
  }) as typeof fetch;
  try {
    const client = new Bull50DhanClient({ ...loadSettings(), logLevel: 'silent' });
    client.setToken('stage-a-dummy-token-0000000000', 'STAGEA');
    const snap = await client.getMarketSnapshot('SENSEX', {}); // default expiry rule: next weekly after today
    const receipts = { chainMs: snap.chain.provenance.epochMs, futuresMs: snap.futures?.provenance.epochMs ?? null };
    const c = await client.getCandles({ underlying: 'SENSEX', series: 'index', timeframe: 'intraday', interval: 5, maxCandles: 100000 });
    if (!snap.pricing) {
      return { calls, receipts, expiry: snap.expiry, expected: { status: 'BLOCKED', reasons: snap.blocked?.reasons ?? [] } };
    }
    const p = snap.pricing;
    const spot = snap.chain.underlyingLtpDoNotUseAsSpot ?? NaN;
    const lv = deriveLevels(c.candles, spot, { confirmationBuffer: 5, minTouches: 2, roundTo: 5 }); // compute_levels defaults
    const legs: unknown[] = [];
    const excluded: unknown[] = [];
    for (const k of s.strikes) {
      for (const type of ['CE', 'PE'] as const) {
        const priced = p.legs.find((l) => l.strike === k && l.type === type);
        const raw = snap.chain.strikes.find((x) => x.strike === k)?.[type === 'CE' ? 'ce' : 'pe'] ?? null;
        if (!priced || priced.ivPct === null || !raw) {
          excluded.push({ strike: k, type, reason: !priced ? 'no priced leg' : priced.ivPct === null ? 'no IV (outside no-arbitrage bounds)' : 'no chain leg' });
          continue;
        }
        legs.push({ strike: k, isCall: type === 'CE', ivPct: priced.ivPct, bid: raw.topBidPrice ?? 0, ask: raw.topAskPrice ?? 0, ltp: raw.lastPrice });
      }
    }
    return {
      calls, receipts, expiry: snap.expiry,
      expected: enc({
        status: 'OK', forward: p.forward, T: p.T, df: p.discountFactor, atmStrike: p.atmStrike, candleRefPrice: spot,
        resistances: lv.allResistance.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
        supports: lv.allSupport.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
        atr: lv.atr14, barMinutes: lv.timeframeMinutes, legs, excludedLegs: excluded, snapshotMs: p.asOfMs,
        candlesMs: c.candles.length ? c.candles[c.candles.length - 1]!.timestampMs : null, gateWarnings: p.gate.warnings,
      }),
    };
  } finally {
    Date.now = realNow;
    globalThis.fetch = realFetch;
  }
}
