/**
 * TypeScript reference for RECORDED live scans (engine/scan_local.py --record DIR).
 * For every DIR/scan-NNN it runs the existing TS functions on the recorded bodies at
 * the recorded receipt times and writes ts_reference.json (the same shape as the
 * Python scan inputs). Then: cd engine && python3 tests/replay_recorded.py DIR
 *    cd server && npx tsx scripts/replay-recorded.ts ../scan-records
 */
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { normalizeChain, normalizeQuote } from '../src/normalize.js';
import { attachPricing } from '../src/pricingBridge.js';
import { deriveLevels } from '../src/levels.js';
import { toCandles } from '../src/endpoints/historical.js';
import { enc } from '../test/parity/builder.js';

const unwrap = (b: { data?: unknown } | unknown) =>
  b && typeof b === 'object' && 'data' in (b as object) ? (b as { data: unknown }).data : b;

const root = process.argv[2] ?? '../scan-records';
for (const name of readdirSync(root).filter((n) => n.startsWith('scan-')).sort()) {
  const dir = join(root, name);
  if (!existsSync(join(dir, 'bodies.json'))) continue;
  const bodies = JSON.parse(readFileSync(join(dir, 'bodies.json'), 'utf8')).bodies;
  const receipts = JSON.parse(readFileSync(join(dir, 'receipts.json'), 'utf8'));
  const meta = JSON.parse(readFileSync(join(dir, 'meta.json'), 'utf8'));
  const today = new Date(Date.parse(meta.clockUtc) + 5.5 * 3600_000).toISOString().slice(0, 10);
  const expiries = (unwrap(bodies['/optionchain/expirylist']) as string[]).filter((e) => /^\d{4}-\d{2}-\d{2}$/.test(e)).sort();
  const expiry = expiries.find((e) => e > today)!;           // RULES.md §6: next weekly after today
  const chain = normalizeChain(unwrap(bodies['/optionchain']), { underlying: 'SENSEX', underlyingScrip: 51, underlyingSeg: 'IDX_I', expiry }, 'chain', receipts['/optionchain'], '/optionchain');
  const q = (unwrap(bodies['/marketfeed/quote']) as Record<string, Record<string, unknown>>)?.['BSE_FNO']?.[String(meta.futuresSecurityId)];
  const fut = q ? normalizeQuote(q, String(meta.futuresSecurityId), 'BSE_FNO', 'futures', receipts['/marketfeed/quote'], '/marketfeed/quote') : null;
  let expected: unknown;
  try {
    const p = attachPricing(chain, fut, { riskFreeRate: 0.065, maxSnapshotSkewMs: 3000, futuresExpiry: meta.futuresExpiry });
    const candles = bodies['/charts/intraday'] ? toCandles(unwrap(bodies['/charts/intraday']) as never) : [];
    const spot = chain.underlyingLtpDoNotUseAsSpot ?? NaN;
    const lv = deriveLevels(candles, spot, { confirmationBuffer: 5, minTouches: 2, roundTo: 5 });
    const legs: unknown[] = [];
    const excluded: unknown[] = [];
    for (const k of meta.strikes as number[]) for (const type of ['CE', 'PE'] as const) {
      const priced = p.legs.find((l) => l.strike === k && l.type === type);
      const raw = chain.strikes.find((x) => x.strike === k)?.[type === 'CE' ? 'ce' : 'pe'] ?? null;
      if (!priced || priced.ivPct === null || !raw) {
        excluded.push({ strike: k, type, reason: !priced ? 'no priced leg' : priced.ivPct === null ? 'no IV (outside no-arbitrage bounds)' : 'no chain leg' });
        continue;
      }
      legs.push({ strike: k, isCall: type === 'CE', ivPct: priced.ivPct, bid: raw.topBidPrice ?? 0, ask: raw.topAskPrice ?? 0, ltp: raw.lastPrice });
    }
    expected = enc({ status: 'OK', forward: p.forward, T: p.T, df: p.discountFactor, atmStrike: p.atmStrike, candleRefPrice: spot,
      resistances: lv.allResistance.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
      supports: lv.allSupport.map((l) => ({ price: l.price, touches: l.touches, sources: [...l.sources] })),
      atr: lv.atr14, barMinutes: lv.timeframeMinutes, legs, excludedLegs: excluded, snapshotMs: p.asOfMs,
      candlesMs: candles.length ? candles[candles.length - 1]!.timestampMs : null, gateWarnings: p.gate.warnings });
  } catch (e) {
    const err = e as Error & { reasons?: string[] };
    expected = err.name === 'GateBlockedError' ? { status: 'BLOCKED', reasons: err.reasons }
      : err.name === 'SnapshotSkewError' ? { status: 'BLOCKED', reasons: [err.message] }
      : { status: 'ERROR', reasons: [err.message] };
  }
  writeFileSync(join(dir, 'ts_reference.json'), JSON.stringify({ expiry, expected }, null, 1));
  console.log(name, (expected as { status: string }).status, expiry);
}
