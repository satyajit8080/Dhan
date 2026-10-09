/**
 * Breakout / breakdown level engine. PURE MODULE — no I/O, no clock.
 *
 * Derives a real trigger level from UNDERLYING PRICE ACTION. Option-chain
 * strikes and OI peaks are never inputs here: those are confirmation, and
 * treating an OI wall as a technical level would make the trigger circular.
 *
 * Method, in order:
 *   1. collect candidate prices from swings, session extremes, opening range
 *      and VWAP
 *   2. cluster candidates that sit within a tolerance of each other
 *   3. count TOUCHES against the real candle series
 *   4. reject isolated spikes — a lone wick is not a level
 *   5. nearest surviving resistance above spot, support below
 *   6. add / subtract the confirmation buffer
 *
 * If no level survives, the answer is null. There is no fallback that makes one
 * up, because a fabricated trigger is worse than no signal.
 */

import type { Candle } from './endpoints/historical.js';
import { atr, openingRange, sessions, sessionVwap } from './indicators.js';
import { findSwingPoints } from './structure.js';

export type LevelSource =
  | 'swing_high'
  | 'swing_low'
  | 'session_high'
  | 'session_low'
  | 'previous_session_high'
  | 'previous_session_low'
  | 'opening_range_high'
  | 'opening_range_low'
  | 'vwap'
  | 'consolidation_high'
  | 'consolidation_low';

/**
 * Levels that stand on their own without touch confirmation.
 *
 * These are references from a COMPLETED period that traders watch regardless of
 * how often price has revisited them.
 *
 * The CURRENT session high and low are deliberately NOT here. They are derived
 * from the same candles being tested, so a single violent wick becomes "the
 * session high" and would earn an exemption it has not deserved. Exempting them
 * let a one-off spike to 75,500 become the trigger in testing. They are treated
 * as ordinary candidates and must be confirmed by touches like any swing.
 */
const STRUCTURAL: ReadonlySet<LevelSource> = new Set<LevelSource>([
  'previous_session_high',
  'previous_session_low',
  'opening_range_high',
  'opening_range_low',
  'vwap',
]);

export interface LevelCandidate {
  price: number;
  source: LevelSource;
}

export interface ConfirmedLevel {
  /** Representative price of the cluster (touch-weighted mean). */
  price: number;
  kind: 'resistance' | 'support';
  /** How many candles actually traded into this band. */
  touches: number;
  /** Every source that contributed to this cluster. */
  sources: LevelSource[];
  /** True when at least one source is structural. */
  structural: boolean;
  /** Half-width of the cluster band, in points. */
  tolerance: number;
  distanceFromSpot: number;
  distanceInAtr: number | null;
  /** Why this level was kept, in plain words. */
  note: string;
}

export interface RejectedLevel {
  price: number;
  sources: LevelSource[];
  touches: number;
  reason: string;
}

export interface LevelConfig {
  /** Points added above resistance / subtracted below support. */
  confirmationBuffer?: number;
  /** Cluster half-width. Defaults to max(0.03% of spot, 0.25 * ATR). */
  tolerance?: number;
  /** Minimum touches for a NON-structural level to survive. */
  minTouches?: number;
  /** Fractal lookback for swing detection. */
  swingLookback?: number;
  /** Bars used for the consolidation range. */
  consolidationWindow?: number;
  /** Round the final trigger to this step. 0 disables. */
  roundTo?: number;
}

export interface LevelResult {
  spot: number;
  atr14: number | null;
  toleranceUsed: number;
  confirmationBuffer: number;

  /** Nearest confirmed resistance above spot. Null when none survived. */
  resistance: ConfirmedLevel | null;
  /** Nearest confirmed support below spot. Null when none survived. */
  support: ConfirmedLevel | null;

  /** resistance.price + buffer, rounded. Null when no resistance. */
  breakoutAbove: number | null;
  /** support.price - buffer, rounded. Null when no support. */
  breakdownBelow: number | null;

  /** Next resistance beyond the first — a natural target ladder. */
  nextResistance: ConfirmedLevel | null;
  nextSupport: ConfirmedLevel | null;

  allResistance: ConfirmedLevel[];
  allSupport: ConfirmedLevel[];
  rejected: RejectedLevel[];

  barsAnalyzed: number;
  timeframeMinutes: number | null;
  diagnostics: string[];
}

