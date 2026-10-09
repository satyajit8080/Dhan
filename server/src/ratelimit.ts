/**
 * Token buckets, one per limit class. Never burst.
 *
 * Published Dhan limits:
 *   non-trading  20 / sec
 *   data          5 / sec
 *   quote         1 / sec
 *   optionchain   1 unique underlying / 3 sec
 *
 * Callers await a permit BEFORE the request goes out, so we shape traffic at
 * the source instead of discovering the limit from a 429 and retrying into it.
 */

import { DailyQuotaError } from './errors.js';
import type { Logger } from './config.js';

export type LimitClass = 'nontrading' | 'data' | 'quote' | 'optionchain';

interface BucketSpec {
  capacity: number;
  refillPerSec: number;
}

const SPECS: Record<LimitClass, BucketSpec> = {
  nontrading: { capacity: 20, refillPerSec: 20 },
  data: { capacity: 5, refillPerSec: 5 },
  // Capacity 1: a burst of two quote calls in the same second is exactly what
  // the 1/sec limit forbids, so the bucket must not be able to hold two.
  quote: { capacity: 1, refillPerSec: 1 },
  optionchain: { capacity: 1, refillPerSec: 1 / 3 },
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, Math.max(0, ms)));

class TokenBucket {
  private tokens: number;
  private lastRefillMs: number;
  /** Serialises waiters so they drain in arrival order. */
  private chain: Promise<void> = Promise.resolve();

  constructor(private readonly spec: BucketSpec) {
    this.tokens = spec.capacity;
    this.lastRefillMs = Date.now();
  }

  private refill(): void {
    const now = Date.now();
    const elapsedSec = (now - this.lastRefillMs) / 1000;
    if (elapsedSec <= 0) return;
    this.tokens = Math.min(this.spec.capacity, this.tokens + elapsedSec * this.spec.refillPerSec);
    this.lastRefillMs = now;
  }

  /** Milliseconds until one token is available. 0 when immediate. */
  private waitMs(): number {
    this.refill();
    if (this.tokens >= 1) return 0;
    return ((1 - this.tokens) / this.spec.refillPerSec) * 1000;
  }

  acquire(): Promise<void> {
    const run = async () => {
      for (;;) {
        const wait = this.waitMs();
        if (wait <= 0) {
          this.tokens -= 1;
          return;
        }
        await sleep(wait);
      }
    };
    const next = this.chain.then(run, run);
    // Keep the chain alive even if a waiter rejects.
    this.chain = next.then(
      () => undefined,
      () => undefined,
    );
    return next;
  }
}

export class RateLimiter {
  private readonly buckets = new Map<LimitClass, TokenBucket>();
  /** For optionchain: last request time per unique key. */
  private readonly lastByKey = new Map<string, number>();
  private dayStamp: string;
  private used = 0;

  constructor(
    private readonly dailyQuota: number,
    private readonly log: Logger,
  ) {
    for (const [k, spec] of Object.entries(SPECS)) {
      this.buckets.set(k as LimitClass, new TokenBucket(spec));
    }
    this.dayStamp = RateLimiter.today();
    this.log.info('Rate limiter armed', {
      dailyQuota,
      note: 'Brief specifies 7000/day; Dhan publishes a higher figure. Conservative default in force.',
    });
  }

  private static today(): string {
    // IST day boundary: Dhan quotas reset on the trading day.
    return new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
  }

  private tickDaily(): void {
    const today = RateLimiter.today();
    if (today !== this.dayStamp) {
      this.dayStamp = today;
      this.used = 0;
      this.log.info('Daily request budget reset', { day: today });
    }
    if (this.used >= this.dailyQuota) {
      throw new DailyQuotaError(this.used, this.dailyQuota);
    }
    this.used += 1;
  }

  /**
   * Await a permit.
   *
   * @param cls        which published limit applies
   * @param uniqueKey  for `optionchain`, the underlying (or underlying+expiry);
   *                   enforces the "1 unique request / 3 sec" rule per key
   */
  async acquire(cls: LimitClass, uniqueKey?: string): Promise<void> {
    this.tickDaily();

    if (cls === 'optionchain' && uniqueKey) {
      const last = this.lastByKey.get(uniqueKey);
      if (last !== undefined) {
        const since = Date.now() - last;
        if (since < 3000) await sleep(3000 - since);
      }
    }

    await this.buckets.get(cls)!.acquire();

    if (cls === 'optionchain' && uniqueKey) this.lastByKey.set(uniqueKey, Date.now());
  }

  stats(): { day: string; used: number; quota: number; remaining: number } {
    return {
      day: this.dayStamp,
      used: this.used,
      quota: this.dailyQuota,
      remaining: Math.max(0, this.dailyQuota - this.used),
    };
  }
}

/** Exponential backoff with full jitter, so retries never synchronise. */
export function backoffWithJitter(attempt: number, baseMs = 500, capMs = 20_000): number {
  const exp = Math.min(capMs, baseMs * 2 ** attempt);
  return Math.random() * exp;
}
