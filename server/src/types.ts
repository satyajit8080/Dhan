/**
 * Canonical schema. Everything that leaves this server is shaped like this.
 *
 * Two invariants encoded in the types themselves:
 *   1. Vendor IV and vendor Greeks live ONLY inside `vendorQuarantined`. No
 *      pricing code accepts that type, so they cannot be read by accident.
 *   2. Every payload carries provenance: fetchId, epochMs, source.
 */

export type ExchangeSegment =
  | 'IDX_I'
  | 'NSE_EQ'
  | 'NSE_FNO'
  | 'NSE_CURRENCY'
  | 'BSE_EQ'
  | 'MCX_COMM'
  | 'BSE_CURRENCY'
  | 'BSE_FNO';

export const SEGMENT_CODES: Record<ExchangeSegment, number> = {
  IDX_I: 0,
  NSE_EQ: 1,
  NSE_FNO: 2,
  NSE_CURRENCY: 3,
  BSE_EQ: 4,
  MCX_COMM: 5,
  BSE_CURRENCY: 7,
  BSE_FNO: 8,
};

export type OptionType = 'CE' | 'PE';

/** Where a payload came from and exactly when. Stamped at normalization. */
export interface Provenance {
  /** UUID identifying ONE fetch. Two payloads sharing it are one snapshot. */
  fetchId: string;
  /** Epoch ms at which the HTTP response was received. */
  epochMs: number;
  source: 'dhan-rest-v2';
  endpoint: string;
}

export interface DepthLevel {
  price: number;
  quantity: number;
  orders: number;
}

export interface MarketDepth {
  buy: DepthLevel[];
  sell: DepthLevel[];
}

export interface OHLC {
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface CanonicalQuote {
  securityId: string;
  segment: ExchangeSegment;
  ltp: number;
  ohlc: OHLC | null;
  volume: number | null;
  oi: number | null;
  oiDayHigh: number | null;
  oiDayLow: number | null;
  averagePrice: number | null;
  buyQuantity: number | null;
  sellQuantity: number | null;
  netChange: number | null;
  upperCircuit: number | null;
  lowerCircuit: number | null;
  /** Epoch ms, or null when Dhan returned the 01/01/1980 "never traded" sentinel. */
  lastTradeTimeMs: number | null;
  depth: MarketDepth | null;
  provenance: Provenance;
  /** Fields that failed parsing, kept for diagnosis rather than discarded. */
  quarantined: Record<string, unknown>;
}

/**
 * Vendor-supplied analytics. QUARANTINED.
 *
 * Roughly 30% of chain legs return IV = 0 or IV > 60%, and the vendor computes
 * Greeks against the index LTP rather than the forward. These values are
 * carried so they can be COMPARED against ours in diagnostics, and for no
 * other purpose. Nothing in pricing/ accepts this type.
 */
export interface VendorQuarantined {
  impliedVolatility: number | null;
  greeks: {
    delta: number | null;
    gamma: number | null;
    theta: number | null;
    vega: number | null;
  } | null;
  reason: string;
}

export interface ChainLeg {
  securityId: string | null;
  lastPrice: number | null;
  oi: number | null;
  previousOi: number | null;
  volume: number | null;
  previousVolume: number | null;
  previousClosePrice: number | null;
  averagePrice: number | null;
  topBidPrice: number | null;
  topBidQuantity: number | null;
  topAskPrice: number | null;
  topAskQuantity: number | null;
  vendorQuarantined: VendorQuarantined;
}

export interface ChainStrike {
  strike: number;
  ce: ChainLeg | null;
  pe: ChainLeg | null;
}

export interface CanonicalChain {
  underlying: string;
  underlyingScrip: number;
  underlyingSeg: ExchangeSegment;
  expiry: string;
  /**
   * The chain's own `last_price`, i.e. the INDEX LTP.
   *
   * Named to make misuse obvious. This is the number that was 321 points wrong
   * on 18-Sep-2026. It is carried for the gate's carry warning and for display.
   * It is NEVER an input to option maths.
   */
  underlyingLtpDoNotUseAsSpot: number | null;
  strikes: ChainStrike[];
  provenance: Provenance;
  quarantined: Record<string, unknown>;
}

export interface IntegrityFinding {
  severity: 'block' | 'warn' | 'info';
  code: string;
  message: string;
}

export interface IntegrityReport {
  ok: boolean;
  findings: IntegrityFinding[];
  checkedAtMs: number;
}
