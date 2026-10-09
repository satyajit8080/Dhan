/**
 * Facade. Wires config -> limiter -> transport -> endpoints -> normalize ->
 * integrity -> pricing, and exposes exactly the read-only surface the MCP
 * layer publishes.
 *
 * There is no method here that places, modifies or cancels anything.
 */

import {
  TokenProvider,
  createLogger,
  loadSettings,
  type Logger,
  type Settings,
  type TokenStatus,
} from './config.js';
import { RateLimiter } from './ratelimit.js';
import { Transport } from './transport.js';
import { InstrumentRegistry, type UnderlyingSpec } from './instruments/registry.js';
import { ExpiryCache } from './instruments/expiries.js';
import { ScripMaster, type ScripRow } from './instruments/scripmaster.js';
import { fetchQuote, pickInstrument } from './endpoints/marketQuote.js';
import { fetchOptionChain } from './endpoints/optionChain.js';
import {
  fetchDailyHistorical,
  fetchIntraday,
  toCandles,
  realisedVolatility,
  type Candle,
  type InstrumentKind,
  type IntradayInterval,
} from './endpoints/historical.js';
import { computeIndicators, type IndicatorSet } from './indicators.js';
import { analyzeChain, type LegPositioning } from './analytics.js';
import { analyzeStructure } from './structure.js';
import { selectStrike, type SelectionCriteria } from './strikeSelect.js';
import {
  deriveLevels,
  aggregateCandles,
  buildTradePlan,
  type LevelConfig,
} from './levels.js';
import type { DepthAssessment } from './liquidity.js';
import { PriceHistory, type LevelSourceQuality } from './priceHistory.js';
import type { ConfirmedLevel, LevelResult } from './levels.js';
import { newFetchId, normalizeChain, normalizeQuote } from './normalize.js';
import { checkChain, checkQuote } from './integrity.js';
import { buildSnapshot, type MarketSnapshot, type SnapshotOptions } from './snapshot.js';
import { InstrumentError } from './errors.js';
import type {
  CanonicalChain,
  CanonicalQuote,
  ExchangeSegment,
  IntegrityReport,
} from './types.js';

export interface QuoteResult {
  quote: CanonicalQuote;
  integrity: IntegrityReport;
}

export interface ChainResult {
  chain: CanonicalChain;
  integrity: IntegrityReport;
  /** Restated on every chain response so it cannot be forgotten. */
  notice: string;
}

export class Bull50DhanClient {
  readonly settings: Settings;
  readonly log: Logger;
  readonly tokens: TokenProvider;
  readonly registry: InstrumentRegistry;

  private readonly limiter: RateLimiter;
  private readonly transport: Transport;
  private readonly expiryCache: ExpiryCache;
  private readonly scripMaster: ScripMaster;
  /** Rolling price action, so a dead candle endpoint cannot block levels. */
  readonly history: PriceHistory;

  constructor(settings?: Settings) {
    this.settings = settings ?? loadSettings();
    this.log = createLogger(this.settings);
    this.tokens = new TokenProvider(this.log);
    this.limiter = new RateLimiter(this.settings.dailyQuota, this.log);
    this.transport = new Transport(this.settings, this.tokens, this.limiter, this.log);
    this.registry = new InstrumentRegistry(this.log);
    this.expiryCache = new ExpiryCache(this.transport, this.settings.expiryCacheTtlMs, this.log);
    this.scripMaster = new ScripMaster(this.settings.scripMasterTtlMs, this.log);
    this.history = new PriceHistory();
  }

  // --- credentials ---------------------------------------------------------

  setToken(token: string, clientId?: string): TokenStatus {
    return this.tokens.setToken(token, clientId);
  }

  tokenStatus(): TokenStatus {
    return this.tokens.status();
  }

  clearToken(): void {
    this.tokens.clear();
  }

  rateLimitStats() {
    return this.limiter.stats();
  }

