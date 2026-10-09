/**
 * Two-stage liquidity assessment.
 *
 * Stage 1 screens EVERY strike on top-of-book data that already arrived with
 * the chain — free, no extra request. Stage 2 spends one /marketfeed/quote call
 * on the survivors and walks all five levels.
 *
 * WHY THE SECOND STAGE EXISTS: a 0.2% quoted spread can cost 0.68% per side
 * once real size has to be worked. Against one observed book, the round-trip
 * cost more than doubled from 1 lot (1.005%) to 10 lots (2.312%). Top-of-book
 * screening cannot see that — it prices the first level and stops.
 *
 * PURE MODULE: no I/O. The caller fetches; this decides.
 */

import type { CanonicalQuote, ChainStrike, DepthLevel, OptionType } from './types.js';

export type LiquidityGrade = 'A' | 'B' | 'C' | 'F';

export interface BookWalk {
  /** Quantity actually fillable from the visible book. */
  filledQuantity: number;
  /** Quantity requested but not available at any visible level. */
  shortfall: number;
  /** Size-weighted average price of the fill. NaN when nothing fills. */
  vwap: number;
  notional: number;
  levelsConsumed: number;
  /** True when the visible book could not cover the requested size. */
  incomplete: boolean;
}

/**
 * Walk one side of the book for `quantity` units.
 *
 * Levels are consumed in the order given, so pass the ask ladder ascending to
 * buy and the bid ladder descending to sell.
 */
export function walkBook(levels: DepthLevel[], quantity: number): BookWalk {
  let remaining = quantity;
  let notional = 0;
  let filled = 0;
  let levelsConsumed = 0;

  for (const lvl of levels) {
    if (remaining <= 0) break;
    if (!(lvl.price > 0) || !(lvl.quantity > 0)) continue;
    const take = Math.min(remaining, lvl.quantity);
    notional += take * lvl.price;
    filled += take;
    remaining -= take;
    levelsConsumed++;
  }

  return {
    filledQuantity: filled,
    shortfall: Math.max(0, remaining),
    vwap: filled > 0 ? notional / filled : NaN,
    notional,
    levelsConsumed,
    incomplete: remaining > 0,
  };
}

export interface DepthAssessment {
  securityId: string;
  lots: number;
  lotSize: number;
  quantity: number;
  bestBid: number | null;
  bestAsk: number | null;
  midPrice: number | null;
  /** Top-of-book spread as a percentage of mid. The number that misleads. */
  quotedSpreadPct: number | null;
  /** VWAP paying up the ask ladder for the full size. */
  effectiveBuy: number | null;
  /** VWAP hitting down the bid ladder for the full size. */
  effectiveSell: number | null;
  /** (effectiveBuy - effectiveSell) / mid, as a percentage. The real cost. */
  roundtripPct: number | null;
  /** How much worse the real round-trip is than the quoted spread. */
  slippageMultiple: number | null;
  grade: LiquidityGrade;
  reasons: string[];
  buyWalk: BookWalk | null;
  sellWalk: BookWalk | null;
}

export const GRADE_THRESHOLDS = {
  /** Round-trip cost, in percent of mid. */
  A: 0.6,
  B: 1.2,
  C: 2.5,
} as const;

function gradeFor(roundtripPct: number | null, incomplete: boolean): LiquidityGrade {
  if (roundtripPct === null || !Number.isFinite(roundtripPct)) return 'F';
  if (incomplete) return 'F';
  if (roundtripPct <= GRADE_THRESHOLDS.A) return 'A';
  if (roundtripPct <= GRADE_THRESHOLDS.B) return 'B';
  if (roundtripPct <= GRADE_THRESHOLDS.C) return 'C';
  return 'F';
}

/**
 * Stage 2: assess real executable cost at a real size, from 5-level depth.
 *
 * @param lots     number of lots you actually intend to trade
 * @param lotSize  contract multiplier (SENSEX: 20)
 */
export function assessDepth(
  quote: CanonicalQuote,
  lots: number,
  lotSize: number,
): DepthAssessment {
  const quantity = lots * lotSize;
  const reasons: string[] = [];

  if (!quote.depth) {
    return {
      securityId: quote.securityId,
      lots,
      lotSize,
      quantity,
      bestBid: null,
      bestAsk: null,
      midPrice: null,
      quotedSpreadPct: null,
      effectiveBuy: null,
      effectiveSell: null,
      roundtripPct: null,
      slippageMultiple: null,
      grade: 'F',
      reasons: ['No market depth in the response.'],
      buyWalk: null,
      sellWalk: null,
    };
  }

  // Ask ladder ascending (cheapest first), bid ladder descending (best first).
  const asks = quote.depth.sell
    .filter((l) => l.price > 0 && l.quantity > 0)
    .sort((a, b) => a.price - b.price);
  const bids = quote.depth.buy
    .filter((l) => l.price > 0 && l.quantity > 0)
    .sort((a, b) => b.price - a.price);

  const bestAsk = asks[0]?.price ?? null;
  const bestBid = bids[0]?.price ?? null;
  const midPrice = bestAsk !== null && bestBid !== null ? (bestAsk + bestBid) / 2 : null;

  const quotedSpreadPct =
    midPrice !== null && bestAsk !== null && bestBid !== null && midPrice > 0
      ? ((bestAsk - bestBid) / midPrice) * 100
      : null;

  const buyWalk = walkBook(asks, quantity);
  const sellWalk = walkBook(bids, quantity);

  if (buyWalk.incomplete) {
    reasons.push(
      `Ask side covers only ${buyWalk.filledQuantity}/${quantity} units across ` +
        `${asks.length} visible level(s).`,
    );
  }
  if (sellWalk.incomplete) {
    reasons.push(
      `Bid side covers only ${sellWalk.filledQuantity}/${quantity} units across ` +
        `${bids.length} visible level(s).`,
    );
  }

  const effectiveBuy = Number.isFinite(buyWalk.vwap) ? buyWalk.vwap : null;
  const effectiveSell = Number.isFinite(sellWalk.vwap) ? sellWalk.vwap : null;

  const roundtripPct =
    effectiveBuy !== null && effectiveSell !== null && midPrice !== null && midPrice > 0
      ? ((effectiveBuy - effectiveSell) / midPrice) * 100
      : null;

  const slippageMultiple =
    roundtripPct !== null && quotedSpreadPct !== null && quotedSpreadPct > 0
      ? roundtripPct / quotedSpreadPct
      : null;

  if (slippageMultiple !== null && slippageMultiple > 2) {
    reasons.push(
      `Real round-trip is ${slippageMultiple.toFixed(2)}x the quoted spread at ${lots} lot(s).`,
    );
  }

  const grade = gradeFor(roundtripPct, buyWalk.incomplete || sellWalk.incomplete);
  if (grade === 'A') reasons.push('Executable at size with minimal give-up.');

  return {
    securityId: quote.securityId,
    lots,
    lotSize,
    quantity,
    bestBid,
    bestAsk,
    midPrice,
    quotedSpreadPct,
    effectiveBuy,
    effectiveSell,
    roundtripPct,
    slippageMultiple,
    grade,
    reasons,
    buyWalk,
    sellWalk,
  };
}

