/**
 * Raw Dhan JSON -> canonical schema.
 *
 * Three jobs, in order:
 *   1. stamp provenance (fetchId + receipt epochMs) on every payload
 *   2. QUARANTINE vendor IV and vendor Greeks so no pricing code can read them
 *   3. quarantine malformed or sentinel vendor fields instead of coercing them
 *
 * NOTE ON CHAIN TIMESTAMPS: Dhan's /optionchain response carries no per-leg
 * `last_trade_time` — only /marketfeed/quote does. The chain is one atomic
 * vendor object, so the single-timestamp rule is enforced on fetchId identity
 * plus receipt-time skew rather than on a vendor clock that does not exist.
 */

import { randomUUID } from 'node:crypto';
import type {
  CanonicalChain,
  CanonicalQuote,
  ChainLeg,
  ChainStrike,
  DepthLevel,
  ExchangeSegment,
  MarketDepth,
  OHLC,
  Provenance,
  VendorQuarantined,
} from './types.js';

export function newFetchId(): string {
  return randomUUID();
}

export function makeProvenance(
  fetchId: string,
  epochMs: number,
  endpoint: string,
): Provenance {
  return { fetchId, epochMs, source: 'dhan-rest-v2', endpoint };
}

/** Dhan's "never traded" sentinel. Treated as null, not as 1980. */
const NEVER_TRADED = '01/01/1980 00:00:00';
const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;

/** Parse "DD/MM/YYYY HH:MM:SS" (IST wall clock) to epoch ms. */
export function parseLastTradeTime(raw: unknown): number | null {
  if (typeof raw !== 'string' || !raw.trim()) return null;
  const s = raw.trim();
  if (s === NEVER_TRADED) return null;
  const m = /^(\d{2})\/(\d{2})\/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})$/.exec(s);
  if (!m) return null;
  const [, d, mo, y, h, mi, sec] = m;
  return (
    Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(sec)) -
    IST_OFFSET_MS
  );
}

function num(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** A zero price/quantity in depth means "no order at this level", not "free". */
function normalizeDepthSide(raw: unknown): DepthLevel[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((lvl) => {
    const o = (lvl ?? {}) as Record<string, unknown>;
    return {
      price: num(o['price']) ?? 0,
      quantity: num(o['quantity']) ?? 0,
      orders: num(o['orders']) ?? 0,
    };
  });
}

function normalizeOhlc(raw: unknown): OHLC | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const open = num(o['open']);
  const high = num(o['high']);
  const low = num(o['low']);
  const close = num(o['close']);
  if (open === null && high === null && low === null && close === null) return null;
  return { open: open ?? 0, high: high ?? 0, low: low ?? 0, close: close ?? 0 };
}

/**
 * Normalize one instrument out of a /marketfeed/quote response.
 *
 * @param raw       the per-security object from data[segment][securityId]
 */
export function normalizeQuote(
  raw: unknown,
  securityId: string,
  segment: ExchangeSegment,
  fetchId: string,
  receivedAtMs: number,
  endpoint: string,
): CanonicalQuote {
  const o = (raw ?? {}) as Record<string, unknown>;
  const quarantined: Record<string, unknown> = {};

  const ltp = num(o['last_price']);
  if (ltp === null) quarantined['last_price'] = o['last_price'];

  const rawLtt = o['last_trade_time'];
  const lastTradeTimeMs = parseLastTradeTime(rawLtt);
  if (rawLtt !== undefined && lastTradeTimeMs === null) {
    quarantined['last_trade_time'] = rawLtt;
  }

  let depth: MarketDepth | null = null;
  const rawDepth = o['depth'] as Record<string, unknown> | undefined;
  if (rawDepth && typeof rawDepth === 'object') {
    depth = {
      buy: normalizeDepthSide(rawDepth['buy']),
      sell: normalizeDepthSide(rawDepth['sell']),
    };
  }

  return {
    securityId,
    segment,
    ltp: ltp ?? NaN,
    ohlc: normalizeOhlc(o['ohlc']),
    volume: num(o['volume']),
    oi: num(o['oi']),
    oiDayHigh: num(o['oi_day_high']),
    oiDayLow: num(o['oi_day_low']),
    averagePrice: num(o['average_price']),
    buyQuantity: num(o['buy_quantity']),
    sellQuantity: num(o['sell_quantity']),
    netChange: num(o['net_change']),
    upperCircuit: num(o['upper_circuit_limit']),
    lowerCircuit: num(o['lower_circuit_limit']),
    lastTradeTimeMs,
    depth,
    provenance: makeProvenance(fetchId, receivedAtMs, endpoint),
    quarantined,
  };
}

