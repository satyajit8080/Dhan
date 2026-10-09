/**
 * /v2/marketfeed/{ltp,ohlc,quote}
 *
 * `quote` is the only one of the three that returns 5-level depth,
 * top-of-book quantities, OI and last_trade_time. It is the Stage-2 workhorse.
 *
 * Rate class: `quote` -> 1 request/sec. Up to 1000 instruments per request, so
 * batch aggressively rather than looping.
 */

import type { Transport } from '../transport.js';
import type { ExchangeSegment } from '../types.js';

/** { NSE_FNO: [49081, 49082], BSE_FNO: [844615] } */
export type InstrumentRequest = Partial<Record<ExchangeSegment, number[]>>;

export type RawQuoteResponse = Partial<
  Record<ExchangeSegment, Record<string, unknown>>
>;

function assertNonEmpty(req: InstrumentRequest): void {
  const total = Object.values(req).reduce((n, ids) => n + (ids?.length ?? 0), 0);
  if (total === 0) throw new Error('marketfeed request must name at least one instrument');
  if (total > 1000) {
    throw new Error(`marketfeed accepts at most 1000 instruments per call, got ${total}`);
  }
}

/** Full quote: depth, OI, volume, circuits, last_trade_time. */
export async function fetchQuote(
  transport: Transport,
  req: InstrumentRequest,
): Promise<{ data: RawQuoteResponse; receivedAtMs: number }> {
  assertNonEmpty(req);
  return transport.post<RawQuoteResponse>({
    path: '/marketfeed/quote',
    body: req,
    limitClass: 'quote',
  });
}

/** LTP only. Cheapest call; use when depth is not needed. */
export async function fetchLtp(
  transport: Transport,
  req: InstrumentRequest,
): Promise<{ data: RawQuoteResponse; receivedAtMs: number }> {
  assertNonEmpty(req);
  return transport.post<RawQuoteResponse>({
    path: '/marketfeed/ltp',
    body: req,
    limitClass: 'quote',
  });
}

/** LTP plus the day's OHLC. */
export async function fetchOhlc(
  transport: Transport,
  req: InstrumentRequest,
): Promise<{ data: RawQuoteResponse; receivedAtMs: number }> {
  assertNonEmpty(req);
  return transport.post<RawQuoteResponse>({
    path: '/marketfeed/ohlc',
    body: req,
    limitClass: 'quote',
  });
}

/** Pull one instrument's raw object out of a marketfeed response. */
export function pickInstrument(
  raw: RawQuoteResponse,
  segment: ExchangeSegment,
  securityId: number | string,
): unknown {
  return raw[segment]?.[String(securityId)];
}
