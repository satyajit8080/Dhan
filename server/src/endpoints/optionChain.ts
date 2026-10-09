/**
 * /v2/optionchain and /v2/optionchain/expirylist
 *
 * Rate class: `optionchain` -> 1 unique request per 3 seconds. The limiter
 * keys on underlying+expiry so two different expiries do not serialise
 * unnecessarily while two calls for the SAME key still space out.
 *
 * Each leg of the response carries its own `security_id`. That is what makes
 * Stage-2 depth possible without an instrument search: screen from the chain,
 * then fetch depth for the survivors by the id the chain already gave us.
 */

import type { Transport } from '../transport.js';
import type { ExchangeSegment } from '../types.js';

export interface RawOptionChain {
  last_price?: number;
  oc?: Record<string, unknown>;
}

export async function fetchOptionChain(
  transport: Transport,
  params: { scrip: number; segment: ExchangeSegment; expiry: string },
): Promise<{ data: RawOptionChain; receivedAtMs: number }> {
  return transport.post<RawOptionChain>({
    path: '/optionchain',
    body: {
      UnderlyingScrip: params.scrip,
      UnderlyingSeg: params.segment,
      Expiry: params.expiry,
    },
    limitClass: 'optionchain',
    uniqueKey: `${params.segment}:${params.scrip}:${params.expiry}`,
  });
}

export async function fetchExpiryList(
  transport: Transport,
  params: { scrip: number; segment: ExchangeSegment },
): Promise<{ data: string[]; receivedAtMs: number }> {
  return transport.post<string[]>({
    path: '/optionchain/expirylist',
    body: {
      UnderlyingScrip: params.scrip,
      UnderlyingSeg: params.segment,
    },
    limitClass: 'optionchain',
    uniqueKey: `expirylist:${params.segment}:${params.scrip}`,
  });
}
