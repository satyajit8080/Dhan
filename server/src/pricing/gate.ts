/**
 * The data-integrity gate.
 *
 * This gate exists because a wrong forward is worse than no forward. A 321-point
 * carry error is invisible in the output — the Greeks still look plausible —
 * but it moves delta by 16.6%, theta by ~40%, and roughly Rs196/lot on a
 * 100-point move. So when the inputs disagree, this module publishes NOTHING
 * and says why.
 *
 * There is deliberately no fallback path to the index LTP. Falling back to the
 * number that caused the defect would convert a loud failure into a silent one.
 *
 * PURE MODULE: no I/O, no clock, no config.
 */

export const GATE_DEFAULTS = {
  /** Max |parity forward - listed future|, in index points. */
  maxFutureDivergence: 75,
  /** Max dispersion of per-strike parity forwards, in index points. */
  maxPerStrikeSpread: 40,
  /** Index divergence above this raises a warning (never a block). */
  indexDivergenceWarn: 50,
  /**
   * When the future expires on a DIFFERENT date from the options (e.g. the
   * weekly expires 01-Oct, the monthly future 29-Oct), a fixed point band is
   * meaningless: the two forwards legitimately differ by the carry between the
   * two dates. The check becomes: implied annualised carry between the parity
   * forward and the future must lie inside this band.
   */
  minCarryAnnual: -0.05,
  maxCarryAnnual: 0.15,
  /** Gap (years) below which the two expiries count as the same date. */
  sameExpiryGapYears: 1.5 / 365,
} as const;

export type GateSeverity = 'block' | 'warn';

export interface GateFinding {
  severity: GateSeverity;
  code: string;
  message: string;
  observed: number | null;
  threshold: number | null;
}

export interface GateInput {
  parityForward: number;
  /** Listed futures LTP for the cross-check. Null when unavailable. */
  listedFuture: number | null;
  perStrikeSpread: number;
  /** Index LTP, used ONLY to quantify and warn about the carry error. */
  indexLtp?: number | null;
  slopeWithinTolerance?: boolean;
  slopeRelativeError?: number;
  /**
   * Years between the option expiry and the future's expiry (future minus
   * option). Omitted or ~0 means same-expiry and the strict point band applies.
   */
  futureExpiryGapYears?: number | null;
  thresholds?: Partial<typeof GATE_DEFAULTS>;
}

export interface GateResult {
  /** When true, callers MUST NOT publish pricing. */
  blocked: boolean;
  findings: GateFinding[];
  /** Human-readable block reasons. Empty when not blocked. */
  reasons: string[];
  warnings: string[];
  divergenceVsFuture: number | null;
  /** Annualised carry implied between parity forward and future (calendar mode only). */
  impliedCarryAnnual: number | null;
  /** 'same_expiry' (point band) or 'calendar' (carry band). */
  futureCheckMode: 'same_expiry' | 'calendar' | null;
  indexDivergence: number | null;
  perStrikeSpread: number;
  thresholds: typeof GATE_DEFAULTS;
}

