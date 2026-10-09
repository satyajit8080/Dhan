#!/usr/bin/env node
/**
 * MCP stdio shell.
 *
 * Every tool here is read-only market data. There is no order tool, and the
 * transport layer refuses order-shaped paths even if one were added by mistake.
 *
 * Responses are DIGESTS, not raw dumps: a full SENSEX chain is far too large to
 * put in a model's context verbatim, and the useful part is the near-ATM band
 * plus the integrity block. Raw legs remain available via get_option_chain.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

import { Bull50DhanClient } from './client.js';
import { Bull50DhanError } from './errors.js';
import { digestSnapshot, round } from './digest.js';
import { deriveLevels, aggregateCandles } from './levels.js';
import type { ExchangeSegment } from './types.js';

const client = new Bull50DhanClient();

const SEGMENTS = [
  'IDX_I',
  'NSE_EQ',
  'NSE_FNO',
  'NSE_CURRENCY',
  'BSE_EQ',
  'MCX_COMM',
  'BSE_CURRENCY',
  'BSE_FNO',
] as const;

type ToolResult = {
  content: { type: 'text'; text: string }[];
  isError?: boolean;
};

function ok(payload: unknown): ToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] };
}

function fail(err: unknown): ToolResult {
  if (err instanceof Bull50DhanError) {
    return {
      content: [{ type: 'text', text: JSON.stringify(err.toJSON(), null, 2) }],
      isError: true,
    };
  }
  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify(
          { error: 'UnexpectedError', message: (err as Error).message },
          null,
          2,
        ),
      },
    ],
    isError: true,
  };
}

/** Wrap a handler so no tool can throw past the protocol boundary. */
function guard<A>(fn: (args: A) => Promise<ToolResult>) {
  return async (args: A): Promise<ToolResult> => {
    try {
      return await fn(args);
    } catch (err) {
      return fail(err);
    }
  };
}

const server = new McpServer({
  name: 'bull50-dhan',
  version: '1.0.0',
});

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------

server.registerTool(
  'set_dhan_token',
  {
    title: 'Set the daily Dhan access token',
    description:
      'Supply the Dhan access token for this session. Held in memory only, never ' +
      'written to disk, and redacted from every log line. Dhan tokens expire daily, ' +
      'so this is the once-a-morning step.',
    inputSchema: {
      token: z.string().min(16).describe('The Dhan access token (JWT).'),
      client_id: z
        .string()
        .optional()
        .describe('Dhan client id. Optional if DHAN_CLIENT_ID is already set.'),
    },
  },
  guard(async ({ token, client_id }: { token: string; client_id?: string }) => {
    const status = client.setToken(token, client_id);
    return ok({
      accepted: true,
      source: status.source,
      fingerprint: status.fingerprint,
      client_id: status.clientId,
      expires_at: status.expiresAtIso,
      hours_until_expiry:
        status.msUntilExpiry === null ? null : round(status.msUntilExpiry / 3_600_000, 2),
      note:
        'Token stored in memory for this server process only. ' +
        'Security note: the token now also exists in your chat transcript.',
    });
  }),
);

server.registerTool(
  'dhan_token_status',
  {
    title: 'Check token freshness',
    description:
      'Report whether a usable Dhan token is present, where it came from, and how ' +
      'long until it expires. Never returns the token itself.',
    inputSchema: {},
  },
  guard(async () => {
    const s = client.tokenStatus();
    return ok({
      present: s.present,
      source: s.source,
      fingerprint: s.fingerprint,
      client_id: s.clientId,
      expires_at: s.expiresAtIso,
      expired: s.expired,
      hours_until_expiry:
        s.msUntilExpiry === null ? null : round(s.msUntilExpiry / 3_600_000, 2),
      rate_limit: client.rateLimitStats(),
    });
  }),
);

// ---------------------------------------------------------------------------
// Market data
// ---------------------------------------------------------------------------