/** Aggregate 1-minute candles into N-minute candles, aligned to the series start. */
export function aggregateCandles(candles: Candle[], minutes: number): Candle[] {
  if (candles.length === 0 || minutes <= 1) return candles;

  const bucketMs = minutes * 60_000;
  const out: Candle[] = [];
  let bucket: Candle[] = [];
  let bucketStart = Math.floor(candles[0]!.timestampMs / bucketMs) * bucketMs;

  const flush = () => {
    if (bucket.length === 0) return;
    let high = -Infinity;
    let low = Infinity;
    let volume = 0;
    let hasVol = false;
    for (const b of bucket) {
      if (b.high > high) high = b.high;
      if (b.low < low) low = b.low;
      if (b.volume != null) {
        volume += b.volume;
        hasVol = true;
      }
    }
    out.push({
      timestampMs: bucket[0]!.timestampMs,
      open: bucket[0]!.open,
      high,
      low,
      close: bucket[bucket.length - 1]!.close,
      volume: hasVol ? volume : null,
      openInterest: bucket[bucket.length - 1]!.openInterest,
    });
    bucket = [];
  };

  for (const c of candles) {
    const thisBucket = Math.floor(c.timestampMs / bucketMs) * bucketMs;
    if (thisBucket !== bucketStart) {
      flush();
      bucketStart = thisBucket;
    }
    bucket.push(c);
  }
  flush();
  return out;
}

/** Every price the series offers as a possible level. */
export function collectCandidates(
  candles: Candle[],
  cfg: Required<Pick<LevelConfig, 'swingLookback' | 'consolidationWindow'>>,
): LevelCandidate[] {
  const out: LevelCandidate[] = [];
  const sess = sessions(candles);
  const current = sess.length > 0 ? sess[sess.length - 1]! : null;
  const previous = sess.length > 1 ? sess[sess.length - 2]! : null;
  const or = openingRange(candles, 15);

  for (const s of findSwingPoints(candles, cfg.swingLookback)) {
    out.push({ price: s.price, source: s.kind === 'high' ? 'swing_high' : 'swing_low' });
  }

  if (current) {
    out.push({ price: current.high, source: 'session_high' });
    out.push({ price: current.low, source: 'session_low' });
  }
  if (previous) {
    out.push({ price: previous.high, source: 'previous_session_high' });
    out.push({ price: previous.low, source: 'previous_session_low' });
  }
  if (or) {
    out.push({ price: or.high, source: 'opening_range_high' });
    out.push({ price: or.low, source: 'opening_range_low' });
  }

  const v = sessionVwap(candles);
  if (v !== null) out.push({ price: v, source: 'vwap' });

  const window = candles.slice(-Math.min(cfg.consolidationWindow, candles.length));
  if (window.length > 0) {
    let hi = -Infinity;
    let lo = Infinity;
    for (const c of window) {
      if (c.high > hi) hi = c.high;
      if (c.low < lo) lo = c.low;
    }
    out.push({ price: hi, source: 'consolidation_high' });
    out.push({ price: lo, source: 'consolidation_low' });
  }

  return out.filter((c) => Number.isFinite(c.price));
}

/**
 * Count candles whose high (resistance) or low (support) traded into the band.
 *
 * This is what separates a level from a spike: a price only counts as a level
 * if the market actually revisited it.
 */
export function countTouches(
  candles: Candle[],
  price: number,
  tolerance: number,
  kind: 'resistance' | 'support',
): number {
  let n = 0;
  for (const c of candles) {
    const probe = kind === 'resistance' ? c.high : c.low;
    if (Math.abs(probe - price) <= tolerance) n++;
  }
  return n;
}

interface Cluster {
  prices: number[];
  sources: LevelSource[];
}

function clusterCandidates(cands: LevelCandidate[], tolerance: number): Cluster[] {
  const sorted = [...cands].sort((a, b) => a.price - b.price);
  const clusters: Cluster[] = [];

  for (const c of sorted) {
    const last = clusters[clusters.length - 1];
    if (last) {
      const mean = last.prices.reduce((a, b) => a + b, 0) / last.prices.length;
      if (Math.abs(c.price - mean) <= tolerance) {
        last.prices.push(c.price);
        if (!last.sources.includes(c.source)) last.sources.push(c.source);
        continue;
      }
    }
    clusters.push({ prices: [c.price], sources: [c.source] });
  }
  return clusters;
}

function roundTo(value: number, step: number): number {
  if (!step || step <= 0) return value;
  return Math.round(value / step) * step;
}

/**
 * Derive confirmed levels and the trigger prices.
 *
 * @param candles series to analyse (1-minute preferred, per the spec)
 * @param spot    live underlying price
 */
