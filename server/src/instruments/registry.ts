/**
 * Underlying and futures registry.
 *
 * IMPORTANT DISTINCTION, stated once so it is never confused again:
 *   - Using securityId 51 / IDX_I to IDENTIFY the SENSEX underlying when
 *     requesting an option chain is correct and required by the API.
 *   - Using that instrument's LTP as the SPOT PRICE for option maths is
 *     forbidden. It was 321 points below the true forward on 18-Sep-2026.
 * The first is an identifier. The second is a price. Only the second is banned.
 */

import type { ExchangeSegment } from '../types.js';
import type { Logger } from '../config.js';
import { InstrumentError } from '../errors.js';

export interface UnderlyingSpec {
  name: string;
  /** UnderlyingScrip for the option-chain request. */
  scrip: number;
  /** UnderlyingSeg for the option-chain request. */
  segment: ExchangeSegment;
  /** Segment the option and futures contracts trade in. */
  derivativeSegment: ExchangeSegment;
  lotSize: number;
  strikeStep: number;
  /**
   * False when the securityId has NOT been confirmed against the instrument
   * master. Every use logs a warning until it is.
   */
  verified: boolean;
  verificationNote: string;
}

export interface FuturesSpec {
  underlying: string;
  /** Contract month label, e.g. "SEP". */
  month: string;
  securityId: number;
  segment: ExchangeSegment;
  /** Contract expiry, YYYY-MM-DD (last Thursday of the month, 15:30 IST). */
  expiry: string;
  verified: boolean;
}

/** Today's date in IST, YYYY-MM-DD. */
function todayIst(): string {
  return new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
}

const UNDERLYINGS: Record<string, UnderlyingSpec> = {
  SENSEX: {
    name: 'SENSEX',
    scrip: 51,
    segment: 'IDX_I',
    derivativeSegment: 'BSE_FNO',
    lotSize: 20,
    strikeStep: 100,
    verified: true,
    verificationNote:
      'securityId 51 / IDX_I confirmed. Lot size 20 confirmed via margin call: ' +
      'Rs9,231 = 461.55 x 20.',
  },
  NIFTY: {
    name: 'NIFTY',
    scrip: 13,
    segment: 'IDX_I',
    derivativeSegment: 'NSE_FNO',
    lotSize: 75,
    strikeStep: 50,
    verified: false,
    verificationNote:
      'UNVERIFIED: securityId 13 and lot size have NOT been confirmed against the ' +
      'instrument master. Verify before trusting any output for this underlying.',
  },
  BANKNIFTY: {
    name: 'BANKNIFTY',
    scrip: 25,
    segment: 'IDX_I',
    derivativeSegment: 'NSE_FNO',
    lotSize: 30,
    strikeStep: 100,
    verified: false,
    verificationNote:
      'UNVERIFIED: securityId 25 and lot size have NOT been confirmed against the ' +
      'instrument master. Verify before trusting any output for this underlying.',
  },
};

/** SENSEX futures, for the parity cross-check. Verified. */
const FUTURES: FuturesSpec[] = [
  { underlying: 'SENSEX', month: 'SEP', securityId: 844615, segment: 'BSE_FNO', expiry: '2026-09-24', verified: true },
  { underlying: 'SENSEX', month: 'OCT', securityId: 864571, segment: 'BSE_FNO', expiry: '2026-10-29', verified: true },
  { underlying: 'SENSEX', month: 'NOV', securityId: 1100929, segment: 'BSE_FNO', expiry: '2026-11-26', verified: true },
];

export class InstrumentRegistry {
  constructor(private readonly log: Logger) {}

  underlying(name: string): UnderlyingSpec {
    const key = name.trim().toUpperCase();
    const spec = UNDERLYINGS[key];
    if (!spec) {
      throw new InstrumentError(
        `Unknown underlying "${name}". Known: ${Object.keys(UNDERLYINGS).join(', ')}.`,
        { requested: name, known: Object.keys(UNDERLYINGS) },
      );
    }
    if (!spec.verified) {
      this.log.warn(`UNVERIFIED INSTRUMENT: ${spec.name}`, {
        scrip: spec.scrip,
        note: spec.verificationNote,
      });
    }
    return spec;
  }

  list(): UnderlyingSpec[] {
    return Object.values(UNDERLYINGS);
  }

  futures(underlying: string, month?: string): FuturesSpec {
    const key = underlying.trim().toUpperCase();
    const all = FUTURES.filter((f) => f.underlying === key);
    if (all.length === 0) {
      throw new InstrumentError(`No futures registered for "${underlying}".`, {
        requested: underlying,
      });
    }
    if (!month) {
      // Front month = nearest contract that has not expired. all[0] used to be
      // returned blindly, which served the dead SEP contract after 24-Sep.
      const today = todayIst();
      const live = [...all]
        .filter((f) => f.expiry >= today)
        .sort((a, b) => a.expiry.localeCompare(b.expiry));
      if (live.length === 0) {
        throw new InstrumentError(
          `Every registered ${key} future has expired. Add the next contract to the registry.`,
          { registered: all.map((f) => `${f.month} ${f.expiry}`) },
        );
      }
      return live[0]!;
    }

    const m = month.trim().toUpperCase().slice(0, 3);
    const hit = all.find((f) => f.month === m);
    if (!hit) {
      throw new InstrumentError(
        `No ${key} future for month "${month}". Available: ${all.map((f) => f.month).join(', ')}.`,
        { requested: month, available: all.map((f) => f.month) },
      );
    }
    return hit;
  }

  futuresList(underlying: string): FuturesSpec[] {
    return FUTURES.filter((f) => f.underlying === underlying.trim().toUpperCase());
  }

  /**
   * Choose the futures contract that best cross-checks a given option expiry:
   * the nearest contract month that has not already passed the expiry.
   */
  futuresForExpiry(underlying: string, expiry: string): FuturesSpec {
    const all = this.futuresList(underlying);
    if (all.length === 0) {
      throw new InstrumentError(`No futures registered for "${underlying}".`);
    }
    // Nearest contract expiring on or after the option expiry.
    const sorted = [...all].sort((a, b) => a.expiry.localeCompare(b.expiry));
    const hit = sorted.find((f) => f.expiry >= expiry);
    if (!hit) {
      throw new InstrumentError(
        `No registered ${underlying} future expires on or after ${expiry}. Add the next contract.`,
        { registered: sorted.map((f) => `${f.month} ${f.expiry}`) },
      );
    }
    return hit;
  }
}