export function checkGate(input: GateInput): GateResult {
  const t = { ...GATE_DEFAULTS, ...(input.thresholds ?? {}) };
  const findings: GateFinding[] = [];

  // --- Block 1: no futures quote to cross-check against. -------------------
  let divergenceVsFuture: number | null = null;
  let impliedCarryAnnual: number | null = null;
  let futureCheckMode: 'same_expiry' | 'calendar' | null = null;
  if (input.listedFuture === null || !Number.isFinite(input.listedFuture)) {
    findings.push({
      severity: 'block',
      code: 'NO_FUTURES_QUOTE',
      message:
        'No futures quote available for cross-check. The parity forward cannot be ' +
        'validated, and the index LTP is not an acceptable substitute.',
      observed: null,
      threshold: null,
    });
  } else {
    divergenceVsFuture = input.listedFuture - input.parityForward;
    const abs = Math.abs(divergenceVsFuture);
    const gap = input.futureExpiryGapYears ?? 0;
    if (Number.isFinite(gap) && gap > t.sameExpiryGapYears) {
      // Calendar mode: future and options expire on different dates.
      futureCheckMode = 'calendar';
      impliedCarryAnnual =
        input.parityForward > 0 && input.listedFuture > 0
          ? Math.log(input.listedFuture / input.parityForward) / gap
          : null;
      if (
        impliedCarryAnnual === null ||
        !Number.isFinite(impliedCarryAnnual) ||
        impliedCarryAnnual < t.minCarryAnnual ||
        impliedCarryAnnual > t.maxCarryAnnual
      ) {
        findings.push({
          severity: 'block',
          code: 'FUTURE_CARRY',
          message:
            `Parity forward ${input.parityForward.toFixed(4)} vs listed future ` +
            `${input.listedFuture.toFixed(4)} (${(gap * 365).toFixed(1)} days later) implies ` +
            `${impliedCarryAnnual === null ? 'no' : (impliedCarryAnnual * 100).toFixed(2) + '%'} ` +
            `annualised carry, outside the ${(t.minCarryAnnual * 100).toFixed(0)}%..` +
            `${(t.maxCarryAnnual * 100).toFixed(0)}% band. Wrong contract or stale leg.`,
          observed: impliedCarryAnnual,
          threshold: t.maxCarryAnnual,
        });
      }
    } else if (abs > t.maxFutureDivergence) {
      futureCheckMode = 'same_expiry';
      findings.push({
        severity: 'block',
        code: 'FUTURE_DIVERGENCE',
        message:
          `Parity forward ${input.parityForward.toFixed(4)} diverges from listed ` +
          `future ${input.listedFuture.toFixed(4)} by ${abs.toFixed(4)} points, ` +
          `above the ${t.maxFutureDivergence}-point limit.`,
        observed: abs,
        threshold: t.maxFutureDivergence,
      });
    }
  }

  // --- Block 2: per-strike parity dispersion. ------------------------------
  if (input.perStrikeSpread > t.maxPerStrikeSpread) {
    findings.push({
      severity: 'block',
      code: 'PARITY_SPREAD',
      message:
        `Per-strike parity spread ${input.perStrikeSpread.toFixed(4)} points exceeds ` +
        `the ${t.maxPerStrikeSpread}-point limit. At least one leg is stale or crossed.`,
      observed: input.perStrikeSpread,
      threshold: t.maxPerStrikeSpread,
    });
  }

  // --- Warn: the index carry error this whole architecture exists to avoid. -
  let indexDivergence: number | null = null;
  if (input.indexLtp != null && Number.isFinite(input.indexLtp)) {
    indexDivergence = input.parityForward - input.indexLtp;
    if (Math.abs(indexDivergence) > t.indexDivergenceWarn) {
      const annualised = input.indexLtp > 0 ? indexDivergence / input.indexLtp : 0;
      findings.push({
        severity: 'warn',
        code: 'INDEX_CARRY',
        message:
          `Index LTP ${input.indexLtp.toFixed(2)} sits ${indexDivergence.toFixed(4)} ` +
          `points below the forward (${(annualised * 100).toFixed(2)}% carry over the ` +
          `remaining life). Do NOT use the index LTP as spot for option maths.`,
        observed: Math.abs(indexDivergence),
        threshold: t.indexDivergenceWarn,
      });
    }
  }

  // --- Warn: slope diagnostic. Never a block; the band is ill-conditioned. --
  if (input.slopeWithinTolerance === false) {
    findings.push({
      severity: 'warn',
      code: 'SLOPE_DIAGNOSTIC',
      message:
        `Regression slope of (C-P) on K is off by ` +
        `${((input.slopeRelativeError ?? NaN) * 100).toFixed(2)}%. This is a ` +
        `DIAGNOSTIC only — over a narrow strike band the fit is ill-conditioned, ` +
        `so it does not and must not affect the forward.`,
      observed: input.slopeRelativeError ?? null,
      threshold: null,
    });
  }

  const blocks = findings.filter((f) => f.severity === 'block');

  return {
    blocked: blocks.length > 0,
    findings,
    reasons: blocks.map((f) => f.message),
    warnings: findings.filter((f) => f.severity === 'warn').map((f) => f.message),
    divergenceVsFuture,
    impliedCarryAnnual,
    futureCheckMode:
      futureCheckMode ?? (divergenceVsFuture === null ? null : 'same_expiry'),
    indexDivergence,
    perStrikeSpread: input.perStrikeSpread,
    thresholds: t,
  };
}
