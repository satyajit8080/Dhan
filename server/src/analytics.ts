/**
 * Option-chain positioning analytics. PURE MODULE — no I/O, no clock.
 *
 * Computes PCR, max pain, OI buildup classification, OI concentration and
 * OI-derived support/resistance from ONE chain snapshot.
 *
 * EMITS NO VERDICT. Every function here reports what the book shows. Nothing
 * decides direction, and nothing produces a trigger level — those rules are
 * the caller's, and they are not defined in this server.
 *
 * SPOT DISCIPLINE: max pain and moneyness take an explicit `reference` price.
 * Callers must pass the PARITY FORWARD, never the index LTP. Computing max pain
 * against the index is how a 321-point carry error leaks into positioning
 * analysis after being carefully removed from the Greeks.
 */

import type { ChainStrike } from './types.js';

export type Buildup =
  | 'long_buildup'
  | 'short_buildup'
  | 'short_covering'
  | 'long_unwinding'
  | 'neutral'
  | 'unknown';

export interface PcrResult {
  /** Total PE OI / total CE OI. Null when call OI is zero. */
  oiPcr: number | null;
  /** Total PE volume / total CE volume. */
  volumePcr: number | null;
  totalCallOi: number;
  totalPutOi: number;
  totalCallVolume: number;
  totalPutVolume: number;
  strikesCounted: number;
}

/** Put-call ratio over the supplied strikes. No interpretation attached. */
export function calculatePcr(strikes: ChainStrike[]): PcrResult {
  let ce = 0;
  let pe = 0;
  let cev = 0;
  let pev = 0;
  let counted = 0;

  for (const s of strikes) {
    if (!s.ce && !s.pe) continue;
    counted++;
    ce += s.ce?.oi ?? 0;
    pe += s.pe?.oi ?? 0;
    cev += s.ce?.volume ?? 0;
    pev += s.pe?.volume ?? 0;
  }

  return {
    oiPcr: ce > 0 ? pe / ce : null,
    volumePcr: cev > 0 ? pev / cev : null,
    totalCallOi: ce,
    totalPutOi: pe,
    totalCallVolume: cev,
    totalPutVolume: pev,
    strikesCounted: counted,
  };
}

export interface MaxPainResult {
  /** Strike at which option writers lose least in aggregate. */
  maxPainStrike: number | null;
  /** Distance from the reference, in points. Positive = max pain above. */
  distanceFromReference: number | null;
  /** Per-strike total writer pain, ascending by strike. */
  painByStrike: { strike: number; pain: number }[];
  reference: number;
  note: string;
}

/**
 * Max pain: the strike minimising total intrinsic payout to option holders.
 *
 * At expiry settlement S, call writers pay sum over K<S of (S-K)*CE_OI, and put
 * writers pay sum over K>S of (K-S)*PE_OI. Max pain is the S among listed
 * strikes minimising the sum.
 */
export function calculateMaxPain(
  strikes: ChainStrike[],
  reference: number,
): MaxPainResult {
  const usable = strikes.filter((s) => (s.ce?.oi ?? 0) > 0 || (s.pe?.oi ?? 0) > 0);

  if (usable.length === 0) {
    return {
      maxPainStrike: null,
      distanceFromReference: null,
      painByStrike: [],
      reference,
      note: 'No open interest in the supplied strikes; max pain is undefined.',
    };
  }

  const painByStrike = usable
    .map((candidate) => {
      const settle = candidate.strike;
      let pain = 0;
      for (const s of usable) {
        if (s.strike < settle) pain += (settle - s.strike) * (s.ce?.oi ?? 0);
        if (s.strike > settle) pain += (s.strike - settle) * (s.pe?.oi ?? 0);
      }
      return { strike: settle, pain };
    })
    .sort((a, b) => a.strike - b.strike);

  let best = painByStrike[0]!;
  for (const p of painByStrike) if (p.pain < best.pain) best = p;

  return {
    maxPainStrike: best.strike,
    distanceFromReference: best.strike - reference,
    painByStrike,
    reference,
    note:
      'Computed against the supplied reference. Pass the parity forward, never ' +
      'the index LTP.',
  };
}

export interface LegPositioning {
  strike: number;
  side: 'CE' | 'PE';
  lastPrice: number | null;
  priceChange: number | null;
  priceChangePct: number | null;
  oi: number | null;
  previousOi: number | null;
  oiChange: number | null;
  oiChangePct: number | null;
  volume: number | null;
  buildup: Buildup;
  /** Plain description of what the buildup means for this leg. */
  buildupNote: string;
}

/**
 * Classify one leg from price change and OI change.
 *
 *   price up   + OI up   -> long buildup    (fresh buying)
 *   price down + OI up   -> short buildup   (fresh writing)
 *   price up   + OI down -> short covering  (writers buying back)
 *   price down + OI down -> long unwinding  (holders exiting)
 */
export function classifyBuildup(
  priceChange: number | null,
  oiChange: number | null,
  epsilonPrice = 0,
  epsilonOi = 0,
): Buildup {
  if (priceChange === null || oiChange === null) return 'unknown';

  const pUp = priceChange > epsilonPrice;
  const pDown = priceChange < -epsilonPrice;
  const oUp = oiChange > epsilonOi;
  const oDown = oiChange < -epsilonOi;

  if (pUp && oUp) return 'long_buildup';
  if (pDown && oUp) return 'short_buildup';
  if (pUp && oDown) return 'short_covering';
  if (pDown && oDown) return 'long_unwinding';
  return 'neutral';
}

