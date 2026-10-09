/**
 * Snapshot -> response digest.
 *
 * A full SENSEX chain is far too large to hand a model verbatim, and the useful
 * part is the near-ATM band plus the integrity block. This trims to that while
 * preserving EVERY integrity signal — a digest that dropped a gate warning
 * would be worse than no digest.
 *
 * Kept out of mcpServer.ts so it can be tested without starting a server.
 */

import type { MarketSnapshot } from './snapshot.js';

export interface RateLimitStats {
  day: string;
  used: number;
  quota: number;
  remaining: number;
}

export const round = (v: number | null | undefined, dp = 4): number | null =>
  v === null || v === undefined || !Number.isFinite(v) ? null : Number(v.toFixed(dp));

export function digestSnapshot(
  s: MarketSnapshot,
  bandStrikes: number,
  rateLimit: RateLimitStats,
) {
  const base = {
    underlying: s.underlying,
    expiry: s.expiry,
    fetch_id: s.fetchId,
    epoch_ms: s.epochMs,
    as_of_ist: new Date(s.epochMs + 5.5 * 3600_000).toISOString().replace('Z', ' IST'),
    source: s.source,
    integrity: { ok: s.integrity.ok, findings: s.integrity.findings },
    warnings: s.warnings,
    rate_limit: rateLimit,
  };

  if (!s.pricing) {
    return {
      ...base,
      published: false,
      blocked: s.blocked,
      note:
        'GATE BLOCKED — no pricing published. The index LTP is NOT used as a fallback. ' +
        'Resolve the reasons above and retry.',
    };
  }

  const p = s.pricing;
  const atm = p.atmStrike ?? p.forward;
  const near = p.legs
    .filter((l) => Math.abs(l.strike - atm) <= bandStrikes)
    .map((l) => ({
      strike: l.strike,
      type: l.type,
      security_id: l.securityId,
      ltp: l.marketPrice,
      iv_pct: round(l.ivPct, 4),
      delta: round(l.delta, 5),
      gamma: round(l.gamma, 8),
      vega_per_iv_pt: round(l.vega, 4),
      theta_per_day: round(l.theta, 4),
      rho_per_1pct: round(l.rho, 5),
      vendor_iv_pct: round(l.vendorIvPct, 4),
      vendor_iv_delta_pct: round(l.vendorIvDeltaPct, 4),
    }));

  return {
    ...base,
    published: true,
    pricing: {
      model: 'Black-76 on the put-call-parity forward',
      T: p.T,
      calendar_days_to_expiry: round(p.calendarDaysToExpiry, 6),
      risk_free_rate: p.riskFreeRate,
      discount_factor: round(p.discountFactor, 12),
      forward: round(p.forward, 4),
      forward_method: 'median of per-strike F_K = K + (C-P)/DF, 1.5% ATM band',
      per_strike_spread: round(p.forwardDetail.spread, 4),
      strikes_used: p.forwardDetail.usedStrikes,
      atm_strike: p.atmStrike,
      listed_future: round(p.listedFuture, 4),
      divergence_vs_future: round(p.gate.divergenceVsFuture, 4),
      index_ltp_do_not_use_as_spot: round(p.indexLtpDoNotUseAsSpot, 4),
      index_divergence: round(p.gate.indexDivergence, 4),
      slope_diagnostic: {
        note: 'DIAGNOSTIC ONLY — never sets the forward.',
        slope: round(p.forwardDetail.slopeDiagnostic.slope, 8),
        expected: round(p.forwardDetail.slopeDiagnostic.expectedSlope, 8),
        relative_error: round(p.forwardDetail.slopeDiagnostic.relativeError, 6),
        within_tolerance: p.forwardDetail.slopeDiagnostic.withinTolerance,
      },
      gate: {
        blocked: p.gate.blocked,
        thresholds: p.gate.thresholds,
        warnings: p.gate.warnings,
      },
      snapshot: p.snapshot,
      greeks_conventions: {
        delta: 'dV/dF, discounted',
        gamma: 'per point^2',
        vega: 'per 1 IV POINT',
        theta: 'per CALENDAR DAY',
        rho: '-T*V per 1% (Black-76)',
        iv: 'bisection on price; null outside no-arbitrage bounds',
      },
      legs_near_atm: near,
      legs_total: p.legs.length,
    },
    liquidity: s.liquidity
      ? {
          note: s.liquidity.note,
          lots: s.liquidity.lots,
          screened_count: s.liquidity.screened.length,
          candidates: s.liquidity.candidates.map((c) => ({
            strike: c.strike,
            type: c.type,
            security_id: c.securityId,
            top_bid: c.topBid,
            top_ask: c.topAsk,
            quoted_spread_pct: round(c.quotedSpreadPct, 4),
            oi: c.oi,
          })),
          depth: s.liquidity.depth.map((d) => ({
            security_id: d.securityId,
            lots: d.lots,
            quantity: d.quantity,
            best_bid: d.bestBid,
            best_ask: d.bestAsk,
            quoted_spread_pct: round(d.quotedSpreadPct, 4),
            effective_buy: round(d.effectiveBuy, 4),
            effective_sell: round(d.effectiveSell, 4),
            roundtrip_pct: round(d.roundtripPct, 4),
            slippage_multiple: round(d.slippageMultiple, 3),
            grade: d.grade,
            reasons: d.reasons,
          })),
        }
      : null,
  };
}