server.registerTool(
  'get_quote',
  {
    title: 'Full quote with 5-level depth',
    description:
      'LTP, OHLC, volume, OI, 5-level market depth and last_trade_time for one ' +
      'instrument. This is the only endpoint that returns depth.',
    inputSchema: {
      security_id: z.union([z.string(), z.number()]).describe('Dhan security id.'),
      segment: z.enum(SEGMENTS).describe('Exchange segment, e.g. BSE_FNO.'),
    },
  },
  guard(async ({ security_id, segment }: { security_id: string | number; segment: ExchangeSegment }) => {
    const { quote, integrity } = await client.getQuote(security_id, segment);
    return ok({
      fetch_id: quote.provenance.fetchId,
      epoch_ms: quote.provenance.epochMs,
      source: quote.provenance.source,
      quote: {
        security_id: quote.securityId,
        segment: quote.segment,
        ltp: quote.ltp,
        ohlc: quote.ohlc,
        volume: quote.volume,
        oi: quote.oi,
        oi_day_high: quote.oiDayHigh,
        oi_day_low: quote.oiDayLow,
        average_price: quote.averagePrice,
        buy_quantity: quote.buyQuantity,
        sell_quantity: quote.sellQuantity,
        net_change: quote.netChange,
        upper_circuit: quote.upperCircuit,
        lower_circuit: quote.lowerCircuit,
        last_trade_time_ms: quote.lastTradeTimeMs,
        depth: quote.depth,
      },
      integrity,
    });
  }),
);

server.registerTool(
  'get_option_chain',
  {
    title: 'Normalized option chain',
    description:
      'CE/PE by strike with top-of-book, OI and volume. Vendor IV and vendor Greeks ' +
      'are quarantined and must not be used. Expiry is resolved live if omitted.',
    inputSchema: {
      underlying: z.string().describe('SENSEX, NIFTY or BANKNIFTY.'),
      expiry: z.string().optional().describe('YYYY-MM-DD. Omit for the nearest listed expiry.'),
      max_strikes: z
        .number()
        .int()
        .positive()
        .optional()
        .describe('Cap the strikes returned, centred on the chain. Default 40.'),
    },
  },
  guard(async ({ underlying, expiry, max_strikes }: { underlying: string; expiry?: string; max_strikes?: number }) => {
    const { chain, integrity, notice } = await client.getOptionChain(underlying, expiry);
    const cap = max_strikes ?? 40;
    let strikes = chain.strikes;
    if (strikes.length > cap) {
      const start = Math.max(0, Math.floor((strikes.length - cap) / 2));
      strikes = strikes.slice(start, start + cap);
    }
    return ok({
      fetch_id: chain.provenance.fetchId,
      epoch_ms: chain.provenance.epochMs,
      source: chain.provenance.source,
      underlying: chain.underlying,
      expiry: chain.expiry,
      index_ltp_do_not_use_as_spot: chain.underlyingLtpDoNotUseAsSpot,
      strikes_returned: strikes.length,
      strikes_total: chain.strikes.length,
      strikes,
      integrity,
      notice,
    });
  }),
);

server.registerTool(
  'get_expiries',
  {
    title: 'Available expiries',
    description:
      'Live expiry list for an underlying, cached with a TTL. Expiries are never ' +
      'hardcoded — weekly expiries shift on exchange holidays.',
    inputSchema: {
      underlying: z.string().describe('SENSEX, NIFTY or BANKNIFTY.'),
      force_refresh: z.boolean().optional().describe('Bypass the cache.'),
    },
  },
  guard(async ({ underlying, force_refresh }: { underlying: string; force_refresh?: boolean }) =>
    ok(await client.getExpiries(underlying, force_refresh ?? false)),
  ),
);

server.registerTool(
  'get_instrument',
  {
    title: 'Resolve a security id',
    description:
      'Search the Dhan instrument master, e.g. "SENSEX 24 SEP 74500 CALL" with ' +
      'exchange BSE. Note: option legs in get_market_snapshot already carry their ' +
      'own security_id, so this is mainly for verification.',
    inputSchema: {
      query: z.string().describe('Free-text, e.g. "SENSEX 24 SEP 74500 CALL".'),
      exchange: z.string().optional().describe('BSE, NSE, MCX.'),
      limit: z.number().int().positive().optional(),
    },
  },
  guard(async ({ query, exchange, limit }: { query: string; exchange?: string; limit?: number }) => {
    const rows = await client.getInstrument(query, exchange, limit ?? 20);
    return ok({ query, exchange: exchange ?? null, count: rows.length, results: rows });
  }),
);