export interface ScreenCandidate {
  strike: number;
  type: OptionType;
  securityId: string | null;
  lastPrice: number | null;
  topBid: number | null;
  topAsk: number | null;
  topBidQty: number | null;
  topAskQty: number | null;
  midPrice: number | null;
  quotedSpreadPct: number | null;
  oi: number | null;
  volume: number | null;
  /** True when this leg is worth spending a depth call on. */
  candidate: boolean;
  rejectReasons: string[];
}

export interface ScreenOptions {
  /** Reject legs whose quoted spread exceeds this, in percent of mid. */
  maxQuotedSpreadPct?: number;
  /** Reject legs below this open interest. */
  minOi?: number;
  /** Reject legs whose top-of-book cannot show at least this many units a side. */
  minTopQuantity?: number;
  /** Only screen strikes within this fraction of the reference level. */
  bandPct?: number;
  /** Reference level for the band. Use the FORWARD, never the index LTP. */
  reference?: number;
  /** Cap on how many candidates Stage 2 may cost. */
  maxCandidates?: number;
}

/**
 * Stage 1: screen every strike from chain top-of-book. Costs nothing extra.
 *
 * Returns every leg with its verdict, so the caller can show why something was
 * skipped rather than silently dropping it.
 */
export function screenChain(
  strikes: ChainStrike[],
  opts: ScreenOptions = {},
): { all: ScreenCandidate[]; candidates: ScreenCandidate[] } {
  const {
    maxQuotedSpreadPct = 1.5,
    minOi = 0,
    minTopQuantity = 1,
    bandPct,
    reference,
    maxCandidates = 12,
  } = opts;

  const all: ScreenCandidate[] = [];

  for (const s of strikes) {
    if (bandPct !== undefined && reference !== undefined) {
      if (Math.abs(s.strike - reference) > reference * bandPct) continue;
    }

    for (const type of ['CE', 'PE'] as const) {
      const leg = type === 'CE' ? s.ce : s.pe;
      if (!leg) continue;

      const bid = leg.topBidPrice;
      const ask = leg.topAskPrice;
      const mid = bid !== null && ask !== null && bid > 0 && ask > 0 ? (bid + ask) / 2 : null;
      const spreadPct = mid !== null && mid > 0 ? ((ask! - bid!) / mid) * 100 : null;

      const rejectReasons: string[] = [];
      if (mid === null) rejectReasons.push('No two-sided top-of-book.');
      if (bid !== null && ask !== null && bid >= ask && bid > 0 && ask > 0) {
        rejectReasons.push('Crossed top-of-book.');
      }
      if (spreadPct !== null && spreadPct > maxQuotedSpreadPct) {
        rejectReasons.push(
          `Quoted spread ${spreadPct.toFixed(2)}% exceeds ${maxQuotedSpreadPct}%.`,
        );
      }
      if (minOi > 0 && (leg.oi ?? 0) < minOi) {
        rejectReasons.push(`OI ${leg.oi ?? 0} below ${minOi}.`);
      }
      if ((leg.topBidQuantity ?? 0) < minTopQuantity) {
        rejectReasons.push('Bid side shows no size.');
      }
      if ((leg.topAskQuantity ?? 0) < minTopQuantity) {
        rejectReasons.push('Ask side shows no size.');
      }
      if (leg.securityId === null) {
        rejectReasons.push('No security_id — cannot fetch depth.');
      }

      all.push({
        strike: s.strike,
        type,
        securityId: leg.securityId,
        lastPrice: leg.lastPrice,
        topBid: bid,
        topAsk: ask,
        topBidQty: leg.topBidQuantity,
        topAskQty: leg.topAskQuantity,
        midPrice: mid,
        quotedSpreadPct: spreadPct,
        oi: leg.oi,
        volume: leg.volume,
        candidate: rejectReasons.length === 0,
        rejectReasons,
      });
    }
  }

  const candidates = all
    .filter((c) => c.candidate)
    .sort((a, b) => (a.quotedSpreadPct ?? 1e9) - (b.quotedSpreadPct ?? 1e9))
    .slice(0, maxCandidates);

  return { all, candidates };
}
