/**
 * Rolling price-action history built from successful market snapshots.
 *
 * WHY THIS EXISTS: the candle endpoint is a single point of failure. When it is
 * down, the plugin still receives a live index level, a forward and a futures
 * quote on every scan. Throwing that away and reporting "breakout unavailable"
 * discards usable price action.
 *
 * Observations are bucketed into synthetic candles and fed to the SAME
 * `deriveLevels` engine the real candles use, so the level logic is identical
 * and only the input source differs.
 *
 * HONEST LIMITATION, recorded on every result: a synthetic bar built from one
 * observation per scan is coarser than a true 1-minute candle. Touch counts are
 * lower and intrabar extremes are missed. The result carries `quality` so the
 * caller always knows which source produced the level.
 *
 * Persisted to disk because an MCP server restarts with the chat session; in
 * memory alone, the first scan of every session would start blind.
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Candle } from './endpoints/historical.js';

export interface Observation {
  /** Epoch ms at which the snapshot was received. */
  t: number;
  /** Index LTP. Used for level placement only, never as option spot. */
  index: number | null;
  /** Parity forward. */
  forward: number | null;
  /** Futures LTP. */
  futures: number | null;
  /** Futures day high, when the quote carried OHLC. */
  dayHigh: number | null;
  /** Futures day low. */
  dayLow: number | null;
}

/** IST calendar day of an epoch-ms instant. */
function istDay(epochMs: number): string {
  return new Date(epochMs + 5.5 * 3600_000).toISOString().slice(0, 10);
}

/** futures - index on one observation; null when either is missing. */
function basisOf(o: Observation): number | null {
  if (o.index === null || o.futures === null) return null;
  if (!Number.isFinite(o.index) || !Number.isFinite(o.futures)) return null;
  return o.futures - o.index;
}

export type LevelSourceQuality = 'candles' | 'rolling_snapshots' | 'reference_only' | 'none';

const DEFAULT_WINDOW_MINUTES = 90;
const MAX_ENTRIES = 5000;

function defaultStorePath(): string {
  return process.env['DHAN_HISTORY_FILE'] ?? join(tmpdir(), 'bull50-dhan-history.json');
}

export class PriceHistory {
  private observations: Observation[] = [];
  private readonly path: string;
  private loaded = false;

  constructor(
    private readonly windowMinutes: number = DEFAULT_WINDOW_MINUTES,
    path?: string,
  ) {
    this.path = path ?? defaultStorePath();
  }