server.registerTool(
  'get_futures_quote',
  {
    title: 'Futures quote for the parity cross-check',
    description:
      'Futures LTP, OI and depth. This is the reference the integrity gate checks ' +
      'the parity forward against.',
    inputSchema: {
      underlying: z.string().describe('SENSEX.'),
      month: z.string().optional().describe('SEP, OCT or NOV. Defaults to the front month.'),
    },
  },
  guard(async ({ underlying, month }: { underlying: string; month?: string }) => {
    const r = await client.getFuturesQuote(underlying, month);
    return ok({
      fetch_id: r.quote.provenance.fetchId,
      epoch_ms: r.quote.provenance.epochMs,
      source: r.quote.provenance.source,
      contract: r.contract,
      ltp: r.quote.ltp,
      oi: r.quote.oi,
      volume: r.quote.volume,
      last_trade_time_ms: r.quote.lastTradeTimeMs,
      depth: r.quote.depth,
      integrity: r.integrity,
    });
  }),
);

server.registerTool(
  'get_market_snapshot',
  {
    title: 'Atomic gated snapshot — chain + futures + P0 pricing',
    description:
      'The main tool. Fetches the chain FIRST, then the futures cross-check, enforces ' +
      'the single-timestamp rule, recovers the forward from put-call parity, runs the ' +
      'integrity gate, and computes Black-76 IV and Greeks internally. Publishes ' +
      'nothing if the gate blocks — it never falls back to the index LTP.',
    inputSchema: {
      underlying: z.string().describe('SENSEX, NIFTY or BANKNIFTY.'),
      expiry: z
        .string()
        .optional()
        .describe('YYYY-MM-DD. Omit for the nearest expiry after today (next weekly on expiry day).'),
      include_depth: z
        .boolean()
        .optional()
        .describe('Run Stage-2 depth on screened candidates. One extra quote call.'),
      lots: z.number().int().positive().optional().describe('Size for depth costing. Default 1.'),
      max_candidates: z.number().int().positive().optional(),
      band_strikes: z
        .number()
        .positive()
        .optional()
        .describe('Half-width, in index points, of the reported near-ATM band. Default 500.'),
    },
  },
  guard(
    async (args: {
      underlying: string;
      expiry?: string;
      include_depth?: boolean;
      lots?: number;
      max_candidates?: number;
      band_strikes?: number;
    }) => {
      const snap = await client.getMarketSnapshot(args.underlying, {
        expiry: args.expiry,
        includeDepth: args.include_depth ?? false,
        lots: args.lots ?? 1,
        maxCandidates: args.max_candidates ?? 12,
      });
      return ok(digestSnapshot(snap, args.band_strikes ?? 500, client.rateLimitStats()));
    },
  ),
);

