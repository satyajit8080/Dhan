/**
 * build_snapshot — the one atomic object.
 *
 * Order is deliberate and must not change:
 *   1. CHAIN FIRST. It is the slowest-refreshing, most rate-limited call
 *      (1 unique request / 3 sec) and it anchors the snapshot.
 *   2. Futures immediately after, for the parity cross-check.
 *   3. Skew gate. If the two did not land close enough together, refuse.
 *   4. Pricing, against that one snapshot.
 *   5. Liquidity, screened from the same chain object.
 *
 * Fetching futures first would mean the cross-check reference is already stale
 * by the time the chain arrives — precisely the two-timestamp defect this
 * architecture exists to prevent.
 */

import { fetchOptionChain } from './endpoints/optionChain.js';
import { fetchQuote, pickInstrument } from './endpoints/marketQuote.js';
import { newFetchId, normalizeChain, normalizeQuote } from './normalize.js';
import { checkChain, checkQuote } from './integrity.js';
import { attachPricing, type PricingContext } from './pricingBridge.js';
import { assessDepth, screenChain, type DepthAssessment, type ScreenCandidate } from './liquidity.js';
import { GateBlockedError, SnapshotSkewError } from './errors.js';
import type { Transport } from './transport.js';
import type { Logger, Settings } from './config.js';
import type { InstrumentRegistry } from './instruments/registry.js';
import type { ExpiryCache } from './instruments/expiries.js';
import type {
  CanonicalChain,
  CanonicalQuote,
  IntegrityFinding,
  IntegrityReport,
} from './types.js';

export interface SnapshotOptions {
  /** Expiry. Omitted means "nearest listed", resolved live and cached. */
  expiry?: string;
  /** Futures contract month for the cross-check. Defaults to the expiry month. */
  futuresMonth?: string;
  /** Run Stage-2 depth on screened candidates. Costs one extra quote call. */
  includeDepth?: boolean;
  /** Size, in lots, that depth is assessed at. */
  lots?: number;
  /** Cap on Stage-2 candidates. */
  maxCandidates?: number;
  /** Restrict screening to strikes within this fraction of the FORWARD. */
  screenBandPct?: number;
}

export interface MarketSnapshot {
  underlying: string;
  expiry: string;
  fetchId: string;
  epochMs: number;
  source: 'dhan-rest-v2';
  chain: CanonicalChain;
  futures: CanonicalQuote | null;
  /** Null only when the gate blocked; `blocked` then explains why. */
  pricing: PricingContext | null;
  blocked: { reasons: string[]; details: Record<string, unknown> } | null;
  liquidity: {
    screened: ScreenCandidate[];
    candidates: ScreenCandidate[];
    depth: DepthAssessment[];
    lots: number;
    note: string;
  } | null;
  integrity: IntegrityReport;
  warnings: string[];
}

function mergeIntegrity(reports: IntegrityReport[], nowMs: number): IntegrityReport {
  const findings: IntegrityFinding[] = reports.flatMap((r) => r.findings);
  return {
    ok: !findings.some((f) => f.severity === 'block'),
    findings,
    checkedAtMs: nowMs,
  };
}