  // --- get_quote -----------------------------------------------------------

  async getQuote(securityId: string | number, segment: ExchangeSegment): Promise<QuoteResult> {
    const id = Number(securityId);
    if (!Number.isFinite(id)) {
      throw new InstrumentError(`security_id must be numeric, got "${securityId}".`);
    }

    const fetchId = newFetchId();
    const raw = await fetchQuote(this.transport, { [segment]: [id] });
    const rawLeg = pickInstrument(raw.data, segment, id);
    if (!rawLeg) {
      throw new InstrumentError(
        `Dhan returned no data for ${segment}:${id}. Check the security id and segment.`,
        { securityId: id, segment },
      );
    }

    const quote = normalizeQuote(
      rawLeg,
      String(id),
      segment,
      fetchId,
      raw.receivedAtMs,
      '/marketfeed/quote',
    );
    return { quote, integrity: checkQuote(quote, Date.now(), this.settings.maxQuoteAgeMs) };
  }

  // --- get_option_chain ----------------------------------------------------

  async getOptionChain(underlyingName: string, expiry?: string): Promise<ChainResult> {
    const spec = this.registry.underlying(underlyingName);
    const today = new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
    const chosen =
      expiry ?? (await this.expiryCache.nearest(spec.scrip, spec.segment, today));
    if (expiry) await this.expiryCache.assertValid(spec.scrip, spec.segment, expiry);

    const fetchId = newFetchId();
    const raw = await fetchOptionChain(this.transport, {
      scrip: spec.scrip,
      segment: spec.segment,
      expiry: chosen,
    });

    const chain = normalizeChain(
      raw.data,
      {
        underlying: spec.name,
        underlyingScrip: spec.scrip,
        underlyingSeg: spec.segment,
        expiry: chosen,
      },
      fetchId,
      raw.receivedAtMs,
      '/optionchain',
    );

    return {
      chain,
      integrity: checkChain(chain, Date.now()),
      notice:
        'Vendor IV and vendor Greeks are QUARANTINED and must not be used. The chain ' +
        'last_price is the INDEX LTP, not spot. Use get_market_snapshot for gated ' +
        'pricing computed against the parity forward.',
    };
  }

  // --- get_expiries --------------------------------------------------------

  async getExpiries(underlyingName: string, force = false) {
    const spec = this.registry.underlying(underlyingName);
    const res = await this.expiryCache.get(spec.scrip, spec.segment, { force });
    return {
      underlying: spec.name,
      scrip: spec.scrip,
      segment: spec.segment,
      ...res,
      ttlMs: this.settings.expiryCacheTtlMs,
    };
  }

  // --- get_instrument ------------------------------------------------------

  async getInstrument(query: string, exchange?: string, limit = 20): Promise<ScripRow[]> {
    return this.scripMaster.search(query, { exchange, limit });
  }

  /** Retire an UNVERIFIED flag by checking the id against the instrument master. */
  async verifyInstrument(securityId: string | number, exchange?: string, symbol?: string) {
    return this.scripMaster.verify(securityId, { exchange, symbol });
  }

  // --- get_futures_quote ---------------------------------------------------

  async getFuturesQuote(underlyingName: string, month?: string): Promise<QuoteResult & {
    contract: { month: string; securityId: number; segment: ExchangeSegment };
  }> {
    const spec = this.registry.underlying(underlyingName);
    const fut = this.registry.futures(spec.name, month);
    const res = await this.getQuote(fut.securityId, fut.segment);
    return {
      ...res,
      contract: { month: fut.month, securityId: fut.securityId, segment: fut.segment },
    };
  }

  // --- get_market_snapshot -------------------------------------------------

  async getMarketSnapshot(
    underlyingName: string,
    opts: SnapshotOptions = {},
  ): Promise<MarketSnapshot> {
    return buildSnapshot(
      {
        transport: this.transport,
        registry: this.registry,
        expiries: this.expiryCache,
        settings: this.settings,
        log: this.log,
      },
      underlyingName,
      opts,
    );
  }

