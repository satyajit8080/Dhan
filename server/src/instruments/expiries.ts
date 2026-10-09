/**
 * Expiry list with a TTL cache.
 *
 * NEVER hardcode an expiry date. Weekly expiries move on exchange holidays,
 * and a hardcoded date silently prices the wrong contract rather than failing.
 */

import { fetchExpiryList } from '../endpoints/optionChain.js';
import type { Transport } from '../transport.js';
import type { Logger } from '../config.js';
import type { ExchangeSegment } from '../types.js';
import { InstrumentError } from '../errors.js';

interface CacheEntry {
  expiries: string[];
  fetchedAtMs: number;
}

export class ExpiryCache {
  private readonly cache = new Map<string, CacheEntry>();

  constructor(
    private readonly transport: Transport,
    private readonly ttlMs: number,
    private readonly log: Logger,
  ) {}

  private key(scrip: number, segment: ExchangeSegment): string {
    return `${segment}:${scrip}`;
  }

  async get(
    scrip: number,
    segment: ExchangeSegment,
    opts: { force?: boolean } = {},
  ): Promise<{ expiries: string[]; cached: boolean; fetchedAtMs: number }> {
    const key = this.key(scrip, segment);
    const hit = this.cache.get(key);
    const fresh = hit && Date.now() - hit.fetchedAtMs < this.ttlMs;

    if (fresh && !opts.force) {
      return { expiries: hit.expiries, cached: true, fetchedAtMs: hit.fetchedAtMs };
    }

    const { data, receivedAtMs } = await fetchExpiryList(this.transport, { scrip, segment });
    const expiries = (Array.isArray(data) ? data : [])
      .filter((d): d is string => typeof d === 'string')
      .sort();

    if (expiries.length === 0) {
      throw new InstrumentError(
        `Dhan returned no expiries for ${segment}:${scrip}. Cannot proceed without one.`,
        { scrip, segment },
      );
    }

    this.cache.set(key, { expiries, fetchedAtMs: receivedAtMs });
    this.log.debug('Expiry list refreshed', { key, count: expiries.length });
    return { expiries, cached: false, fetchedAtMs: receivedAtMs };
  }

  /** Nearest expiry on or after `fromDate` (YYYY-MM-DD). */
  async nearest(
    scrip: number,
    segment: ExchangeSegment,
    fromDate: string,
  ): Promise<string> {
    const { expiries } = await this.get(scrip, segment);
    const hit = expiries.find((e) => e >= fromDate);
    if (!hit) {
      throw new InstrumentError(
        `No expiry on or after ${fromDate} for ${segment}:${scrip}. ` +
          `Available: ${expiries.join(', ')}.`,
        { fromDate, expiries },
      );
    }
    return hit;
  }

  /** Validate a user-supplied expiry against the live list. */
  async assertValid(
    scrip: number,
    segment: ExchangeSegment,
    expiry: string,
  ): Promise<void> {
    const { expiries } = await this.get(scrip, segment);
    if (!expiries.includes(expiry)) {
      throw new InstrumentError(
        `Expiry ${expiry} is not listed for ${segment}:${scrip}. ` +
          `Available: ${expiries.slice(0, 8).join(', ')}${expiries.length > 8 ? ', ...' : ''}.`,
        { requested: expiry, available: expiries },
      );
    }
  }
}
