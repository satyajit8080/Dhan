/**
 * Technical indicators. PURE MODULE — no I/O, no clock, no config.
 *
 * WHY THESE ARE COMPUTED HERE AND NOT BY THE MODEL: a 375-bar intraday series
 * cannot be turned into an EMA or a Wilder-smoothed ADX by reading it. Handing
 * a model raw candles and asking for an indicator produces a plausible number,
 * not a correct one. Every value here is deterministic and unit-tested, so a
 * signal rule built on it is reproducible.
 *
 * Nothing in this file decides direction or emits a level. It reports what the
 * series did. Interpretation is the caller's job.
 */

import type { Candle } from './endpoints/historical.js';

const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;

/** YYYY-MM-DD of an epoch-ms instant, in IST. */
export function istDateOf(epochMs: number): string {
  return new Date(epochMs + IST_OFFSET_MS).toISOString().slice(0, 10);
}

/** HH:MM of an epoch-ms instant, in IST. */
export function istTimeOf(epochMs: number): string {
  return new Date(epochMs + IST_OFFSET_MS).toISOString().slice(11, 16);
}

// ---------------------------------------------------------------------------
// Moving averages
// ---------------------------------------------------------------------------

/** Simple moving average of the last `period` values. Null if too few. */
export function sma(values: number[], period: number): number | null {
  if (period <= 0 || values.length < period) return null;
  let sum = 0;
  for (let i = values.length - period; i < values.length; i++) sum += values[i]!;
  return sum / period;
}

/**
 * Exponential moving average, seeded with the SMA of the first `period` values.
 * Returns the full series (null until the seed is available).
 */