  // --- get_analysis --------------------------------------------------------

  /**
   * The complete internal picture: gated pricing, Greeks, liquidity, chain
   * positioning, price structure and ranked strike candidates for BOTH sides.
   *
   * Returns no verdict and no trigger level. It assembles every input a
   * decision rule could need and stops there, because the decision rules are
   * not defined in this server.
   */
  async getAnalysis(params: {
    underlying: string;
    expiry?: string;
    lots?: number;
    series?: 'index' | 'futures';
    interval?: IntradayInterval;
    selection?: SelectionCriteria;
    levelConfig?: LevelConfig;
  }) {
    const lots = params.lots ?? 5;

    // 1. Gated snapshot first — if this blocks, nothing downstream is valid.
    const snap = await this.getMarketSnapshot(params.underlying, {
      expiry: params.expiry,
      includeDepth: true,
      lots,
      maxCandidates: 20,
    });

    if (!snap.pricing) {
      return {
        published: false,
        blocked: snap.blocked,
        underlying: snap.underlying,
        expiry: snap.expiry,
        fetchId: snap.fetchId,
        epochMs: snap.epochMs,
        integrity: snap.integrity,
        warnings: snap.warnings,
        note:
          'Data-integrity gate blocked. No pricing, no positioning and no strike ' +
          'ranking were produced. The index LTP was not substituted.',
      };
    }

    const forward = snap.pricing.forward;

    // 2. Chain positioning, referenced to the FORWARD.
    const chainAnalysis = analyzeChain(snap.chain.strikes, forward);

    // 3. Candles for price structure and the breakout level.
    //    1-minute is the primary timeframe per spec; 5-minute is DERIVED from
    //    it rather than fetched again. On failure: retry once, then report the
    //    real API error — never silently omit the level.
    const candleErrors: string[] = [];
    let candleData: { candles: Candle[]; indicators: unknown } | null = null;

    for (let attempt = 0; attempt < 2 && candleData === null; attempt++) {
      try {
        const r = await this.getCandles({
          underlying: params.underlying,
          series: params.series ?? 'index',
          timeframe: 'intraday',
          interval: params.interval ?? 1,
          maxCandles: 10_000,
        });
        candleData = { candles: r.candles, indicators: r.indicators };
      } catch (err) {
        const e = err as Error & { code?: string; details?: unknown };
        candleErrors.push(
          `attempt ${attempt + 1}: [${e.code ?? 'ERROR'}] ${e.message}` +
            (e.details ? ` ${JSON.stringify(e.details)}` : ''),
        );
        if (attempt === 0) await new Promise((r) => setTimeout(r, 750));
      }
    }

    // 3b. Record this scan in the rolling history BEFORE deriving levels, so
    //     even a candle-less scan contributes price action for the next one.
    const spotForLevels =
      snap.chain.underlyingLtpDoNotUseAsSpot ??
      (candleData && candleData.candles.length > 0
        ? candleData.candles[candleData.candles.length - 1]!.close
        : NaN);

    this.history.record({
      t: snap.epochMs,
      index: Number.isFinite(spotForLevels) ? spotForLevels : null,
      forward,
      futures: snap.futures && Number.isFinite(snap.futures.ltp) ? snap.futures.ltp : null,
      dayHigh: snap.futures?.ohlc?.high ?? null,
      dayLow: snap.futures?.ohlc?.low ?? null,
    });

    // 4. Breakout / breakdown from PRICE ACTION, with a three-tier source.
    //
    //    Tier 1: real intraday candles
    //    Tier 2: synthetic candles from the rolling snapshot history
    //    Tier 3: exchange reference levels (futures day high/low)
    //
    //    The candle endpoint being down must NOT block the calculation.
    const realCandles = candleData?.candles ?? [];
    let levelSource: LevelSourceQuality = 'none';
    let levelBasis: Candle[] = [];

    if (realCandles.length >= 10) {
      levelBasis = realCandles;
      levelSource = 'candles';
    } else if (this.history.sufficientForLevels()) {
      levelBasis = this.history.toSyntheticCandles(1);
      levelSource = 'rolling_snapshots';
    }

    const fiveMin = levelBasis.length > 0 ? aggregateCandles(levelBasis, 5) : [];
    const structure = levelBasis.length > 0 ? analyzeStructure(levelBasis) : null;

    // Sparse snapshot sampling produces fewer touches than real candles, so the
    // confirmation floor drops to 2 observations of the same area rather than
    // the candle default. Structural levels still carry on their own.
    const cfg =
      levelSource === 'rolling_snapshots'
        ? { minTouches: 2, ...params.levelConfig }
        : params.levelConfig;

    let levels =
      levelBasis.length > 0 && Number.isFinite(spotForLevels)
        ? deriveLevels(levelBasis, spotForLevels, cfg)
        : null;

    const levels5m =
      fiveMin.length > 0 && Number.isFinite(spotForLevels)
        ? deriveLevels(fiveMin, spotForLevels, cfg)
        : null;

    // Tier 3: nothing derived, but the exchange gave us a real day range.
    const refs = this.history.referenceLevels();
    if (
      (levels === null || (levels.breakoutAbove === null && levels.breakdownBelow === null)) &&
      Number.isFinite(spotForLevels) &&
      (refs.dayHigh !== null || refs.dayLow !== null)
    ) {
      const buffer = params.levelConfig?.confirmationBuffer ?? 5;
      const round = params.levelConfig?.roundTo ?? 5;
      const r = (v: number) => (round > 0 ? Math.round(v / round) * round : v);

      levels = {
        spot: spotForLevels,
        atr14: null,
        toleranceUsed: 0,
        confirmationBuffer: buffer,
        resistance:
          refs.dayHigh !== null && refs.dayHigh > spotForLevels
            ? {
                price: refs.dayHigh,
                kind: 'resistance',
                touches: 1,
                sources: ['session_high'],
                structural: true,
                tolerance: 0,
                distanceFromSpot: refs.dayHigh - spotForLevels,
                distanceInAtr: null,
                note: 'Exchange day high from the futures quote (reference level).',
              }
            : null,
        support:
          refs.dayLow !== null && refs.dayLow < spotForLevels
            ? {
                price: refs.dayLow,
                kind: 'support',
                touches: 1,
                sources: ['session_low'],
                structural: true,
                tolerance: 0,
                distanceFromSpot: refs.dayLow - spotForLevels,
                distanceInAtr: null,
                note: 'Exchange day low from the futures quote (reference level).',
              }
            : null,
        breakoutAbove:
          refs.dayHigh !== null && refs.dayHigh > spotForLevels ? r(refs.dayHigh + buffer) : null,
        breakdownBelow:
          refs.dayLow !== null && refs.dayLow < spotForLevels ? r(refs.dayLow - buffer) : null,
        nextResistance: null,
        nextSupport: null,
        allResistance: [],
        allSupport: [],
        rejected: [],
        barsAnalyzed: 0,
        timeframeMinutes: null,
        diagnostics: [
          'Derived from exchange reference levels only: no candles and insufficient ' +
            'rolling history. Day high/low are real exchange values, not estimates.',
        ],
      };
      levelSource = 'reference_only';
    }

    // Reported for diagnosis, never surfaced during REFRESH.
    const levelFailure =
      levels === null || (levels.breakoutAbove === null && levels.breakdownBelow === null)
        ? candleErrors.length > 0
          ? `Dhan /v2/charts/intraday failed after 2 attempts and no fallback had enough data. ${candleErrors.join(' | ')}`
          : 'No candles, insufficient rolling history, and no exchange reference levels available.'
        : null;

    // 4. Rank strikes on BOTH sides. Direction is not decided here.
    const depthMap = new Map<string, DepthAssessment>();
    for (const d of snap.liquidity?.depth ?? []) depthMap.set(d.securityId, d);

    const posMap = new Map<string, LegPositioning>();
    for (const p of chainAnalysis.positioning) posMap.set(`${p.strike}:${p.side}`, p);

    const ce = selectStrike(
      snap.pricing.legs,
      'CE',
      forward,
      depthMap,
      posMap,
      params.selection,
    );
    const pe = selectStrike(
      snap.pricing.legs,
      'PE',
      forward,
      depthMap,
      posMap,
      params.selection,
    );

    return {
      published: true,
      underlying: snap.underlying,
      expiry: snap.expiry,
      fetchId: snap.fetchId,
      epochMs: snap.epochMs,
      pricing: {
        forward,
        T: snap.pricing.T,
        calendarDaysToExpiry: snap.pricing.calendarDaysToExpiry,
        listedFuture: snap.pricing.listedFuture,
        indexLtpDoNotUseAsSpot: snap.pricing.indexLtpDoNotUseAsSpot,
        divergenceVsFuture: snap.pricing.gate.divergenceVsFuture,
        indexDivergence: snap.pricing.gate.indexDivergence,
        perStrikeSpread: snap.pricing.forwardDetail.spread,
        atmStrike: snap.pricing.atmStrike,
        gateWarnings: snap.pricing.gate.warnings,
      },
      chain: chainAnalysis,
      priceStructure: structure,
      candleIndicators: candleData?.indicators ?? null,
      candles: {
        realCandleBars: realCandles.length,
        basisBars: levelBasis.length,
        fiveMinuteBars: fiveMin.length,
        errors: candleErrors,
      },
      /** Breakout/breakdown from 1-minute price action. */
      levels,
      /** Same derivation on 5-minute bars, for cross-checking. */
      levels5m,
      levelSource,
      levelFailure,
      rollingHistory: {
        observations: this.history.count(),
        spanMinutes: Number(this.history.spanMinutes().toFixed(1)),
      },
      spotUsedForLevels: Number.isFinite(spotForLevels) ? spotForLevels : null,
      strikeRanking: {
        lots,
        lotSize: this.registry.underlying(params.underlying).lotSize,
        ce: { best: ce.best, top: ce.ranked.slice(0, 6) },
        pe: { best: pe.best, top: pe.ranked.slice(0, 6) },
        criteriaUsed: ce.criteriaUsed,
      },
      /**
       * Entry / target / stop for each side, IF that side were taken.
       * Presence here is not a recommendation — direction is not decided.
       */
      tradePlans: {
        ce:
          levels && ce.best
            ? buildTradePlan('CE', levels, ce.best.optionPrice, ce.best.absDelta, ce.best.gamma)
            : null,
        pe:
          levels && pe.best
            ? buildTradePlan(
                'PE',
                levels,
                pe.best.optionPrice,
                // selectStrike reports |delta|; a put's delta is negative.
                pe.best.absDelta === null ? null : -pe.best.absDelta,
                pe.best.gamma,
              )
            : null,
      },
      integrity: snap.integrity,
      warnings: snap.warnings,
      note:
        'Complete analysis inputs. NO directional verdict and NO trigger level are ' +
        'produced: those rules are not defined in this server.',
    };
  }

