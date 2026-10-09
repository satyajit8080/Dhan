#!/usr/bin/env tsx
/**
 * LIVE SMOKE TEST — this one DOES hit the real Dhan API.
 *
 * Deliberately separate from `npm test`, which is entirely offline. Nothing in
 * test/ ever opens a socket; nothing here is part of the golden master.
 *
 *   DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... npm run smoke
 *
 * Costs: 1 expiry-list call, 1 option-chain call, 1 futures quote, and one more
 * quote call if --depth is passed. Well inside every published limit.
 */

import { Bull50DhanClient } from '../src/client.js';
import { Bull50DhanError } from '../src/errors.js';

const args = process.argv.slice(2);
const underlying = (args.find((a) => !a.startsWith('--')) ?? 'SENSEX').toUpperCase();
const withDepth = args.includes('--depth');
const lots = Number(args.find((a) => a.startsWith('--lots='))?.split('=')[1] ?? 1);

const line = (s = '') => process.stdout.write(s + '\n');
const rule = (t: string) => {
  line();
  line('='.repeat(72));
  line(`  ${t}`);
  line('='.repeat(72));
};
const n = (v: number | null | undefined, dp = 4) =>
  v === null || v === undefined || !Number.isFinite(v) ? 'n/a' : v.toFixed(dp);