/**
 * Pull vendor analytics OUT of the usable surface and into quarantine.
 *
 * This is the structural enforcement of "never use vendor IV or vendor Greeks".
 */
function quarantineVendor(o: Record<string, unknown>): VendorQuarantined {
  const iv = num(o['implied_volatility']);
  const g = (o['greeks'] ?? null) as Record<string, unknown> | null;

  const reasons: string[] = [
    'Vendor analytics are computed against the index LTP, not the forward.',
  ];
  if (iv === null) reasons.push('IV missing or unparseable.');
  else if (iv <= 0) reasons.push('IV <= 0.');
  else if (iv > 60) reasons.push('IV > 60% — implausible for an index option.');

  return {
    impliedVolatility: iv,
    greeks: g
      ? {
          delta: num(g['delta']),
          gamma: num(g['gamma']),
          theta: num(g['theta']),
          vega: num(g['vega']),
        }
      : null,
    reason: reasons.join(' '),
  };
}

function normalizeLeg(raw: unknown): ChainLeg | null {
  if (!raw || typeof raw !== 'object') return null;
  const o = raw as Record<string, unknown>;
  const sid = o['security_id'];

  return {
    securityId: sid === undefined || sid === null ? null : String(sid),
    lastPrice: num(o['last_price']),
    oi: num(o['oi']),
    previousOi: num(o['previous_oi']),
    volume: num(o['volume']),
    previousVolume: num(o['previous_volume']),
    previousClosePrice: num(o['previous_close_price']),
    averagePrice: num(o['average_price']),
    topBidPrice: num(o['top_bid_price']),
    topBidQuantity: num(o['top_bid_quantity']),
    topAskPrice: num(o['top_ask_price']),
    topAskQuantity: num(o['top_ask_quantity']),
    vendorQuarantined: quarantineVendor(o),
  };
}

/** Normalize a /optionchain response. */
export function normalizeChain(
  raw: unknown,
  params: {
    underlying: string;
    underlyingScrip: number;
    underlyingSeg: ExchangeSegment;
    expiry: string;
  },
  fetchId: string,
  receivedAtMs: number,
  endpoint: string,
): CanonicalChain {
  const o = (raw ?? {}) as Record<string, unknown>;
  const quarantined: Record<string, unknown> = {};

  const oc = (o['oc'] ?? {}) as Record<string, unknown>;
  const strikes: ChainStrike[] = [];

  for (const [key, value] of Object.entries(oc)) {
    const strike = Number(key);
    if (!Number.isFinite(strike)) {
      quarantined[`oc.${key}`] = 'unparseable strike key';
      continue;
    }
    const v = (value ?? {}) as Record<string, unknown>;
    strikes.push({
      strike,
      ce: normalizeLeg(v['ce']),
      pe: normalizeLeg(v['pe']),
    });
  }

  strikes.sort((a, b) => a.strike - b.strike);

  return {
    underlying: params.underlying,
    underlyingScrip: params.underlyingScrip,
    underlyingSeg: params.underlyingSeg,
    expiry: params.expiry,
    underlyingLtpDoNotUseAsSpot: num(o['last_price']),
    strikes,
    provenance: makeProvenance(fetchId, receivedAtMs, endpoint),
    quarantined,
  };
}