export function emaSeries(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (period <= 0 || values.length < period) return out;

  const k = 2 / (period + 1);
  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i]!;
  let prev = seed / period;
  out[period - 1] = prev;

  for (let i = period; i < values.length; i++) {
    prev = values[i]! * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export function ema(values: number[], period: number): number | null {
  const s = emaSeries(values, period);
  return s.length ? (s[s.length - 1] ?? null) : null;
}

// ---------------------------------------------------------------------------
// True range family
// ---------------------------------------------------------------------------

/** True range per bar. First bar uses high-low, having no previous close. */
export function trueRanges(candles: Candle[]): number[] {
  const tr: number[] = [];
  for (let i = 0; i < candles.length; i++) {
    const c = candles[i]!;
    if (i === 0) {
      tr.push(c.high - c.low);
      continue;
    }
    const pc = candles[i - 1]!.close;
    tr.push(Math.max(c.high - c.low, Math.abs(c.high - pc), Math.abs(c.low - pc)));
  }
  return tr;
}

/**
 * Wilder's smoothing: seed with the simple mean of the first `period`, then
 * prev + (x - prev)/period. This is NOT an EMA with k=2/(n+1); using one in
 * place of the other is the most common way ATR and ADX come out wrong.
 */
export function wilderSmooth(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (period <= 0 || values.length < period) return out;

  let sum = 0;
  for (let i = 0; i < period; i++) sum += values[i]!;
  let prev = sum / period;
  out[period - 1] = prev;

  for (let i = period; i < values.length; i++) {
    prev = prev + (values[i]! - prev) / period;
    out[i] = prev;
  }
  return out;
}

/** Average true range, Wilder-smoothed. */
export function atr(candles: Candle[], period = 14): number | null {
  if (candles.length < period + 1) return null;
  const s = wilderSmooth(trueRanges(candles), period);
  return s[s.length - 1] ?? null;
}

// ---------------------------------------------------------------------------
// ADX / DI
// ---------------------------------------------------------------------------

export interface AdxResult {
  adx: number | null;
  plusDi: number | null;
  minusDi: number | null;
}

/**
 * Wilder's ADX with +DI and -DI.
 *
 * Needs roughly 2*period bars before ADX itself is meaningful: one period to
 * smooth DM and TR, another to smooth DX into ADX.
 */
export function adx(candles: Candle[], period = 14): AdxResult {
  const n = candles.length;
  if (n < period * 2) return { adx: null, plusDi: null, minusDi: null };

  const plusDm: number[] = [];
  const minusDm: number[] = [];
  const tr: number[] = [];

  for (let i = 1; i < n; i++) {
    const c = candles[i]!;
    const p = candles[i - 1]!;
    const up = c.high - p.high;
    const down = p.low - c.low;

    plusDm.push(up > down && up > 0 ? up : 0);
    minusDm.push(down > up && down > 0 ? down : 0);
    tr.push(Math.max(c.high - c.low, Math.abs(c.high - p.close), Math.abs(c.low - p.close)));
  }

  const sTr = wilderSmooth(tr, period);
  const sPlus = wilderSmooth(plusDm, period);
  const sMinus = wilderSmooth(minusDm, period);

  const dx: number[] = [];
  const dxIndex: number[] = [];
  for (let i = 0; i < sTr.length; i++) {
    const t = sTr[i];
    const p = sPlus[i];
    const m = sMinus[i];
    if (t == null || p == null || m == null || t === 0) continue;
    const pdi = (100 * p) / t;
    const mdi = (100 * m) / t;
    const sum = pdi + mdi;
    if (sum === 0) continue;
    dx.push((100 * Math.abs(pdi - mdi)) / sum);
    dxIndex.push(i);
  }

  if (dx.length < period) {
    const last = sTr.length - 1;
    const t = sTr[last];
    const p = sPlus[last];
    const m = sMinus[last];
    return {
      adx: null,
      plusDi: t && p != null && t !== 0 ? (100 * p) / t : null,
      minusDi: t && m != null && t !== 0 ? (100 * m) / t : null,
    };
  }

  const adxSeries = wilderSmooth(dx, period);
  const lastIdx = sTr.length - 1;
  const t = sTr[lastIdx]!;
  const p = sPlus[lastIdx]!;
  const m = sMinus[lastIdx]!;

  return {
    adx: adxSeries[adxSeries.length - 1] ?? null,
    plusDi: t === 0 ? null : (100 * p) / t,
    minusDi: t === 0 ? null : (100 * m) / t,
  };
}

// ---------------------------------------------------------------------------
// RSI
// ---------------------------------------------------------------------------

/** Wilder's RSI. */
export function rsi(values: number[], period = 14): number | null {
  if (values.length < period + 1) return null;
  const gains: number[] = [];
  const losses: number[] = [];
  for (let i = 1; i < values.length; i++) {
    const d = values[i]! - values[i - 1]!;
    gains.push(d > 0 ? d : 0);
    losses.push(d < 0 ? -d : 0);
  }
  const g = wilderSmooth(gains, period);
  const l = wilderSmooth(losses, period);
  const lg = g[g.length - 1];
  const ll = l[l.length - 1];
  if (lg == null || ll == null) return null;
  if (ll === 0) return 100;
  const rs = lg / ll;
  return 100 - 100 / (1 + rs);
}

// ---------------------------------------------------------------------------
// VWAP
// ---------------------------------------------------------------------------

/**
 * Session VWAP over the supplied bars, using the typical price.
 *
 * Returns null when no bar carries volume — index series often report zero
 * volume, and a VWAP computed from zero volume is not a VWAP. Reporting null
 * is the honest answer; reporting the mean price dressed up as VWAP is not.
 */
export function vwap(candles: Candle[]): number | null {
  let pv = 0;
  let vol = 0;
  for (const c of candles) {
    const v = c.volume ?? 0;
    if (v <= 0) continue;
    pv += ((c.high + c.low + c.close) / 3) * v;
    vol += v;
  }
  return vol > 0 ? pv / vol : null;
}

// ---------------------------------------------------------------------------
// Structure
// ---------------------------------------------------------------------------

export interface SessionStructure {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  bars: number;
}

/** Group bars into IST sessions, oldest first. */
export function sessions(candles: Candle[]): SessionStructure[] {
  const byDate = new Map<string, Candle[]>();
  for (const c of candles) {
    const d = istDateOf(c.timestampMs);
    const arr = byDate.get(d);
    if (arr) arr.push(c);
    else byDate.set(d, [c]);
  }

  const out: SessionStructure[] = [];
  for (const [date, bars] of byDate) {
    let high = -Infinity;
    let low = Infinity;
    let volume = 0;
    let hasVol = false;
    for (const b of bars) {
      if (b.high > high) high = b.high;
      if (b.low < low) low = b.low;
      if (b.volume != null) {
        volume += b.volume;
        hasVol = true;
      }
    }
    out.push({
      date,
      open: bars[0]!.open,
      high,
      low,
      close: bars[bars.length - 1]!.close,
      volume: hasVol ? volume : null,
      bars: bars.length,
    });
  }
  out.sort((a, b) => a.date.localeCompare(b.date));
  return out;
}

export interface RangeWindow {
  high: number;
  low: number;
  bars: number;
  fromIst: string;
  toIst: string;
}

/**
 * High/low of the first `minutes` of the most recent session.
 * Null when the session has not yet covered that window.
 */
export function openingRange(candles: Candle[], minutes = 15): RangeWindow | null {
  if (candles.length === 0) return null;
  const lastDate = istDateOf(candles[candles.length - 1]!.timestampMs);
  const day = candles.filter((c) => istDateOf(c.timestampMs) === lastDate);
  if (day.length === 0) return null;

  const startMs = day[0]!.timestampMs;
  const windowBars = day.filter((c) => c.timestampMs < startMs + minutes * 60_000);
  if (windowBars.length === 0) return null;

  let high = -Infinity;
  let low = Infinity;
  for (const b of windowBars) {
    if (b.high > high) high = b.high;
    if (b.low < low) low = b.low;
  }
  return {
    high,
    low,
    bars: windowBars.length,
    fromIst: istTimeOf(windowBars[0]!.timestampMs),
    toIst: istTimeOf(windowBars[windowBars.length - 1]!.timestampMs),
  };
}

/** Highest high and lowest low over the last `lookback` bars. */
export function swingRange(candles: Candle[], lookback: number): RangeWindow | null {
  if (candles.length === 0) return null;
  const slice = candles.slice(-Math.min(lookback, candles.length));
  let high = -Infinity;
  let low = Infinity;
  for (const b of slice) {
    if (b.high > high) high = b.high;
    if (b.low < low) low = b.low;
  }
  return {
    high,
    low,
    bars: slice.length,
    fromIst: istTimeOf(slice[0]!.timestampMs),
    toIst: istTimeOf(slice[slice.length - 1]!.timestampMs),
  };
}

// ---------------------------------------------------------------------------
// Aggregate
// ---------------------------------------------------------------------------

export interface IndicatorSet {
  bars: number;
  lastClose: number;
  lastBarIst: string;
  ema: { p9: number | null; p20: number | null; p50: number | null; p200: number | null };
  adx: AdxResult;
  atr14: number | null;
  rsi14: number | null;
  vwap: number | null;
  vwapNote: string | null;
  momentum: {
    /** Close minus close `n` bars ago, in points. */
    roc5: number | null;
    roc15: number | null;
    roc60: number | null;
  };
  volume: {
    lastBar: number | null;
    avg20: number | null;
    /** lastBar / avg20. Null when volume is absent (common on index series). */
    relative: number | null;
  };
  structure: {
    currentSession: SessionStructure | null;
    previousSession: SessionStructure | null;
    openingRange15: RangeWindow | null;
    last20Bars: RangeWindow | null;
    last60Bars: RangeWindow | null;
  };
}

function rocPoints(closes: number[], n: number): number | null {
  if (closes.length <= n) return null;
  return closes[closes.length - 1]! - closes[closes.length - 1 - n]!;
}

/**
 * Everything a direction rule might need, computed once from one series.
 *
 * Emits no verdict: no trend label, no breakout level, no bias. Those are
 * decisions, and the decision rules are not defined yet.
 */
export function computeIndicators(candles: Candle[]): IndicatorSet | null {
  if (candles.length === 0) return null;

  const closes = candles.map((c) => c.close);
  const last = candles[candles.length - 1]!;
  const sess = sessions(candles);
  const v = vwap(candles);

  const vols = candles.map((c) => c.volume ?? 0);
  const hasVolume = vols.some((x) => x > 0);
  const avg20 = hasVolume ? sma(vols, Math.min(20, vols.length)) : null;
  const lastVol = last.volume ?? null;

  return {
    bars: candles.length,
    lastClose: last.close,
    lastBarIst: `${istDateOf(last.timestampMs)} ${istTimeOf(last.timestampMs)}`,
    ema: {
      p9: ema(closes, 9),
      p20: ema(closes, 20),
      p50: ema(closes, 50),
      p200: ema(closes, 200),
    },
    adx: adx(candles, 14),
    atr14: atr(candles, 14),
    rsi14: rsi(closes, 14),
    vwap: v,
    vwapNote:
      v === null
        ? 'No VWAP: the series carries no volume. Index series frequently report zero volume — use the futures series if VWAP matters.'
        : null,
    momentum: {
      roc5: rocPoints(closes, 5),
      roc15: rocPoints(closes, 15),
      roc60: rocPoints(closes, 60),
    },
    volume: {
      lastBar: lastVol,
      avg20,
      relative: lastVol != null && avg20 != null && avg20 > 0 ? lastVol / avg20 : null,
    },
    structure: {
      currentSession: sess.length > 0 ? sess[sess.length - 1]! : null,
      previousSession: sess.length > 1 ? sess[sess.length - 2]! : null,
      openingRange15: openingRange(candles, 15),
      last20Bars: swingRange(candles, 20),
      last60Bars: swingRange(candles, 60),
    },
  };
}