export function deriveLevels(
  candles: Candle[],
  spot: number,
  config: LevelConfig = {},
): LevelResult {
  const diagnostics: string[] = [];

  const a = atr(candles, 14);
  const buffer = config.confirmationBuffer ?? 5;
  const minTouches = config.minTouches ?? 2;
  const swingLookback = config.swingLookback ?? 2;
  const consolidationWindow = config.consolidationWindow ?? 20;
  const round = config.roundTo ?? 5;

  // Tolerance: wide enough to group a cluster of near-identical highs, narrow
  // enough not to merge genuinely distinct levels.
  const tolerance =
    config.tolerance ?? Math.max(spot * 0.0003, a !== null ? a * 0.25 : 0, 2);

  const empty: LevelResult = {
    spot,
    atr14: a,
    toleranceUsed: tolerance,
    confirmationBuffer: buffer,
    resistance: null,
    support: null,
    breakoutAbove: null,
    breakdownBelow: null,
    nextResistance: null,
    nextSupport: null,
    allResistance: [],
    allSupport: [],
    rejected: [],
    barsAnalyzed: candles.length,
    timeframeMinutes: null,
    diagnostics,
  };

  if (candles.length === 0) {
    diagnostics.push('No candles supplied — no level can be derived.');
    return empty;
  }
  if (!Number.isFinite(spot) || spot <= 0) {
    diagnostics.push('Spot price unavailable — cannot place levels relative to price.');
    return empty;
  }

  const tfMs =
    candles.length > 1 ? candles[1]!.timestampMs - candles[0]!.timestampMs : null;
  const timeframeMinutes = tfMs ? Math.round(tfMs / 60_000) : null;

  const candidates = collectCandidates(candles, { swingLookback, consolidationWindow });
  if (candidates.length === 0) {
    diagnostics.push('No candidate levels found in the series.');
    return { ...empty, timeframeMinutes };
  }

  const clusters = clusterCandidates(candidates, tolerance);
  const confirmed: ConfirmedLevel[] = [];
  const rejected: RejectedLevel[] = [];

  for (const cl of clusters) {
    const mean = cl.prices.reduce((x, y) => x + y, 0) / cl.prices.length;
    const isResistance = mean > spot;
    const kind: 'resistance' | 'support' = isResistance ? 'resistance' : 'support';

    const touches = countTouches(candles, mean, tolerance, kind);
    const structural = cl.sources.some((s) => STRUCTURAL.has(s));

    // Spike rejection. A lone swing that the market never revisited is a wick,
    // not a level. Structural levels are exempt: a previous session high is a
    // reference whether or not it has been retested.
    if (!structural && touches < minTouches) {
      rejected.push({
        price: mean,
        sources: cl.sources,
        touches,
        reason:
          `Isolated spike: ${touches} touch(es) within ${tolerance.toFixed(1)} pts, ` +
          `below the ${minTouches} required for a non-structural level.`,
      });
      continue;
    }

    const distance = mean - spot;
    confirmed.push({
      price: mean,
      kind,
      touches,
      sources: cl.sources,
      structural,
      tolerance,
      distanceFromSpot: distance,
      distanceInAtr: a && a > 0 ? distance / a : null,
      note: structural
        ? `Structural level (${cl.sources.join(', ')}), ${touches} touch(es).`
        : `Confirmed by ${touches} touches within ${tolerance.toFixed(1)} pts.`,
    });
  }

  const allResistance = confirmed
    .filter((l) => l.kind === 'resistance')
    .sort((x, y) => x.price - y.price);
  const allSupport = confirmed
    .filter((l) => l.kind === 'support')
    .sort((x, y) => y.price - x.price);

  const resistance = allResistance[0] ?? null;
  const support = allSupport[0] ?? null;

  if (!resistance) {
    diagnostics.push(
      'No confirmed resistance above spot. Price may be at the session extreme, ' +
        'or every candidate above spot was an isolated spike.',
    );
  }
  if (!support) {
    diagnostics.push(
      'No confirmed support below spot. Price may be at the session extreme, ' +
        'or every candidate below spot was an isolated spike.',
    );
  }
  diagnostics.push(
    `${confirmed.length} level(s) confirmed, ${rejected.length} rejected as spikes.`,
  );

  return {
    spot,
    atr14: a,
    toleranceUsed: tolerance,
    confirmationBuffer: buffer,
    resistance,
    support,
    breakoutAbove: resistance ? roundTo(resistance.price + buffer, round) : null,
    breakdownBelow: support ? roundTo(support.price - buffer, round) : null,
    nextResistance: allResistance[1] ?? null,
    nextSupport: allSupport[1] ?? null,
    allResistance,
    allSupport,
    rejected,
    barsAnalyzed: candles.length,
    timeframeMinutes,
    diagnostics,
  };
}

// ---------------------------------------------------------------------------
// Trade levels derived from the ladder
// ---------------------------------------------------------------------------

