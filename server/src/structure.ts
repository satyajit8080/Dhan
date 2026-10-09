/**
 * Price structure. PURE MODULE — no I/O, no clock.
 *
 * Finds swing points, classifies the higher-high / lower-low sequence, measures
 * consolidation, and enumerates CANDIDATE levels a breakout rule might use.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: it never nominates "the" breakout level
 * and never says a breakout has occurred. It reports that a level exists and
 * whether price is above or below it. Choosing which level triggers a trade is
 * a strategy decision, and this server has no strategy.
 */

import type { Candle } from './endpoints/historical.js';
import { istDateOf, istTimeOf, sessions, openingRange, atr } from './indicators.js';

export interface SwingPoint {
  index: number;
  timestampMs: number;
  istTime: string;
  price: number;
  kind: 'high' | 'low';
}

/**
 * Fractal swing points: a bar whose high exceeds `lookback` bars either side
 * (or whose low undercuts them).
 *
 * The last `lookback` bars can never qualify — their right side has not formed
 * yet. That is correct, not a gap: a swing you can only see in hindsight is not
 * a swing you could have traded.
 */
export function findSwingPoints(candles: Candle[], lookback = 2): SwingPoint[] {
  const out: SwingPoint[] = [];
  if (candles.length < lookback * 2 + 1) return out;

  for (let i = lookback; i < candles.length - lookback; i++) {
    const c = candles[i]!;
    let isHigh = true;
    let isLow = true;

    for (let j = i - lookback; j <= i + lookback; j++) {
      if (j === i) continue;
      if (candles[j]!.high >= c.high) isHigh = false;
      if (candles[j]!.low <= c.low) isLow = false;
    }

    if (isHigh) {
      out.push({
        index: i,
        timestampMs: c.timestampMs,
        istTime: istTimeOf(c.timestampMs),
        price: c.high,
        kind: 'high',
      });
    } else if (isLow) {
      out.push({
        index: i,
        timestampMs: c.timestampMs,
        istTime: istTimeOf(c.timestampMs),
        price: c.low,
        kind: 'low',
      });
    }
  }

  return out;
}

export type StructurePattern =
  | 'higher_highs_higher_lows'
  | 'lower_highs_lower_lows'
  | 'higher_highs_lower_lows'
  | 'lower_highs_higher_lows'
  | 'indeterminate';

export interface SwingStructure {
  pattern: StructurePattern;
  lastTwoHighs: number[];
  lastTwoLows: number[];
  highsRising: boolean | null;
  lowsRising: boolean | null;
  swingCount: number;
  note: string;
}

const PATTERN_NOTES: Record<StructurePattern, string> = {
  higher_highs_higher_lows: 'Higher highs and higher lows.',
  lower_highs_lower_lows: 'Lower highs and lower lows.',
  higher_highs_lower_lows: 'Higher highs with lower lows — expanding range.',
  lower_highs_higher_lows: 'Lower highs with higher lows — contracting range.',
  indeterminate: 'Not enough confirmed swing points to classify.',
};

/** Classify the last two highs and last two lows. Descriptive only. */
export function classifyStructure(swings: SwingPoint[]): SwingStructure {
  const highs = swings.filter((s) => s.kind === 'high').map((s) => s.price);
  const lows = swings.filter((s) => s.kind === 'low').map((s) => s.price);

  const lastTwoHighs = highs.slice(-2);
  const lastTwoLows = lows.slice(-2);

  const highsRising =
    lastTwoHighs.length === 2 ? lastTwoHighs[1]! > lastTwoHighs[0]! : null;
  const lowsRising = lastTwoLows.length === 2 ? lastTwoLows[1]! > lastTwoLows[0]! : null;

  let pattern: StructurePattern = 'indeterminate';
  if (highsRising !== null && lowsRising !== null) {
    if (highsRising && lowsRising) pattern = 'higher_highs_higher_lows';
    else if (!highsRising && !lowsRising) pattern = 'lower_highs_lower_lows';
    else if (highsRising && !lowsRising) pattern = 'higher_highs_lower_lows';
    else pattern = 'lower_highs_higher_lows';
  }

  return {
    pattern,
    lastTwoHighs,
    lastTwoLows,
    highsRising,
    lowsRising,
    swingCount: swings.length,
    note: PATTERN_NOTES[pattern],
  };
}

export interface ConsolidationResult {
  /** Range of the window, in points. */
  rangePoints: number;
  /** Range as a multiple of ATR. Below ~1.5 is typically tight. */
  rangeInAtr: number | null;
  /** Range as a percent of price. */
  rangePct: number;
  high: number;
  low: number;
  bars: number;
  note: string;
}

/**
 * Measure how tight the recent range is. Reports the measurement; does not
 * declare "consolidation", because where that line sits is a strategy choice.
 */