async function main(): Promise<number> {
  const client = new Bull50DhanClient();

  rule('LIVE SMOKE TEST — bull50-dhan');
  line(`underlying   : ${underlying}`);
  line(`stage 2 depth: ${withDepth ? `yes, ${lots} lot(s)` : 'no (pass --depth to enable)'}`);

  const tok = client.tokenStatus();
  line(`token        : ${tok.present ? `${tok.source} ${tok.fingerprint}` : 'ABSENT'}`);
  line(`client id    : ${tok.clientId ?? 'ABSENT'}`);
  line(`expires      : ${tok.expiresAtIso ?? 'unknown (not a JWT)'}`);

  if (!tok.present || !tok.clientId) {
    line();
    line('Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN, then re-run.');
    return 2;
  }
  if (tok.expired) {
    line();
    line('Token has EXPIRED. Supply a fresh one.');
    return 2;
  }

  rule('1. EXPIRIES (live, cached with a TTL — never hardcoded)');
  const exp = await client.getExpiries(underlying);
  line(`cached : ${exp.cached}`);
  line(`count  : ${exp.expiries.length}`);
  line(`nearest: ${exp.expiries.slice(0, 5).join(', ')}`);

  rule('2. ATOMIC SNAPSHOT (chain FIRST, then futures, skew-gated)');
  const snap = await client.getMarketSnapshot(underlying, {
    includeDepth: withDepth,
    lots,
  });

  line(`fetch_id : ${snap.fetchId}`);
  line(`epoch_ms : ${snap.epochMs}  (${new Date(snap.epochMs).toISOString()})`);
  line(`expiry   : ${snap.expiry}`);
  line(`strikes  : ${snap.chain.strikes.length}`);
  line(`futures  : ${snap.futures ? `${snap.futures.securityId} @ ${n(snap.futures.ltp, 2)}` : 'UNAVAILABLE'}`);

  rule('3. INTEGRITY REPORT');
  line(`overall ok: ${snap.integrity.ok}`);
  for (const f of snap.integrity.findings) {
    line(`  [${f.severity.toUpperCase().padEnd(5)}] ${f.code}: ${f.message}`);
  }
  if (snap.warnings.length) {
    line();
    line('warnings:');
    for (const w of snap.warnings) line(`  - ${w}`);
  }

  rule('4. P0 PRICING');
  if (!snap.pricing) {
    line('GATE BLOCKED — nothing published.');
    for (const r of snap.blocked?.reasons ?? []) line(`  - ${r}`);
    line();
    line('No fallback to the index LTP was attempted, by design.');
    return 1;
  }

  const p = snap.pricing;
  line(`T                      : ${p.T}`);
  line(`calendar days          : ${n(p.calendarDaysToExpiry, 6)}`);
  line(`discount factor        : ${n(p.discountFactor, 10)}`);
  line();
  line(`parity forward         : ${n(p.forward)}`);
  line(`per-strike spread      : ${n(p.forwardDetail.spread)}   (limit ${p.gate.thresholds.maxPerStrikeSpread})`);
  line(`strikes used           : ${p.forwardDetail.usedStrikes.join(', ')}`);
  line(`listed future          : ${n(p.listedFuture)}`);
  line(`divergence vs future   : ${n(p.gate.divergenceVsFuture)}   (limit ${p.gate.thresholds.maxFutureDivergence})`);
  line();
  line('--- BEFORE vs AFTER: the defect this exists to fix ---');
  line(`index LTP (VENDOR USES THIS AS SPOT): ${n(p.indexLtpDoNotUseAsSpot, 2)}`);
  line(`parity forward (WE USE THIS)        : ${n(p.forward, 2)}`);
  line(`error embedded in vendor Greeks     : ${n(p.gate.indexDivergence, 2)} points`);
  if (p.indexLtpDoNotUseAsSpot && p.gate.indexDivergence !== null && p.T > 0) {
    const implied = (p.gate.indexDivergence / p.indexLtpDoNotUseAsSpot / p.T) * 100;
    line(`implied annualised carry            : ${implied.toFixed(1)}%`);
  }

  rule('5. NEAR-ATM LEGS — ours vs vendor');
  const atm = p.atmStrike ?? p.forward;
  const near = p.legs
    .filter((l) => Math.abs(l.strike - atm) <= 300)
    .sort((a, b) => a.strike - b.strike || a.type.localeCompare(b.type));

  line(
    'strike  type   ltp      our IV   vendor IV  diff     delta     theta/day   vega',
  );
  for (const l of near) {
    line(
      `${String(l.strike).padEnd(7)} ${l.type}   ` +
        `${n(l.marketPrice, 2).padStart(8)} ` +
        `${n(l.ivPct, 3).padStart(8)} ` +
        `${n(l.vendorIvPct, 3).padStart(10)} ` +
        `${n(l.vendorIvDeltaPct, 3).padStart(8)} ` +
        `${n(l.delta, 5).padStart(9)} ` +
        `${n(l.theta, 3).padStart(11)} ` +
        `${n(l.vega, 3).padStart(8)}`,
    );
  }

  line();
  line('--- put-call IV agreement (must hold under parity) ---');
  const byStrike = new Map<number, { ce?: number | null; pe?: number | null }>();
  for (const l of p.legs) {
    const e = byStrike.get(l.strike) ?? {};
    if (l.type === 'CE') e.ce = l.ivPct;
    else e.pe = l.ivPct;
    byStrike.set(l.strike, e);
  }
  let worst = 0;
  let worstK = 0;
  for (const [k, v] of byStrike) {
    if (v.ce == null || v.pe == null) continue;
    if (Math.abs(k - atm) > 500) continue;
    const gap = Math.abs(v.ce - v.pe);
    if (gap > worst) {
      worst = gap;
      worstK = k;
    }
  }
  line(`worst CE/PE IV gap within 500 pts of ATM: ${worst.toFixed(4)} IV points at ${worstK}`);
  line(worst <= 0.5 ? 'PASS — consistent with put-call parity.' : 'INVESTIGATE — gap exceeds 0.5 IV points.');

  if (snap.liquidity) {
    rule('6. LIQUIDITY');
    line(snap.liquidity.note);
    line(`screened: ${snap.liquidity.screened.length}, candidates: ${snap.liquidity.candidates.length}`);
    if (snap.liquidity.depth.length) {
      line();
      line('security_id   grade  quoted%   roundtrip%   xSpread  effBuy     effSell');
      for (const d of snap.liquidity.depth) {
        line(
          `${d.securityId.padEnd(13)} ${d.grade}      ` +
            `${n(d.quotedSpreadPct, 3).padStart(7)} ` +
            `${n(d.roundtripPct, 3).padStart(12)} ` +
            `${n(d.slippageMultiple, 2).padStart(8)} ` +
            `${n(d.effectiveBuy, 2).padStart(10)} ` +
            `${n(d.effectiveSell, 2).padStart(10)}`,
        );
      }
    }
  }

  rule('RATE LIMIT BUDGET');
  const rl = client.rateLimitStats();
  line(`day ${rl.day}: used ${rl.used} of ${rl.quota}, ${rl.remaining} remaining`);

  rule('SMOKE TEST COMPLETE');
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    line();
    if (err instanceof Bull50DhanError) {
      line(`FAILED [${err.code}]: ${err.message}`);
      line(JSON.stringify(err.details, null, 2));
    } else {
      line(`FAILED: ${(err as Error).message}`);
    }
    process.exit(1);
  });
