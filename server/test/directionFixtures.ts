import type { Candle } from '../src/endpoints/historical.js';

/** 09:15 IST on 29 Oct 2026, in epoch ms. */
export const OPEN_MS = Date.UTC(2026, 9, 29, 3, 45);
/** 13:00 IST the previous day. */
const PREV_MS = Date.UTC(2026, 9, 28, 7, 30);
export const MIN = 60_000;

/** A 1-minute bar `i` minutes after today's open. */
export function bar(
  i: number,
  o: number,
  h: number,
  l: number,
  c: number,
  volume: number | null = 1000,
): Candle {
  return { timestampMs: OPEN_MS + i * MIN, open: o, high: h, low: l, close: c, volume, openInterest: null };
}

/**
 * Previous afternoon: a steady 150-bar uptrend from 74575 to ~74950, enough
 * history for a 5-minute ADX. Today from 09:15: a 20-bar base under 75000 with
 * the high tagged repeatedly, then whatever `tail` adds (tail bar i = 20 + k).
 */
export function trendThenBase(tail: Candle[] = []): Candle[] {
  const out: Candle[] = [];
  let p = 74575;
  for (let i = 0; i < 150; i++) {
    const o = p;
    const c = p + 2.5;
    out.push({ timestampMs: PREV_MS + i * MIN, open: o, high: c + 1, low: o - 0.5, close: c, volume: 1000, openInterest: null });
    p = c;
  }
  for (let i = 0; i < 20; i++) {
    const up = i % 2 === 0;
    const o = up ? 74972 : 74992;
    const c = up ? 74992 : 74972;
    out.push(bar(i, o, i % 4 === 0 ? 75000 : 74996, 74966, c));
  }
  return [...out, ...tail];
}

/** Fully bullish OI: put writing below, call unwinding above, PCR rising. */
export const BULLISH_OI = {
  basis: 'intraday' as const,
  minutesBack: 20,
  strikes: [
    { strike: 74900, ceOiChange: 0, peOiChange: 5000 },
    { strike: 75000, ceOiChange: -100, peOiChange: 2000 },
    { strike: 75100, ceOiChange: -50, peOiChange: 0 },
  ],
  pcrNow: 1.1,
  pcrPast: 1.0,
};
