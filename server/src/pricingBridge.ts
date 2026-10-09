/**
 * The bridge between fetched data and the pure pricing core.
 *
 * This is the ONLY place where I/O-shaped data meets Black-76, and it is where
 * the single-timestamp rule is enforced. `attachPricing` REFUSES to run when
 * the chain and the futures quote did not come from one snapshot — it does not
 * warn, it does not degrade, it throws.
 *
 * It also refuses to publish when the integrity gate blocks. There is no code
 * path from here to the index LTP.
 */

import {
  b76Greeks,
  b76IV,
  checkGate,
  discountFactor,
  parityForward,
  yearFractionToExpiry,
  type GateResult,
  type ForwardResult,
  type ParityLeg,
} from './pricing/index.js';
import { checkSnapshotSkew } from './integrity.js';
import { GateBlockedError, SnapshotSkewError, ValidationError } from './errors.js';
import type { CanonicalChain, CanonicalQuote, ChainLeg, OptionType } from './types.js';

/**
 * Price used for parity and IV.
 *
 * LAST TRADED PRICE IS NOT A QUOTE. On a thin strike it can be seconds to
 * minutes old, and near expiry a 20-second-old print is 50-100 points off —
 * enough to blow the per-strike parity spread and block the gate for no real
 * reason. The live top-of-book mid is current by construction, so it is used
 * whenever the book is two-sided, uncrossed and reasonably tight. LTP is the
 * fallback only when there is no usable book.
 */
export const MAX_MID_SPREAD_FRAC = 0.05;
export function fairPrice(leg: ChainLeg | null | undefined): { price: number; source: 'mid' | 'ltp' } | null {
  if (!leg) return null;
  const b = leg.topBidPrice;
  const a = leg.topAskPrice;
  if (b !== null && a !== null && b > 0 && a > 0 && a >= b) {
    const mid = (a + b) / 2;
    if ((a - b) / mid <= MAX_MID_SPREAD_FRAC) return { price: mid, source: 'mid' };
  }
  const l = leg.lastPrice;
  return typeof l === 'number' && l > 0 ? { price: l, source: 'ltp' } : null;
}

export interface PricedLeg {
  strike: number;
  type: OptionType;
  securityId: string | null;
  marketPrice: number;
  /** Null when the price sits outside no-arbitrage bounds. Never fabricated. */
  ivPct: number | null;
  delta: number | null;
  gamma: number | null;
  /** Per 1 IV point. */
  vega: number | null;
  /** Per calendar day. */
  theta: number | null;
  /** Per 1%, Black-76 convention. */
  rho: number | null;
  moneyness: number;
  /** Vendor's own IV, for comparison only. Never used in any calculation. */
  vendorIvPct: number | null;
  /** Signed difference, ours minus theirs, in IV points. */
  vendorIvDeltaPct: number | null;
}

export interface PricingContext {
  asOfMs: number;
  expiry: string;
  T: number;
  calendarDaysToExpiry: number;
  riskFreeRate: number;
  discountFactor: number;
  forward: number;
  forwardDetail: ForwardResult;
  listedFuture: number | null;
  indexLtpDoNotUseAsSpot: number | null;
  gate: GateResult;
  legs: PricedLeg[];
  atmStrike: number | null;
  snapshot: {
    chainFetchId: string;
    futuresFetchId: string | null;
    skewMs: number;
    sameFetch: boolean;
  };
}

export interface AttachPricingOptions {
  riskFreeRate: number;
  maxSnapshotSkewMs: number;
  /** Overrides the ATM hint. Defaults to the listed future, then the chain ATM. */
  atmHint?: number;
  bandPct?: number;
  /** Gate threshold overrides. */
  thresholds?: Parameters<typeof checkGate>[0]['thresholds'];
  /** Evaluation instant. Defaults to the chain's own receipt time. */
  nowMs?: number;
  /** Expiry (YYYY-MM-DD) of the futures contract used for the cross-check. */
  futuresExpiry?: string;
}

/**
 * Pick the strike nearest a reference level, from strikes having both legs.
 */
function nearestStrike(strikes: number[], reference: number): number | null {
  if (strikes.length === 0) return null;
  return strikes.reduce((best, k) =>
    Math.abs(k - reference) < Math.abs(best - reference) ? k : best,
  );
}

/**
 * Compute the forward, run the gate, and price every leg — all against ONE
 * snapshot.
 *
 * @throws SnapshotSkewError  when chain and futures are not one snapshot
 * @throws GateBlockedError   when the integrity gate refuses to publish
 */
