/**
 * Strike selection. PURE MODULE — no I/O, no clock.
 *
 * Given a SIDE that has already been decided elsewhere, rank the tradeable
 * strikes on that side using delta, liquidity, spread, OI, volume, theta burden
 * and distance from the forward.
 *
 * This module never chooses CE vs PE. It answers "given CE, which strike", and
 * only that. Per the spec it does NOT default to ATM — ATM is one candidate
 * among several and must earn its place on the same criteria as the rest.
 */

import type { PricedLeg } from './pricingBridge.js';
import type { DepthAssessment, LiquidityGrade } from './liquidity.js';
import type { LegPositioning } from './analytics.js';

export interface StrikeCandidate {
  strike: number;
  side: 'CE' | 'PE';
  securityId: string | null;
  optionPrice: number;
  /** Absolute delta — the directional exposure per point of forward. */
  absDelta: number | null;
  gamma: number | null;
  /** Per calendar day, negative for a long option. */
  theta: number | null;
  vega: number | null;
  ivPct: number | null;
  /** theta per day divided by option price — daily decay as a fraction. */
  thetaBurnPctPerDay: number | null;
  distanceFromForward: number;
  moneyness: 'ITM' | 'ATM' | 'OTM';
  oi: number | null;
  oiChange: number | null;
  volume: number | null;
  liquidityGrade: LiquidityGrade | null;
  roundtripPct: number | null;
  quotedSpreadPct: number | null;
  /** 0..1, higher is better. Composite of the criteria below. */
  score: number;
  scoreBreakdown: Record<string, number>;
  rejectReasons: string[];
  eligible: boolean;
}

export interface SelectionCriteria {
  /** Preferred absolute delta band. Outside it, score decays. */
  targetDeltaMin?: number;
  targetDeltaMax?: number;
  /** Worst acceptable liquidity grade. */
  minGrade?: LiquidityGrade;
  /** Maximum acceptable round-trip cost at size, percent. */
  maxRoundtripPct?: number;
  /** Minimum open interest. */
  minOi?: number;
  /** Maximum acceptable daily theta burn as a percent of premium. */
  maxThetaBurnPctPerDay?: number;
  /** Relative weights; normalised internally. */
  weights?: Partial<Record<'delta' | 'liquidity' | 'spread' | 'oi' | 'theta' | 'volume', number>>;
}

const GRADE_RANK: Record<LiquidityGrade, number> = { A: 4, B: 3, C: 2, F: 1 };

const DEFAULTS: Required<Omit<SelectionCriteria, 'weights'>> & {
  weights: Record<'delta' | 'liquidity' | 'spread' | 'oi' | 'theta' | 'volume', number>;
} = {
  // A common scalping band: enough directional pull without paying for deep ITM.
  targetDeltaMin: 0.35,
  targetDeltaMax: 0.6,
  minGrade: 'B',
  maxRoundtripPct: 1.2,
  minOi: 0,
  maxThetaBurnPctPerDay: 100,
  weights: { delta: 0.3, liquidity: 0.25, spread: 0.15, oi: 0.1, theta: 0.15, volume: 0.05 },
};

/** Triangular preference: 1.0 inside the band, decaying outside. */
function deltaScore(absDelta: number | null, lo: number, hi: number): number {
  if (absDelta === null) return 0;
  if (absDelta >= lo && absDelta <= hi) return 1;
  const d = absDelta < lo ? lo - absDelta : absDelta - hi;
  return Math.max(0, 1 - d / 0.25);
}

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x));
}

/**
 * Rank strikes on one side.
 *
 * @param legs        priced legs from the snapshot (one side only is fine)
 * @param side        the already-decided direction
 * @param forward     parity forward — never the index LTP
 * @param depth       Stage-2 depth assessments keyed by security id
 * @param positioning OI/buildup per leg, optional
 */