server.registerTool(
  'compute_levels',
  {
    title: 'Breakout/breakdown levels from EXTERNALLY supplied candles',
    description:
      'Runs the same confirmed-level engine as get_analysis — clustering, touch ' +
      'counting, spike rejection, buffer — but on candles you pass in, from ANY ' +
      'source. Use this when the Dhan candle endpoint is unavailable but another ' +
      'connector (for example an INDmoney OHLC tool) can supply SENSEX bars. ' +
      'Accepts either datetime_ist strings or epoch timestamps. Returns confirmed ' +
      'resistance/support, breakoutAbove and breakdownBelow. Emits no verdict.',
    inputSchema: {
      candles: z
        .array(
          z.object({
            datetime_ist: z
              .string()
              .optional()
              .describe('"YYYY-MM-DD HH:MM:SS" IST wall clock.'),
            timestamp: z.number().optional().describe('Epoch seconds or milliseconds.'),
            open: z.number(),
            high: z.number(),
            low: z.number(),
            close: z.number(),
            volume: z.number().nullable().optional(),
          }),
        )
        .min(2)
        .describe('Candles, any order; sorted internally.'),
      spot: z
        .number()
        .optional()
        .describe('Live underlying level. Defaults to the last candle close.'),
      confirmation_buffer: z.number().optional().describe('Points added/subtracted. Default 5.'),
      min_touches: z.number().int().positive().optional().describe('Default 2.'),
      round_to: z.number().optional().describe('Round the trigger to this step. Default 5.'),
      tolerance: z.number().optional().describe('Cluster half-width in points. Auto if omitted.'),
    },
  },
  guard(
    async (args: {
      candles: {
        datetime_ist?: string;
        timestamp?: number;
        open: number;
        high: number;
        low: number;
        close: number;
        volume?: number | null;
      }[];
      spot?: number;
      confirmation_buffer?: number;
      min_touches?: number;
      round_to?: number;
      tolerance?: number;
    }) => {
      const IST_OFFSET = (5 * 60 + 30) * 60 * 1000;
      const parsed = args.candles.map((c, i) => {
        let ms: number;
        if (typeof c.timestamp === 'number' && Number.isFinite(c.timestamp)) {
          // Seconds or milliseconds, disambiguated by magnitude.
          ms = c.timestamp > 1e11 ? c.timestamp : c.timestamp * 1000;
        } else if (c.datetime_ist) {
          const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/.exec(
            c.datetime_ist.trim(),
          );
          if (!m) throw new Error(`candles[${i}].datetime_ist unparseable: ${c.datetime_ist}`);
          const [, y, mo, d, h, mi, s] = m;
          ms =
            Date.UTC(
              Number(y),
              Number(mo) - 1,
              Number(d),
              Number(h),
              Number(mi),
              Number(s ?? 0),
            ) - IST_OFFSET;
        } else {
          throw new Error(`candles[${i}] needs datetime_ist or timestamp`);
        }
        return {
          timestampMs: ms,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
          volume: c.volume ?? null,
          openInterest: null,
        };
      });

      parsed.sort((a, b) => a.timestampMs - b.timestampMs);
      const spot = args.spot ?? parsed[parsed.length - 1]!.close;

      const cfg = {
        confirmationBuffer: args.confirmation_buffer ?? 5,
        minTouches: args.min_touches ?? 2,
        roundTo: args.round_to ?? 5,
        ...(args.tolerance !== undefined ? { tolerance: args.tolerance } : {}),
      };

      const oneMin = deriveLevels(parsed, spot, cfg);
      const five = aggregateCandles(parsed, 5);
      const fiveMin = five.length > 1 ? deriveLevels(five, spot, cfg) : null;

      return ok({
        source: 'externally-supplied candles',
        spot,
        bars: parsed.length,
        first_bar_ms: parsed[0]!.timestampMs,
        last_bar_ms: parsed[parsed.length - 1]!.timestampMs,
        levels: oneMin,
        levels_aggregated_5: fiveMin,
        note:
          'Same engine as get_analysis. Levels come from the supplied price action ' +
          'only. No directional verdict is produced.',
      });
    },
  ),
);

server.registerTool(
  'get_analysis',
  {
    title: 'Complete options-market analysis (no verdict)',
    description:
      'One call assembling everything a decision rule could need: gated parity ' +
      'pricing and Greeks, executable liquidity at size, full chain positioning ' +
      '(PCR, max pain, OI buildup, OI concentration, support/resistance peaks), ' +
      'price structure (swings, higher-highs/lower-lows, consolidation, candidate ' +
      'breakout levels), chart indicators, and ranked strike candidates for BOTH ' +
      'CE and PE. Produces NO directional verdict and NO trigger level, because ' +
      'those rules are not configured. Use this as the internal analysis step.',
    inputSchema: {
      underlying: z.string().describe('SENSEX, NIFTY or BANKNIFTY.'),
      expiry: z
        .string()
        .optional()
        .describe('YYYY-MM-DD. Omit for the nearest expiry after today (next weekly on expiry day).'),
      lots: z.number().int().positive().optional().describe('Size for liquidity. Default 5.'),
      series: z
        .enum(['index', 'futures'])
        .optional()
        .describe('Candle series for structure. futures gives volume and VWAP.'),
      interval: z
        .union([z.literal(1), z.literal(5), z.literal(15), z.literal(25), z.literal(60)])
        .optional()
        .describe('Intraday minutes. Default 1 (5-minute levels are derived from it).'),
    },
  },
  guard(
    async (args: {
      underlying: string;
      expiry?: string;
      lots?: number;
      series?: 'index' | 'futures';
      interval?: 1 | 5 | 15 | 25 | 60;
    }) => ok(await client.getAnalysis(args)),
  ),
);

