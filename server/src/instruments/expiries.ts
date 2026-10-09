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

/** A real calendar date in YYYY-MM-DD form (rejects 2026-02-30, "N/A", 15-10-2026). */
export function isIsoDate(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
  const d = new Date(`${v}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v;
}

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
    const raw = Array.isArray(data) ? data : [];
    // Only real YYYY-MM-DD dates. Expiries are compared as strings, and a
    // malformed entry such as "N/A" sorts AFTER every date — it was being
    // returned as the "nearest" expiry once the real dates had passed.
    const expiries = raw.filter(isIsoDate).sort();
    if (expiries.length !== raw.length) {
      this.log.warn('Expiry list contained malformed entries; ignored', {
        key,
        ignored: raw.length - expiries.length,
      });
    }

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

  /**
   * Nearest expiry STRICTLY after `today` (YYYY-MM-DD).
   *
   * The scalping scope (RULES.md §6) is "nearest weekly expiry, next weekly on
   * expiry day". `nearest()` returns today's contract on expiry day, which
   * after 15:30 IST is already expired and cannot be priced (T <= 0).
   */
  async nextAfter(
    scrip: number,
    segment: ExchangeSegment,
    today: string,
  ): Promise<string> {
    const { expiries } = await this.get(scrip, segment);
    const hit = expiries.find((e) => e > today);
    if (!hit) {
      throw new InstrumentError(
        `No expiry after ${today} for ${segment}:${scrip}. Available: ${expiries.join(', ')}.`,
        { today, expiries },
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