  /** Load once, lazily. A missing or corrupt file is not an error. */
  private ensureLoaded(): void {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const raw = readFileSync(this.path, 'utf8');
      const parsed = JSON.parse(raw) as { observations?: Observation[] };
      if (Array.isArray(parsed.observations)) {
        this.observations = parsed.observations.filter(
          (o) => typeof o?.t === 'number' && Number.isFinite(o.t),
        );
      }
    } catch {
      this.observations = [];
    }
    this.prune();
  }

  private persist(): void {
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      writeFileSync(
        this.path,
        JSON.stringify({ observations: this.observations }),
        'utf8',
      );
    } catch {
      // Persistence is an optimisation. Losing it must never fail a scan.
    }
  }

  private prune(): void {
    const cutoff = Date.now() - this.windowMinutes * 60_000;
    this.observations = this.observations.filter((o) => o.t >= cutoff);
    if (this.observations.length > MAX_ENTRIES) {
      this.observations = this.observations.slice(-MAX_ENTRIES);
    }
  }

  /** Record one scan. Ignores an observation with no usable price. */
  record(o: Observation): void {
    this.ensureLoaded();
    if (o.index === null && o.forward === null && o.futures === null) return;
    this.observations.push(o);
    this.prune();
    this.persist();
  }

  all(): Observation[] {
    this.ensureLoaded();
    return [...this.observations];
  }

  count(): number {
    this.ensureLoaded();
    return this.observations.length;
  }

  spanMinutes(): number {
    this.ensureLoaded();
    if (this.observations.length < 2) return 0;
    const first = this.observations[0]!.t;
    const last = this.observations[this.observations.length - 1]!.t;
    return (last - first) / 60_000;
  }

  clear(): void {
    this.observations = [];
    this.loaded = true;
    this.persist();
  }

  /**
   * Bucket observations into synthetic candles on the index series.
   *
   * Each bucket's open is the first observation, close the last, high/low the
   * extremes actually seen. Volume is null — snapshots carry no traded volume,
   * and reporting zero would let a VWAP be computed from nothing.
   *
   * The futures day high/low can widen a bar, under two conditions:
   *   1. It moved since the previous observation of the same day. The day
   *      high is the SESSION's extreme; only a NEW extreme tells us price
   *      went there between this scan and the last one. Applying an
   *      unchanged day high to every bar stamped the session high onto every
   *      bar and manufactured a "confirmed" level with one touch per bar.
   *   2. It can be moved into index space. The day range comes from the
   *      futures quote and sits a basis (futures - index) away from the index.
   *      Without both prices on the observation it is not used.
   */
  toSyntheticCandles(bucketMinutes = 1): Candle[] {
    this.ensureLoaded();
    const usable = this.observations
      .filter((o) => o.index !== null && Number.isFinite(o.index))
      .sort((a, b) => a.t - b.t);
    if (usable.length === 0) return [];

    // Index-space high/low each observation newly revealed, if any.
    const extensions = new Map<Observation, { high: number | null; low: number | null }>();
    let prev: Observation | null = null;
    for (const o of usable) {
      const sameDay = prev !== null && istDay(prev.t) === istDay(o.t);
      const basis = basisOf(o);
      let high: number | null = null;
      let low: number | null = null;
      if (sameDay && basis !== null) {
        if (o.dayHigh !== null && prev!.dayHigh !== null && o.dayHigh > prev!.dayHigh) {
          high = o.dayHigh - basis;
        }
        if (o.dayLow !== null && prev!.dayLow !== null && o.dayLow < prev!.dayLow) {
          low = o.dayLow - basis;
        }
      }
      extensions.set(o, { high, low });
      prev = o;
    }

    const bucketMs = Math.max(1, bucketMinutes) * 60_000;
    const out: Candle[] = [];
    let bucket: Observation[] = [];
    let bucketStart = Math.floor(usable[0]!.t / bucketMs) * bucketMs;

    const flush = () => {
      if (bucket.length === 0) return;
      const prices = bucket.map((o) => o.index!);
      let high = Math.max(...prices);
      let low = Math.min(...prices);

      for (const o of bucket) {
        const ext = extensions.get(o)!;
        if (ext.high !== null && ext.high > high && ext.high < high * 1.02) high = ext.high;
        if (ext.low !== null && ext.low < low && ext.low > low * 0.98) low = ext.low;
      }

      out.push({
        timestampMs: bucket[0]!.t,
        open: prices[0]!,
        high,
        low,
        close: prices[prices.length - 1]!,
        volume: null,
        openInterest: null,
      });
      bucket = [];
    };

    for (const o of usable) {
      const b = Math.floor(o.t / bucketMs) * bucketMs;
      if (b !== bucketStart) {
        flush();
        bucketStart = b;
      }
      bucket.push(o);
    }
    flush();
    return out;
  }

  /**
   * Reference levels available even on a first scan, from the futures day
   * range. These are real exchange values, not derived from history.
   *
   * Returned in INDEX space (futures day range minus the basis observed on the
   * same scan), because they are compared against the index LTP. Observations
   * without both an index and a futures price cannot be converted and are
   * skipped. Only the most recent IST day is considered.
   */
  referenceLevels(): { dayHigh: number | null; dayLow: number | null; observations: number } {
    this.ensureLoaded();
    let dayHigh: number | null = null;
    let dayLow: number | null = null;
    const last = this.observations[this.observations.length - 1];
    const today = last ? istDay(last.t) : null;
    for (const o of this.observations) {
      if (istDay(o.t) !== today) continue;
      const basis = basisOf(o);
      if (basis === null) continue;
      if (o.dayHigh !== null) {
        const h = o.dayHigh - basis;
        if (dayHigh === null || h > dayHigh) dayHigh = h;
      }
      if (o.dayLow !== null) {
        const l = o.dayLow - basis;
        if (dayLow === null || l < dayLow) dayLow = l;
      }
    }
    return { dayHigh, dayLow, observations: this.observations.length };
  }

  /** Enough history to attempt a level derivation? */
  sufficientForLevels(minObservations = 6, minSpanMinutes = 5): boolean {
    this.ensureLoaded();
    return this.count() >= minObservations && this.spanMinutes() >= minSpanMinutes;
  }
}