server.registerTool(
  'get_candles',
  {
    title: 'Underlying price history with computed indicators',
    description:
      'Intraday or daily candles for the UNDERLYING, plus deterministic indicators ' +
      'computed server-side: EMA 9/20/50/200, ADX with +DI/-DI, ATR(14), RSI(14), ' +
      'VWAP, rate-of-change momentum, relative volume, session high/low, previous ' +
      'session high/low, opening range, and recent swing ranges. Use series="futures" ' +
      'when you need VWAP or volume — the index series usually reports neither. ' +
      'This is price data only: it computes no trend verdict, no bias and no breakout ' +
      'level, because those rules are not defined yet.',
    inputSchema: {
      underlying: z.string().describe('SENSEX, NIFTY or BANKNIFTY.'),
      series: z
        .enum(['index', 'futures'])
        .optional()
        .describe('index (default) for chart levels; futures for volume and VWAP.'),
      timeframe: z.enum(['intraday', 'daily']).optional().describe('Default intraday.'),
      interval: z
        .union([z.literal(1), z.literal(5), z.literal(15), z.literal(25), z.literal(60)])
        .optional()
        .describe('Intraday minutes: 1, 5, 15, 25 or 60. Default 5.'),
      from_date: z.string().optional().describe('YYYY-MM-DD. Default 5 days back intraday, 120 daily.'),
      to_date: z.string().optional().describe('YYYY-MM-DD. Default today.'),
      futures_month: z.string().optional().describe('SEP, OCT, NOV when series=futures.'),
      max_candles: z
        .number()
        .int()
        .positive()
        .optional()
        .describe('Cap on candles returned (newest kept). Indicators always use the full series. Default 120.'),
    },
  },
  guard(
    async (args: {
      underlying: string;
      series?: 'index' | 'futures';
      timeframe?: 'intraday' | 'daily';
      interval?: 1 | 5 | 15 | 25 | 60;
      from_date?: string;
      to_date?: string;
      futures_month?: string;
      max_candles?: number;
    }) => {
      const r = await client.getCandles({
        underlying: args.underlying,
        series: args.series,
        timeframe: args.timeframe,
        interval: args.interval,
        fromDate: args.from_date,
        toDate: args.to_date,
        futuresMonth: args.futures_month,
        maxCandles: args.max_candles,
      });

      return ok({
        fetch_id: r.fetchId,
        epoch_ms: r.epochMs,
        source: 'dhan-rest-v2',
        underlying: r.underlying,
        series: r.series,
        timeframe: r.timeframe,
        interval_minutes: r.interval,
        security_id: r.securityId,
        segment: r.segment,
        instrument: r.instrument,
        from_date: r.fromDate,
        to_date: r.toDate,
        candles_returned: r.candlesReturned,
        candles_total: r.candlesTotal,
        indicators: r.indicators,
        realised_vol_annualised: r.realisedVolAnnualised,
        candles: r.candles,
        notes: r.notes,
        disclaimer:
          'Price data only. No trend verdict, no directional bias and no breakout ' +
          'level are produced here — those rules are not defined yet.',
      });
    },
  ),
);

// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  client.log.info('bull50-dhan MCP server ready (read-only market data)');
}

main().catch((err) => {
  process.stderr.write(`[bull50-dhan] fatal: ${(err as Error).message}\n`);
  process.exit(1);
});