export interface TradePlan {
  side: 'CE' | 'PE';
  /** Underlying trigger. */
  triggerLevel: number;
  /** Underlying target — the next level in the ladder, or ATR-projected. */
  targetLevel: number;
  targetSource: string;
  /** Underlying invalidation. */
  stopLevel: number;
  stopSource: string;
  /** Option premium now, with the underlying at `levels.spot`. */
  entryPremium: number;
  /**
   * Premium projections, second order in the underlying move FROM SPOT:
   *   V(x) ~= V0 + delta*(x - spot) + 0.5*gamma*(x - spot)^2
   * The Greeks are measured at spot, so the expansion must be centred there.
   * These are ESTIMATES from the Greeks, not quotes.
   */
  /** Projected premium when the underlying reaches the trigger. */
  triggerPremium: number | null;
  targetPremium: number | null;
  stopPremium: number | null;
  riskRewardRatio: number | null;
  note: string;
}

/**
 * Project option premium at a given underlying level using delta and gamma.
 * Returns null when the Greeks are unavailable — never a guessed price.
 */
export function projectPremium(
  entryPremium: number,
  delta: number | null,
  gamma: number | null,
  moveInPoints: number,
): number | null {
  if (delta === null) return null;
  const second = gamma !== null ? 0.5 * gamma * moveInPoints * moveInPoints : 0;
  return Math.max(0, entryPremium + delta * moveInPoints + second);
}

/**
 * Build entry / target / stop from the confirmed level ladder.
 *
 * Target defaults to the NEXT level in the direction of the trade; when no
 * further level exists, it projects one ATR beyond the trigger. Stop defaults
 * to the opposing confirmed level. Both are documented conventions, not market
 * data, and both are configurable by the caller.
 */
export function buildTradePlan(
  side: 'CE' | 'PE',
  levels: LevelResult,
  entryPremium: number,
  delta: number | null,
  gamma: number | null,
): TradePlan | null {
  const atrVal = levels.atr14;

  if (side === 'CE') {
    if (levels.breakoutAbove === null) return null;
    const trigger = levels.breakoutAbove;

    const target =
      levels.nextResistance?.price ?? (atrVal ? trigger + atrVal : trigger * 1.002);
    const targetSource = levels.nextResistance
      ? `Next confirmed resistance (${levels.nextResistance.touches} touches).`
      : 'No further resistance confirmed; projected one ATR above the trigger.';

    const stop = levels.support?.price ?? (atrVal ? trigger - atrVal : trigger * 0.998);
    const stopSource = levels.support
      ? `Nearest confirmed support (${levels.support.touches} touches).`
      : 'No confirmed support; projected one ATR below the trigger.';

    return {
      side,
      triggerLevel: trigger,
      targetLevel: target,
      targetSource,
      stopLevel: stop,
      stopSource,
      entryPremium,
      triggerPremium: projectPremium(entryPremium, delta, gamma, trigger - levels.spot),
      targetPremium: projectPremium(entryPremium, delta, gamma, target - levels.spot),
      stopPremium: projectPremium(entryPremium, delta, gamma, stop - levels.spot),
      riskRewardRatio:
        trigger - stop !== 0 ? Math.abs((target - trigger) / (trigger - stop)) : null,
      note:
        'Premium targets are delta/gamma projections from current spot, not quotes. ' +
        'Underlying levels come from confirmed price action.',
    };
  }

  if (levels.breakdownBelow === null) return null;
  const trigger = levels.breakdownBelow;

  const target = levels.nextSupport?.price ?? (atrVal ? trigger - atrVal : trigger * 0.998);
  const targetSource = levels.nextSupport
    ? `Next confirmed support (${levels.nextSupport.touches} touches).`
    : 'No further support confirmed; projected one ATR below the trigger.';

  const stop = levels.resistance?.price ?? (atrVal ? trigger + atrVal : trigger * 1.002);
  const stopSource = levels.resistance
    ? `Nearest confirmed resistance (${levels.resistance.touches} touches).`
    : 'No confirmed resistance; projected one ATR above the trigger.';

  return {
    side,
    triggerLevel: trigger,
    targetLevel: target,
    targetSource,
    stopLevel: stop,
    stopSource,
    entryPremium,
    // A put gains as the underlying falls; delta is already negative.
    triggerPremium: projectPremium(entryPremium, delta, gamma, trigger - levels.spot),
    targetPremium: projectPremium(entryPremium, delta, gamma, target - levels.spot),
    stopPremium: projectPremium(entryPremium, delta, gamma, stop - levels.spot),
    riskRewardRatio:
      stop - trigger !== 0 ? Math.abs((trigger - target) / (stop - trigger)) : null,
    note:
      'Premium targets are delta/gamma projections from current spot, not quotes. ' +
      'Underlying levels come from confirmed price action.',
  };
}