export function measureConsolidation(
  candles: Candle[],
  window = 20,
): ConsolidationResult | null {
  if (candles.length === 0) return null;
  const slice = candles.slice(-Math.min(window, candles.length));

  let high = -Infinity;
  let low = Infinity;
  for (const c of slice) {
    if (c.high > high) high = c.high;
    if (c.low < low) low = c.low;
  }

  const range = high - low;
  const a = atr(candles, 14);
  const last = candles[candles.length - 1]!.close;

  return {
    rangePoints: range,
    rangeInAtr: a && a > 0 ? range / a : null,
    rangePct: last > 0 ? (range / last) * 100 : 0,
    high,
    low,
    bars: slice.length,
    note:
      'Range measurement only. No threshold is applied, because what counts as ' +
      'consolidation is a strategy parameter this server does not hold.',
  };
}

export interface CandidateLevel {
  label: string;
  price: number;
  /** Where the last close sits relative to this level. */
  side: 'above' | 'below' | 'at';
  distancePoints: number;
  distanceInAtr: number | null;
  source: string;
}

export interface LevelSet {
  lastClose: number;
  atr14: number | null;
  candidates: CandidateLevel[];
  note: string;
}

/**
 * Enumerate candidate levels with price's position relative to each.
 *
 * This is a menu, not a recommendation. Every entry is a level some strategy
 * might use as a trigger; nothing here says which one is yours.
 */
export function candidateLevels(candles: Candle[]): LevelSet | null {
  if (candles.length === 0) return null;

  const last = candles[candles.length - 1]!.close;
  const a = atr(candles, 14);
  const sess = sessions(candles);
  const current = sess.length > 0 ? sess[sess.length - 1]! : null;
  const previous = sess.length > 1 ? sess[sess.length - 2]! : null;
  const or15 = openingRange(candles, 15);
  const swings = findSwingPoints(candles, 2);
  const lastSwingHigh = [...swings].reverse().find((s) => s.kind === 'high');
  const lastSwingLow = [...swings].reverse().find((s) => s.kind === 'low');

  const raw: { label: string; price: number | null | undefined; source: string }[] = [
    { label: 'session_high', price: current?.high, source: 'Current session high.' },
    { label: 'session_low', price: current?.low, source: 'Current session low.' },
    { label: 'session_open', price: current?.open, source: 'Current session open.' },
    { label: 'previous_session_high', price: previous?.high, source: 'Previous session high.' },
    { label: 'previous_session_low', price: previous?.low, source: 'Previous session low.' },
    { label: 'previous_session_close', price: previous?.close, source: 'Previous session close.' },
    { label: 'opening_range_high', price: or15?.high, source: 'High of the first 15 minutes.' },
    { label: 'opening_range_low', price: or15?.low, source: 'Low of the first 15 minutes.' },
    { label: 'last_swing_high', price: lastSwingHigh?.price, source: 'Most recent confirmed swing high.' },
    { label: 'last_swing_low', price: lastSwingLow?.price, source: 'Most recent confirmed swing low.' },
  ];

  const candidates: CandidateLevel[] = [];
  for (const r of raw) {
    if (r.price == null || !Number.isFinite(r.price)) continue;
    const d = last - r.price;
    candidates.push({
      label: r.label,
      price: r.price,
      side: d > 0 ? 'above' : d < 0 ? 'below' : 'at',
      distancePoints: d,
      distanceInAtr: a && a > 0 ? d / a : null,
      source: r.source,
    });
  }

  candidates.sort((x, y) => Math.abs(x.distancePoints) - Math.abs(y.distancePoints));

  return {
    lastClose: last,
    atr14: a,
    candidates,
    note:
      'Candidate levels only. This server does not nominate a trigger level and ' +
      'does not declare a breakout — those are strategy decisions.',
  };
}

export interface StructureAnalysis {
  bars: number;
  lastClose: number;
  lastBarIst: string;
  swings: SwingPoint[];
  structure: SwingStructure;
  consolidation: ConsolidationResult | null;
  levels: LevelSet | null;
}

/** Full structural read of a candle series. */
export function analyzeStructure(
  candles: Candle[],
  opts: { swingLookback?: number; consolidationWindow?: number; maxSwings?: number } = {},
): StructureAnalysis | null {
  if (candles.length === 0) return null;

  const swings = findSwingPoints(candles, opts.swingLookback ?? 2);
  const last = candles[candles.length - 1]!;
  const maxSwings = opts.maxSwings ?? 12;

  return {
    bars: candles.length,
    lastClose: last.close,
    lastBarIst: `${istDateOf(last.timestampMs)} ${istTimeOf(last.timestampMs)}`,
    swings: swings.slice(-maxSwings),
    structure: classifyStructure(swings),
    consolidation: measureConsolidation(candles, opts.consolidationWindow ?? 20),
    levels: candidateLevels(candles),
  };
}