export function selectStrike(
  legs: PricedLeg[],
  side: 'CE' | 'PE',
  forward: number,
  depth: Map<string, DepthAssessment>,
  positioning: Map<string, LegPositioning>,
  criteria: SelectionCriteria = {},
): { ranked: StrikeCandidate[]; best: StrikeCandidate | null; criteriaUsed: typeof DEFAULTS } {
  const c = {
    ...DEFAULTS,
    ...criteria,
    weights: { ...DEFAULTS.weights, ...(criteria.weights ?? {}) },
  };

  const totalWeight = Object.values(c.weights).reduce((a, b) => a + b, 0) || 1;
  const candidates: StrikeCandidate[] = [];

  // Strike step, to decide what counts as ATM.
  const strikes = [...new Set(legs.map((l) => l.strike))].sort((a, b) => a - b);
  const step =
    strikes.length > 1
      ? Math.min(...strikes.slice(1).map((s, i) => s - strikes[i]!))
      : 100;

  for (const leg of legs) {
    if (leg.type !== side) continue;

    const key = leg.securityId ?? '';
    const d = depth.get(key) ?? null;
    const pos = positioning.get(`${leg.strike}:${side}`) ?? null;

    const absDelta = leg.delta === null ? null : Math.abs(leg.delta);
    const dist = leg.strike - forward;
    const moneyness: StrikeCandidate['moneyness'] =
      Math.abs(dist) <= step / 2
        ? 'ATM'
        : side === 'CE'
          ? dist < 0
            ? 'ITM'
            : 'OTM'
          : dist > 0
            ? 'ITM'
            : 'OTM';

    const thetaBurn =
      leg.theta !== null && leg.marketPrice > 0
        ? (Math.abs(leg.theta) / leg.marketPrice) * 100
        : null;

    const rejectReasons: string[] = [];
    if (leg.ivPct === null) {
      rejectReasons.push('No IV — price outside no-arbitrage bounds, so the quote is untrusted.');
    }
    if (d && GRADE_RANK[d.grade] < GRADE_RANK[c.minGrade]) {
      rejectReasons.push(`Liquidity grade ${d.grade} is below the ${c.minGrade} floor.`);
    }
    if (d?.roundtripPct != null && d.roundtripPct > c.maxRoundtripPct) {
      rejectReasons.push(
        `Round-trip ${d.roundtripPct.toFixed(2)}% exceeds ${c.maxRoundtripPct}% at size.`,
      );
    }
    if (c.minOi > 0 && (pos?.oi ?? 0) < c.minOi) {
      rejectReasons.push(`OI ${pos?.oi ?? 0} below the ${c.minOi} floor.`);
    }
    if (thetaBurn !== null && thetaBurn > c.maxThetaBurnPctPerDay) {
      rejectReasons.push(
        `Theta burn ${thetaBurn.toFixed(1)}%/day exceeds ${c.maxThetaBurnPctPerDay}%.`,
      );
    }
    if (!leg.securityId) rejectReasons.push('No security id.');

    const sDelta = deltaScore(absDelta, c.targetDeltaMin, c.targetDeltaMax);
    const sLiquidity = d ? (GRADE_RANK[d.grade] - 1) / 3 : 0;
    const sSpread =
      d?.roundtripPct != null ? clamp01(1 - d.roundtripPct / (c.maxRoundtripPct * 2)) : 0;
    const sOi = pos?.oi ? clamp01(Math.log10(pos.oi + 1) / 7) : 0;
    const sVolume = pos?.volume ? clamp01(Math.log10(pos.volume + 1) / 7) : 0;
    const sTheta = thetaBurn === null ? 0 : clamp01(1 - thetaBurn / 25);

    const breakdown = {
      delta: sDelta * c.weights.delta,
      liquidity: sLiquidity * c.weights.liquidity,
      spread: sSpread * c.weights.spread,
      oi: sOi * c.weights.oi,
      theta: sTheta * c.weights.theta,
      volume: sVolume * c.weights.volume,
    };

    const score =
      Object.values(breakdown).reduce((a, b) => a + b, 0) / totalWeight;

    candidates.push({
      strike: leg.strike,
      side,
      securityId: leg.securityId,
      optionPrice: leg.marketPrice,
      absDelta,
      gamma: leg.gamma,
      theta: leg.theta,
      vega: leg.vega,
      ivPct: leg.ivPct,
      thetaBurnPctPerDay: thetaBurn,
      distanceFromForward: dist,
      moneyness,
      oi: pos?.oi ?? null,
      oiChange: pos?.oiChange ?? null,
      volume: pos?.volume ?? null,
      liquidityGrade: d?.grade ?? null,
      roundtripPct: d?.roundtripPct ?? null,
      quotedSpreadPct: d?.quotedSpreadPct ?? null,
      score,
      scoreBreakdown: breakdown,
      rejectReasons,
      eligible: rejectReasons.length === 0,
    });
  }

  candidates.sort((a, b) => b.score - a.score);
  const eligible = candidates.filter((x) => x.eligible);

  return {
    ranked: candidates,
    best: eligible.length > 0 ? eligible[0]! : null,
    criteriaUsed: c,
  };
}