  // --- get_candles ---------------------------------------------------------

  /**
   * Price history for the UNDERLYING, plus deterministic indicators.
   *
   * Series choice matters. The index (51 / IDX_I) is what chart levels are
   * quoted against, but it usually reports no volume, so VWAP and volume
   * confirmation need the futures series instead. Both are offered; neither is
   * ever fed into the parity forward or the integrity gate.
   */
  async getCandles(params: {
    underlying: string;
    series?: 'index' | 'futures';
    timeframe?: 'intraday' | 'daily';
    interval?: IntradayInterval;
    fromDate?: string;
    toDate?: string;
    futuresMonth?: string;
    maxCandles?: number;
  }): Promise<{
    underlying: string;
    series: 'index' | 'futures';
    timeframe: 'intraday' | 'daily';
    interval: number | null;
    securityId: string;
    segment: ExchangeSegment;
    instrument: InstrumentKind;
    fromDate: string;
    toDate: string;
    fetchId: string;
    epochMs: number;
    candles: Candle[];
    candlesReturned: number;
    candlesTotal: number;
    indicators: IndicatorSet | null;
    realisedVolAnnualised: number | null;
    notes: string[];
  }> {
    const spec = this.registry.underlying(params.underlying);
    const series = params.series ?? 'index';
    const timeframe = params.timeframe ?? 'intraday';
    const notes: string[] = [];

    let securityId: string;
    let segment: ExchangeSegment;
    let instrument: InstrumentKind;

    if (series === 'futures') {
      const fut = this.registry.futures(spec.name, params.futuresMonth);
      securityId = String(fut.securityId);
      segment = fut.segment;
      instrument = 'FUTIDX';
      notes.push(
        `Futures series (${fut.month} contract, id ${fut.securityId}). Carries volume, ` +
          'so VWAP and volume confirmation are available. Rolls at expiry.',
      );
    } else {
      securityId = String(spec.scrip);
      segment = spec.segment;
      instrument = 'INDEX';
      notes.push(
        'Index series. This is the level chart breakouts are quoted against. It ' +
          'usually reports NO volume, so VWAP and volume confirmation will be null — ' +
          'pass series="futures" for those. The index LTP remains forbidden as spot ' +
          'for option maths; using it for price structure is a separate question.',
      );
    }

    const todayIst = new Date(Date.now() + 5.5 * 3600_000).toISOString().slice(0, 10);
    const daysBack = timeframe === 'intraday' ? 5 : 120;
    const defaultFrom = new Date(Date.now() + 5.5 * 3600_000 - daysBack * 86_400_000)
      .toISOString()
      .slice(0, 10);
    const fromDate = params.fromDate ?? defaultFrom;
    // Daily toDate is NON-INCLUSIVE, so push it one day out to include today.
    const toDate =
      params.toDate ??
      (timeframe === 'daily'
        ? new Date(Date.now() + 5.5 * 3600_000 + 86_400_000).toISOString().slice(0, 10)
        : todayIst);

    const fetchId = newFetchId();
    const interval = params.interval ?? 5;

    const res =
      timeframe === 'daily'
        ? await fetchDailyHistorical(this.transport, {
            securityId,
            exchangeSegment: segment,
            instrument,
            fromDate,
            toDate,
          })
        : await fetchIntraday(this.transport, {
            securityId,
            exchangeSegment: segment,
            instrument,
            interval,
            fromDate,
            toDate,
          });

    const all = toCandles(res.data);
    if (all.length === 0) {
      notes.push(
        'No candles returned. Outside market hours, or the range covers no trading day.',
      );
    }

    // Indicators run over the FULL series; only the returned tail is trimmed.
    const indicators = computeIndicators(all);
    const cap = params.maxCandles ?? 120;
    const candles = all.length > cap ? all.slice(-cap) : all;

    return {
      underlying: spec.name,
      series,
      timeframe,
      interval: timeframe === 'intraday' ? interval : null,
      securityId,
      segment,
      instrument,
      fromDate,
      toDate,
      fetchId,
      epochMs: res.receivedAtMs,
      candles,
      candlesReturned: candles.length,
      candlesTotal: all.length,
      indicators,
      realisedVolAnnualised: realisedVolatility(all),
      notes,
    };
  }

  underlyings(): UnderlyingSpec[] {
    return this.registry.list();
  }
}
