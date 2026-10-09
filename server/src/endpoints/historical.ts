/**
 * /v2/charts/historical and /v2/charts/intraday
 *
 * Rate class: `data` -> 5 requests/sec.
 *
 * Verified against the DhanHQ v2 docs:
 *   - intraday `interval` is an INTEGER enum: 1, 5, 15, 25, 60
 *   - intraday dates are "YYYY-MM-DD HH:MM:SS"; daily dates are "YYYY-MM-DD"
 *   - daily `toDate` is NON-INCLUSIVE
 *   - at most 90 days per intraday request
 *   - `timestamp` is epoch SECONDS
 *
 * This supplies the price series for trend, structure and momentum work. It is
 * never an input to the parity forward or the integrity gate — those must come
 * from the live snapshot alone.
 */

import type { Transport } from '../transport.js';
import type { ExchangeSegment } from '../types.js';
import { ValidationError } from '../errors.js';

export interface RawCandles {
  open?: number[];
  high?: number[];
  low?: number[];
  close?: number[];
  volume?: number[];
  timestamp?: number[];
  open_interest?: number[];
}

export interface Candle {
  timestampMs: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  openInterest: number | null;
}

export type InstrumentKind =
  | 'INDEX'
  | 'EQUITY'
  | 'FUTIDX'
  | 'OPTIDX'
  | 'FUTSTK'
  | 'OPTSTK'
  | 'FUTCOM'
  | 'OPTFUT';

/** The only intervals Dhan accepts. */
export const INTRADAY_INTERVALS = [1, 5, 15, 25, 60] as const;
export type IntradayInterval = (typeof INTRADAY_INTERVALS)[number];

const MAX_INTRADAY_DAYS = 90;

function assertIsoDate(v: string, field: string): void {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    throw new ValidationError(`${field} must be YYYY-MM-DD, got "${v}"`);
  }
}

/** Accepts "YYYY-MM-DD" and pads to the datetime form intraday requires. */
function toDateTime(v: string, endOfDay: boolean): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    return `${v} ${endOfDay ? '15:30:00' : '09:15:00'}`;
  }
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(v)) return v;
  throw new ValidationError(
    `Intraday dates must be "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS", got "${v}"`,
  );
}

function daysBetween(from: string, to: string): number {
  const a = Date.parse(`${from.slice(0, 10)}T00:00:00Z`);
  const b = Date.parse(`${to.slice(0, 10)}T00:00:00Z`);
  return Math.abs(b - a) / 86_400_000;
}

/**
 * Daily candles.
 *
 * NOTE: `toDate` is non-inclusive — pass the day AFTER the last one you want.
 */
export async function fetchDailyHistorical(
  transport: Transport,
  params: {
    securityId: string;
    exchangeSegment: ExchangeSegment;
    instrument: InstrumentKind;
    fromDate: string;
    toDate: string;
    expiryCode?: number;
    oi?: boolean;
  },
): Promise<{ data: RawCandles; receivedAtMs: number }> {
  assertIsoDate(params.fromDate, 'fromDate');
  assertIsoDate(params.toDate, 'toDate');

  return transport.post<RawCandles>({
    path: '/charts/historical',
    body: {
      securityId: String(params.securityId),
      exchangeSegment: params.exchangeSegment,
      instrument: params.instrument,
      expiryCode: params.expiryCode ?? 0,
      oi: params.oi ?? false,
      fromDate: params.fromDate,
      toDate: params.toDate,
    },
    limitClass: 'data',
  });
}

/** Intraday candles. At most 90 days per call. */
export async function fetchIntraday(
  transport: Transport,
  params: {
    securityId: string;
    exchangeSegment: ExchangeSegment;
    instrument: InstrumentKind;
    interval: IntradayInterval;
    fromDate: string;
    toDate: string;
    oi?: boolean;
  },
): Promise<{ data: RawCandles; receivedAtMs: number }> {
  if (!INTRADAY_INTERVALS.includes(params.interval)) {
    throw new ValidationError(
      `interval must be one of ${INTRADAY_INTERVALS.join(', ')} (minutes), got ${params.interval}`,
    );
  }

  const from = toDateTime(params.fromDate, false);
  const to = toDateTime(params.toDate, true);

  if (daysBetween(from, to) > MAX_INTRADAY_DAYS) {
    throw new ValidationError(
      `Intraday requests are limited to ${MAX_INTRADAY_DAYS} days; ` +
        `${from.slice(0, 10)} to ${to.slice(0, 10)} exceeds that.`,
    );
  }

  return transport.post<RawCandles>({
    path: '/charts/intraday',
    body: {
      securityId: String(params.securityId),
      exchangeSegment: params.exchangeSegment,
      instrument: params.instrument,
      // Integer, per the docs. Sending a string is the common mistake here.
      interval: params.interval,
      oi: params.oi ?? false,
      fromDate: from,
      toDate: to,
    },
    limitClass: 'data',
  });
}

/**
 * Dhan returns parallel arrays. Zip them, dropping any ragged tail, and sort
 * oldest-first so every indicator can assume ascending time.
 */
export function toCandles(raw: RawCandles): Candle[] {
  const ts = raw.timestamp ?? [];
  const n = Math.min(
    ts.length,
    raw.open?.length ?? 0,
    raw.high?.length ?? 0,
    raw.low?.length ?? 0,
    raw.close?.length ?? 0,
  );

  const out: Candle[] = [];
  for (let i = 0; i < n; i++) {
    const t = ts[i]!;
    if (!Number.isFinite(t)) continue;
    out.push({
      // Epoch SECONDS per the docs.
      timestampMs: t * 1000,
      open: raw.open![i]!,
      high: raw.high![i]!,
      low: raw.low![i]!,
      close: raw.close![i]!,
      volume: raw.volume?.[i] ?? null,
      openInterest: raw.open_interest?.[i] ?? null,
    });
  }
  out.sort((a, b) => a.timestampMs - b.timestampMs);
  return out;
}

/** Annualised close-to-close realised volatility, for context only. */
export function realisedVolatility(candles: Candle[], periodsPerYear = 252): number | null {
  if (candles.length < 3) return null;
  const rets: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const prev = candles[i - 1]!.close;
    const cur = candles[i]!.close;
    if (prev > 0 && cur > 0) rets.push(Math.log(cur / prev));
  }
  if (rets.length < 2) return null;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1);
  return Math.sqrt(variance * periodsPerYear);
}