export function attachPricing(
  chain: CanonicalChain,
  futures: CanonicalQuote | null,
  opts: AttachPricingOptions,
): PricingContext {
  // --- 1. Single-timestamp rule. Before anything else. ---------------------
  let skewMs = 0;
  let sameFetch = true;

  if (futures) {
    const skew = checkSnapshotSkew(
      { fetchId: chain.provenance.fetchId, epochMs: chain.provenance.epochMs },
      { fetchId: futures.provenance.fetchId, epochMs: futures.provenance.epochMs },
      opts.maxSnapshotSkewMs,
    );
    if (!skew.ok) {
      throw new SnapshotSkewError(skew.message, {
        chainFetchId: chain.provenance.fetchId,
        futuresFetchId: futures.provenance.fetchId,
        skewMs: skew.skewMs,
        maxSkewMs: opts.maxSnapshotSkewMs,
      });
    }
    skewMs = skew.skewMs;
    sameFetch = skew.sameFetch;
  }

  // --- 2. Time, measured from the snapshot's own clock. --------------------
  const asOfMs = opts.nowMs ?? chain.provenance.epochMs;
  const T = yearFractionToExpiry(asOfMs, chain.expiry);
  if (!(T > 0)) {
    throw new ValidationError(
      `Expiry ${chain.expiry} is at or before the snapshot time — T = ${T}. ` +
        `Cannot price an expired contract.`,
      { expiry: chain.expiry, asOfMs, T },
    );
  }
  const r = opts.riskFreeRate;
  const DF = discountFactor(r, T);

  // --- 3. Parity legs: complete, two-sided pairs only. ---------------------
  const legs: ParityLeg[] = [];
  for (const s of chain.strikes) {
    const c = fairPrice(s.ce);
    const p = fairPrice(s.pe);
    if (!c || !p) continue;
    legs.push({ strike: s.strike, callPrice: c.price, putPrice: p.price });
  }

  if (legs.length < 2) {
    throw new GateBlockedError([
      `Only ${legs.length} strike(s) have a usable CE/PE pair. Put-call parity ` +
        `needs at least 2 to recover a forward.`,
    ]);
  }

  // ATM hint: the listed future is the best available centre. Falling back to
  // the index LTP would recentre the band on the very number under suspicion,
  // so we fall back to the middle of the available strikes instead.
  const pairedStrikes = legs.map((l) => l.strike);
  // Centre the band on the strike where |C - P| is smallest: that strike IS
  // the ATM of THIS expiry by construction. The listed future is NOT used —
  // it is usually the monthly contract, hundreds of points away from a weekly
  // forward, and centring on it drags thin deep-ITM strikes into the median.
  let atmFromParity = pairedStrikes[Math.floor(pairedStrikes.length / 2)]!;
  let best = Infinity;
  for (const l of legs) {
    const d = Math.abs(l.callPrice - l.putPrice);
    if (d < best) { best = d; atmFromParity = l.strike; }
  }
  const atmHint = opts.atmHint ?? atmFromParity;

  // --- 4. Forward from parity. Median, never the regression slope. ---------
  const forwardDetail = parityForward(legs, atmHint, r, T, {
    bandPct: opts.bandPct ?? 0.015,
  });

  // --- 5. The gate. Nothing is published if this blocks. -------------------
  const gate = checkGate({
    parityForward: forwardDetail.forward,
    listedFuture: futures && Number.isFinite(futures.ltp) ? futures.ltp : null,
    perStrikeSpread: forwardDetail.spread,
    indexLtp: chain.underlyingLtpDoNotUseAsSpot,
    slopeWithinTolerance: forwardDetail.slopeDiagnostic.withinTolerance,
    slopeRelativeError: forwardDetail.slopeDiagnostic.relativeError,
    futureExpiryGapYears:
      opts.futuresExpiry && opts.futuresExpiry !== chain.expiry
        ? yearFractionToExpiry(asOfMs, opts.futuresExpiry) - T
        : 0,
    thresholds: opts.thresholds,
  });

  if (gate.blocked) {
    throw new GateBlockedError(gate.reasons, {
      parityForward: forwardDetail.forward,
      listedFuture: futures?.ltp ?? null,
      perStrikeSpread: forwardDetail.spread,
      divergenceVsFuture: gate.divergenceVsFuture,
      impliedCarryAnnual: gate.impliedCarryAnnual,
      futureCheckMode: gate.futureCheckMode,
      futuresExpiry: opts.futuresExpiry ?? null,
      indexDivergence: gate.indexDivergence,
      thresholds: gate.thresholds,
    });
  }

  // --- 6. Price every leg against THIS snapshot's forward. -----------------
  const F = forwardDetail.forward;
  const priced: PricedLeg[] = [];

  for (const s of chain.strikes) {
    for (const type of ['CE', 'PE'] as const) {
      const leg = type === 'CE' ? s.ce : s.pe;
      if (!leg || typeof leg.lastPrice !== 'number' || !(leg.lastPrice > 0)) continue;

      // IV from the same price basis as the forward (live mid), so a stale
      // print cannot produce a bogus IV. marketPrice below stays the LTP.
      const fp = fairPrice(leg);
      const sigma = b76IV(fp ? fp.price : leg.lastPrice, F, s.strike, T, r, type);
      const g = sigma === null || sigma <= 0 ? null : b76Greeks(F, s.strike, T, sigma, r, type);

      const vendorIv = leg.vendorQuarantined.impliedVolatility;
      const ourIvPct = sigma === null ? null : sigma * 100;

      priced.push({
        strike: s.strike,
        type,
        securityId: leg.securityId,
        marketPrice: leg.lastPrice,
        ivPct: ourIvPct,
        delta: g?.delta ?? null,
        gamma: g?.gamma ?? null,
        vega: g?.vega ?? null,
        theta: g?.theta ?? null,
        rho: g?.rho ?? null,
        moneyness: s.strike / F,
        vendorIvPct: vendorIv,
        vendorIvDeltaPct:
          ourIvPct !== null && vendorIv !== null ? ourIvPct - vendorIv : null,
      });
    }
  }

  return {
    asOfMs,
    expiry: chain.expiry,
    T,
    calendarDaysToExpiry: T * 365,
    riskFreeRate: r,
    discountFactor: DF,
    forward: F,
    forwardDetail,
    listedFuture: futures && Number.isFinite(futures.ltp) ? futures.ltp : null,
    indexLtpDoNotUseAsSpot: chain.underlyingLtpDoNotUseAsSpot,
    gate,
    legs: priced,
    atmStrike: nearestStrike(pairedStrikes, F),
    snapshot: {
      chainFetchId: chain.provenance.fetchId,
      futuresFetchId: futures?.provenance.fetchId ?? null,
      skewMs,
      sameFetch,
    },
  };
}
