/**
 * Structural and staleness checks on normalized data, plus the snapshot-skew
 * rule that keeps one calculation inside one timestamp.
 */

import type {
  CanonicalChain,
  CanonicalQuote,
  IntegrityFinding,
  IntegrityReport,
} from './types.js';

function report(findings: IntegrityFinding[], checkedAtMs: number): IntegrityReport {
  return {
    ok: !findings.some((f) => f.severity === 'block'),
    findings,
    checkedAtMs,
  };
}

export function checkQuote(
  q: CanonicalQuote,
  nowMs: number,
  maxAgeMs: number,
): IntegrityReport {
  const f: IntegrityFinding[] = [];

  if (!Number.isFinite(q.ltp)) {
    f.push({
      severity: 'block',
      code: 'QUOTE_NO_LTP',
      message: `No usable last_price for ${q.segment}:${q.securityId}.`,
    });
  }

  if (q.lastTradeTimeMs === null) {
    f.push({
      severity: 'warn',
      code: 'QUOTE_NO_TRADE_TIME',
      message:
        `${q.segment}:${q.securityId} has no last_trade_time (never traded, or the ` +
        `01/01/1980 sentinel). Staleness cannot be established from the vendor clock.`,
    });
  } else {
    const age = nowMs - q.lastTradeTimeMs;
    if (age > maxAgeMs) {
      f.push({
        severity: 'warn',
        code: 'QUOTE_STALE',
        message:
          `${q.segment}:${q.securityId} last traded ${(age / 1000).toFixed(1)}s ago, ` +
          `beyond the ${(maxAgeMs / 1000).toFixed(1)}s freshness window.`,
      });
    }
  }

  if (q.depth) {
    const bestBid = q.depth.buy.find((l) => l.price > 0 && l.quantity > 0);
    const bestAsk = q.depth.sell.find((l) => l.price > 0 && l.quantity > 0);
    if (bestBid && bestAsk && bestBid.price >= bestAsk.price) {
      f.push({
        severity: 'block',
        code: 'QUOTE_CROSSED',
        message:
          `Crossed book on ${q.segment}:${q.securityId}: bid ${bestBid.price} >= ` +
          `ask ${bestAsk.price}. Refusing to derive liquidity from it.`,
      });
    }
    if (!bestBid || !bestAsk) {
      f.push({
        severity: 'warn',
        code: 'QUOTE_ONE_SIDED',
        message: `One-sided or empty book on ${q.segment}:${q.securityId}.`,
      });
    }
  }

  if (Object.keys(q.quarantined).length > 0) {
    f.push({
      severity: 'warn',
      code: 'QUOTE_QUARANTINED_FIELDS',
      message: `Quarantined unparseable fields: ${Object.keys(q.quarantined).join(', ')}.`,
    });
  }

  return report(f, nowMs);
}

export function checkChain(chain: CanonicalChain, nowMs: number): IntegrityReport {
  const f: IntegrityFinding[] = [];

  if (chain.strikes.length === 0) {
    f.push({
      severity: 'block',
      code: 'CHAIN_EMPTY',
      message: `Option chain for ${chain.underlying} ${chain.expiry} has no strikes.`,
    });
    return report(f, nowMs);
  }

  let bothLegs = 0;
  let crossed = 0;
  let zeroPrice = 0;
  let vendorIvSuspect = 0;

  for (const s of chain.strikes) {
    if (s.ce && s.pe) bothLegs++;
    for (const leg of [s.ce, s.pe]) {
      if (!leg) continue;
      if (leg.lastPrice !== null && leg.lastPrice <= 0) zeroPrice++;
      if (
        leg.topBidPrice !== null &&
        leg.topAskPrice !== null &&
        leg.topBidPrice > 0 &&
        leg.topAskPrice > 0 &&
        leg.topBidPrice >= leg.topAskPrice
      ) {
        crossed++;
      }
      const iv = leg.vendorQuarantined.impliedVolatility;
      if (iv === null || iv <= 0 || iv > 60) vendorIvSuspect++;
    }
  }

  if (bothLegs < 2) {
    f.push({
      severity: 'block',
      code: 'CHAIN_INSUFFICIENT_PAIRS',
      message:
        `Only ${bothLegs} strike(s) have both a CE and a PE. Put-call parity needs ` +
        `at least 2 complete pairs to recover a forward.`,
    });
  }

  if (crossed > 0) {
    f.push({
      severity: 'warn',
      code: 'CHAIN_CROSSED_LEGS',
      message: `${crossed} leg(s) show a crossed top-of-book; they are excluded from parity.`,
    });
  }

  if (zeroPrice > 0) {
    f.push({
      severity: 'warn',
      code: 'CHAIN_ZERO_PRICES',
      message: `${zeroPrice} leg(s) have a non-positive last price.`,
    });
  }

  const totalLegs = chain.strikes.reduce(
    (n, s) => n + (s.ce ? 1 : 0) + (s.pe ? 1 : 0),
    0,
  );
  if (totalLegs > 0) {
    const pct = (vendorIvSuspect / totalLegs) * 100;
    f.push({
      severity: 'info',
      code: 'VENDOR_IV_QUARANTINED',
      message:
        `${vendorIvSuspect}/${totalLegs} legs (${pct.toFixed(1)}%) carry vendor IV that is ` +
        `zero, missing or >60%. All vendor IV and Greeks are quarantined regardless; ` +
        `this server computes its own.`,
    });
  }

  if (chain.underlyingLtpDoNotUseAsSpot !== null) {
    f.push({
      severity: 'info',
      code: 'CHAIN_CARRIES_INDEX_LTP',
      message:
        `Chain reports underlying last_price ${chain.underlyingLtpDoNotUseAsSpot}. ` +
        `This is the INDEX LTP and is never used as spot for option maths.`,
    });
  }

  return report(f, nowMs);
}

export interface SkewCheck {
  ok: boolean;
  skewMs: number;
  sameFetch: boolean;
  message: string;
}

/**
 * Are these two payloads one snapshot?
 *
 * Same fetchId is conclusive. Different fetchIds are allowed only when the
 * receipt times are within `maxSkewMs` — that is the whole permission the
 * single-timestamp rule grants, and it is what pricingBridge enforces.
 */
export function checkSnapshotSkew(
  a: { fetchId: string; epochMs: number },
  b: { fetchId: string; epochMs: number },
  maxSkewMs: number,
): SkewCheck {
  const skewMs = Math.abs(a.epochMs - b.epochMs);
  const sameFetch = a.fetchId === b.fetchId;
  if (sameFetch) {
    return { ok: true, skewMs, sameFetch, message: 'Single atomic fetch.' };
  }
  if (skewMs <= maxSkewMs) {
    return {
      ok: true,
      skewMs,
      sameFetch,
      message: `Distinct fetches ${skewMs}ms apart, inside the ${maxSkewMs}ms window.`,
    };
  }
  return {
    ok: false,
    skewMs,
    sameFetch,
    message:
      `Snapshot skew ${skewMs}ms exceeds the ${maxSkewMs}ms limit. The chain and the ` +
      `futures leg are not one snapshot; mixing them would price across two timestamps.`,
  };
}
