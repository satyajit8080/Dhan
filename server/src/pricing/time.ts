/**
 * Time to expiry, ACT/365, measured to the 15:30 IST expiry stamp.
 *
 * PURE MODULE: no clock of its own. The caller supplies "now" so that a
 * snapshot's Greeks are computed against the snapshot's own timestamp and not
 * against wall-clock time at the moment some later code happened to run.
 */

/** IST is UTC+05:30, with no daylight saving, ever. */
const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;
const EXPIRY_HOUR_IST = 15;
const EXPIRY_MINUTE_IST = 30;
const DAY_MS = 86_400_000;
const YEAR_DAYS = 365;

/** Epoch ms of 15:30 IST on the given YYYY-MM-DD. */
export function expiryStampMs(expiryDate: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(expiryDate.trim());
  if (!m) throw new Error(`Expiry must be YYYY-MM-DD, got: ${expiryDate}`);
  const [, y, mo, d] = m;
  const utcMidnight = Date.UTC(Number(y), Number(mo) - 1, Number(d));
  return (
    utcMidnight +
    EXPIRY_HOUR_IST * 3_600_000 +
    EXPIRY_MINUTE_IST * 60_000 -
    IST_OFFSET_MS
  );
}

/** ACT/365 year fraction from `nowMs` to the 15:30 IST stamp on `expiryDate`. */
export function yearFractionToExpiry(nowMs: number, expiryDate: string): number {
  return (expiryStampMs(expiryDate) - nowMs) / (YEAR_DAYS * DAY_MS);
}

/** Calendar days to expiry, unrounded. */
export function daysToExpiry(nowMs: number, expiryDate: string): number {
  return (expiryStampMs(expiryDate) - nowMs) / DAY_MS;
}

/** Parse an IST wall-clock instant, e.g. "2026-09-18T15:30:00", to epoch ms. */
export function istToEpochMs(istIsoNoZone: string): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(
    istIsoNoZone.trim(),
  );
  if (!m) throw new Error(`Expected YYYY-MM-DDTHH:MM[:SS] (IST), got: ${istIsoNoZone}`);
  const [, y, mo, d, h, mi, s] = m;
  return (
    Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s ?? 0)) -
    IST_OFFSET_MS
  );
}