export async function buildSnapshot(
  deps: {
    transport: Transport;
    registry: InstrumentRegistry;
    expiries: ExpiryCache;
    settings: Settings;
    log: Logger;
  },
  underlyingName: string,
  opts: SnapshotOptions = {},
): Promise<MarketSnapshot> {
  const { transport, registry, expiries, settings, log } = deps;
  const spec = registry.underlying(underlyingName);
  const warnings: string[] = [];

  if (!spec.verified) warnings.push(spec.verificationNote);

  // --- resolve the expiry live; never hardcoded -----------------------------
  const today = new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
  const expiry = opts.expiry ?? (await expiries.nearest(spec.scrip, spec.segment, today));
  if (opts.expiry) await expiries.assertValid(spec.scrip, spec.segment, opts.expiry);

  // --- 1. CHAIN FIRST -------------------------------------------------------
  const chainFetchId = newFetchId();
  const rawChain = await fetchOptionChain(transport, {
    scrip: spec.scrip,
    segment: spec.segment,
    expiry,
  });
  const chain = normalizeChain(
    rawChain.data,
    {
      underlying: spec.name,
      underlyingScrip: spec.scrip,
      underlyingSeg: spec.segment,
      expiry,
    },
    chainFetchId,
    rawChain.receivedAtMs,
    '/optionchain',
  );
  const chainIntegrity = checkChain(chain, Date.now());

  // --- 2. Futures, immediately after ---------------------------------------
  let futures: CanonicalQuote | null = null;
  let futuresIntegrity: IntegrityReport = { ok: true, findings: [], checkedAtMs: Date.now() };
  let futuresExpiry: string | undefined;

  try {
    const fut = registry.futuresForExpiry(spec.name, expiry);
    futuresExpiry = fut.expiry;
    const futFetchId = newFetchId();
    const rawFut = await fetchQuote(transport, {
      [fut.segment]: [fut.securityId],
    });
    const rawLeg = pickInstrument(rawFut.data, fut.segment, fut.securityId);
    if (rawLeg) {
      futures = normalizeQuote(
        rawLeg,
        String(fut.securityId),
        fut.segment,
        futFetchId,
        rawFut.receivedAtMs,
        '/marketfeed/quote',
      );
      futuresIntegrity = checkQuote(futures, Date.now(), settings.maxQuoteAgeMs);
    } else {
      warnings.push(
        `Futures ${fut.segment}:${fut.securityId} returned no data. The gate will block.`,
      );
    }
  } catch (err) {
    warnings.push(
      `Could not fetch the futures cross-check: ${(err as Error).message}. The gate will block.`,
    );
    log.warn('Futures fetch failed', { error: (err as Error).message });
  }

  // --- 3+4. Skew gate, then pricing ----------------------------------------
  let pricing: PricingContext | null = null;
  let blocked: MarketSnapshot['blocked'] = null;

  try {
    pricing = attachPricing(chain, futures, {
      riskFreeRate: settings.riskFreeRate,
      maxSnapshotSkewMs: settings.maxSnapshotSkewMs,
      ...(futuresExpiry ? { futuresExpiry } : {}),
    });
    warnings.push(...pricing.gate.warnings);
  } catch (err) {
    if (err instanceof GateBlockedError) {
      blocked = { reasons: err.reasons, details: err.details };
      log.warn('Pricing gate BLOCKED — publishing nothing', { reasons: err.reasons });
    } else if (err instanceof SnapshotSkewError) {
      blocked = { reasons: [err.message], details: err.details };
      log.warn('Snapshot skew — refusing to price across two timestamps', {
        details: err.details,
      });
    } else {
      throw err;
    }
  }

  // --- 5. Liquidity, from the SAME chain object ----------------------------
  let liquidity: MarketSnapshot['liquidity'] = null;

  if (pricing) {
    const screen = screenChain(chain.strikes, {
      reference: pricing.forward, // the forward, never the index LTP
      bandPct: opts.screenBandPct ?? 0.02,
      maxCandidates: opts.maxCandidates ?? 12,
    });

    const lots = opts.lots ?? 1;
    const depth: DepthAssessment[] = [];

    if (opts.includeDepth && screen.candidates.length > 0) {
      const ids = screen.candidates
        .map((c) => Number(c.securityId))
        .filter((n) => Number.isFinite(n));

      if (ids.length > 0) {
        // ONE call for every candidate. Stage 2 costs a single quote request.
        const depthFetchId = newFetchId();
        const rawDepth = await fetchQuote(transport, { [spec.derivativeSegment]: ids });
        for (const c of screen.candidates) {
          const rawLeg = pickInstrument(rawDepth.data, spec.derivativeSegment, c.securityId!);
          if (!rawLeg) continue;
          const q = normalizeQuote(
            rawLeg,
            String(c.securityId),
            spec.derivativeSegment,
            depthFetchId,
            rawDepth.receivedAtMs,
            '/marketfeed/quote',
          );
          depth.push(assessDepth(q, lots, spec.lotSize));
        }
      }
    }

    liquidity = {
      screened: screen.all,
      candidates: screen.candidates,
      depth,
      lots,
      note: opts.includeDepth
        ? `Stage 1 screened ${screen.all.length} legs free from the chain; Stage 2 walked ` +
          `5-level depth for ${depth.length} candidate(s) at ${lots} lot(s) in one quote call.`
        : `Stage 1 only: ${screen.all.length} legs screened on top-of-book. Pass ` +
          `include_depth=true for executable cost at size.`,
    };
  }

  return {
    underlying: spec.name,
    expiry,
    fetchId: chain.provenance.fetchId,
    epochMs: chain.provenance.epochMs,
    source: 'dhan-rest-v2',
    chain,
    futures,
    pricing,
    blocked,
    liquidity,
    integrity: mergeIntegrity([chainIntegrity, futuresIntegrity], Date.now()),
    warnings,
  };
}