const BUILDUP_NOTES: Record<Buildup, string> = {
  long_buildup: 'Price up with rising OI — fresh longs in this leg.',
  short_buildup: 'Price down with rising OI — fresh writing in this leg.',
  short_covering: 'Price up with falling OI — writers buying back.',
  long_unwinding: 'Price down with falling OI — holders exiting.',
  neutral: 'No material change in price or OI.',
  unknown: 'Insufficient data: price change or OI change unavailable.',
};

/** Positioning for every leg in the supplied strikes. */
export function analyzePositioning(strikes: ChainStrike[]): LegPositioning[] {
  const out: LegPositioning[] = [];

  for (const s of strikes) {
    for (const side of ['CE', 'PE'] as const) {
      const leg = side === 'CE' ? s.ce : s.pe;
      if (!leg) continue;

      const priceChange =
        leg.lastPrice !== null && leg.previousClosePrice !== null
          ? leg.lastPrice - leg.previousClosePrice
          : null;
      const oiChange =
        leg.oi !== null && leg.previousOi !== null ? leg.oi - leg.previousOi : null;

      const buildup = classifyBuildup(priceChange, oiChange);

      out.push({
        strike: s.strike,
        side,
        lastPrice: leg.lastPrice,
        priceChange,
        priceChangePct:
          priceChange !== null && leg.previousClosePrice
            ? (priceChange / leg.previousClosePrice) * 100
            : null,
        oi: leg.oi,
        previousOi: leg.previousOi,
        oiChange,
        oiChangePct:
          oiChange !== null && leg.previousOi ? (oiChange / leg.previousOi) * 100 : null,
        volume: leg.volume,
        buildup,
        buildupNote: BUILDUP_NOTES[buildup],
      });
    }
  }

  return out;
}

export interface OiLevel {
  strike: number;
  oi: number;
  oiChange: number | null;
}

export interface ConcentrationResult {
  /** Strikes with the largest call OI — where upside is being written. */
  topCallOi: OiLevel[];
  /** Strikes with the largest put OI — where downside is being written. */
  topPutOi: OiLevel[];
  /** Largest OI ADDITIONS this session, call side. */
  topCallOiAdds: OiLevel[];
  topPutOiAdds: OiLevel[];
  /** Highest put-OI strike. Conventionally read as support. */
  putOiPeakStrike: number | null;
  /** Highest call-OI strike. Conventionally read as resistance. */
  callOiPeakStrike: number | null;
  note: string;
}

/** OI concentration and the strikes where positioning changed most. */
export function analyzeConcentration(
  strikes: ChainStrike[],
  topN = 5,
): ConcentrationResult {
  const calls: OiLevel[] = [];
  const puts: OiLevel[] = [];

  for (const s of strikes) {
    if (s.ce?.oi != null) {
      calls.push({
        strike: s.strike,
        oi: s.ce.oi,
        oiChange: s.ce.previousOi !== null ? s.ce.oi - s.ce.previousOi : null,
      });
    }
    if (s.pe?.oi != null) {
      puts.push({
        strike: s.strike,
        oi: s.pe.oi,
        oiChange: s.pe.previousOi !== null ? s.pe.oi - s.pe.previousOi : null,
      });
    }
  }

  const byOi = (a: OiLevel, b: OiLevel) => b.oi - a.oi;
  const byAdds = (a: OiLevel, b: OiLevel) => (b.oiChange ?? 0) - (a.oiChange ?? 0);

  const topCallOi = [...calls].sort(byOi).slice(0, topN);
  const topPutOi = [...puts].sort(byOi).slice(0, topN);

  return {
    topCallOi,
    topPutOi,
    topCallOiAdds: [...calls].sort(byAdds).slice(0, topN),
    topPutOiAdds: [...puts].sort(byAdds).slice(0, topN),
    putOiPeakStrike: topPutOi[0]?.strike ?? null,
    callOiPeakStrike: topCallOi[0]?.strike ?? null,
    note:
      'OI peaks are where writers are positioned. Treating them as support and ' +
      'resistance is a convention, not a derivation — this module states the ' +
      'concentration and leaves the reading to the caller.',
  };
}

export interface ChainSummary {
  pcr: PcrResult;
  maxPain: MaxPainResult;
  concentration: ConcentrationResult;
  positioning: LegPositioning[];
  buildupTally: Record<Buildup, number>;
  reference: number;
  strikesAnalyzed: number;
}

/** One pass over the chain producing every positioning metric. */
export function analyzeChain(
  strikes: ChainStrike[],
  reference: number,
  opts: { topN?: number } = {},
): ChainSummary {
  const positioning = analyzePositioning(strikes);

  const buildupTally: Record<Buildup, number> = {
    long_buildup: 0,
    short_buildup: 0,
    short_covering: 0,
    long_unwinding: 0,
    neutral: 0,
    unknown: 0,
  };
  for (const p of positioning) buildupTally[p.buildup]++;

  return {
    pcr: calculatePcr(strikes),
    maxPain: calculateMaxPain(strikes, reference),
    concentration: analyzeConcentration(strikes, opts.topN ?? 5),
    positioning,
    buildupTally,
    reference,
    strikesAnalyzed: strikes.length,
  };
}
