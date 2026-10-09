/**
 * Instrument master (scrip master) lookup.
 *
 * Two jobs:
 *   1. VERIFY the hardcoded ids in registry.ts against the exchange's own list,
 *      so "NIFTY 13 is unverified" can stop being true.
 *   2. Resolve an option or futures contract to a securityId when the option
 *      chain has not already supplied one.
 *
 * This is a plain public CSV on a different host with no authentication, so it
 * does not go through Transport and does not consume the Dhan rate budget.
 */

import { InstrumentError, TransportError } from '../errors.js';
import type { Logger } from '../config.js';

const DETAILED_CSV = 'https://images.dhan.co/api-data/api-scrip-master-detailed.csv';

export interface ScripRow {
  exchangeId: string;
  segment: string;
  securityId: string;
  instrumentName: string;
  underlyingSymbol: string;
  tradingSymbol: string;
  customSymbol: string;
  expiryDate: string;
  strikePrice: number | null;
  optionType: string;
  lotSize: number | null;
}

/** RFC-4180-ish splitter: handles quoted fields containing commas. */
function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]!;
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          cur += '"';
          i++;
        } else inQuotes = false;
      } else cur += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === ',') {
      out.push(cur);
      cur = '';
    } else cur += ch;
  }
  out.push(cur);
  return out;
}

function toNum(v: string | undefined): number | null {
  if (v === undefined || v.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export class ScripMaster {
  private rows: ScripRow[] | null = null;
  private fetchedAtMs = 0;

  constructor(
    private readonly ttlMs: number,
    private readonly log: Logger,
  ) {}

  private async load(force = false): Promise<ScripRow[]> {
    if (this.rows && !force && Date.now() - this.fetchedAtMs < this.ttlMs) {
      return this.rows;
    }

    this.log.info('Downloading Dhan instrument master (detailed CSV)');
    let text: string;
    try {
      const res = await fetch(DETAILED_CSV, { signal: AbortSignal.timeout(90_000) });
      if (!res.ok) {
        throw new TransportError(`Scrip master download failed: HTTP ${res.status}`, {
          httpStatus: res.status,
        });
      }
      text = await res.text();
    } catch (err) {
      if (err instanceof TransportError) throw err;
      throw new TransportError(
        `Could not download the instrument master: ${(err as Error).message}`,
      );
    }

    const lines = text.split(/\r?\n/);
    const header = splitCsvLine(lines[0] ?? '').map((h) => h.trim().toUpperCase());
    // Dhan renamed most columns in Sept 2026 (SEM_/SM_ prefixes dropped, some
    // fields restructured). Try the current name first, then the legacy
    // SEM_/SM_-prefixed name, so a future rename degrades to a clear error
    // on the columns we cannot do without instead of silently resolving to
    // -1 and poisoning every downstream lookup.
    const idxAny = (...names: string[]) => {
      for (const n of names) {
        const i = header.indexOf(n);
        if (i !== -1) return i;
      }
      return -1;
    };

    const iExch = idxAny('EXCH_ID', 'SEM_EXM_EXCH_ID');
    const iSeg = idxAny('SEGMENT', 'SEM_SEGMENT');
    const iSec = idxAny('SECURITY_ID', 'SEM_SMST_SECURITY_ID');
    const iInstr = idxAny('INSTRUMENT_TYPE', 'SEM_INSTRUMENT_NAME');
    const iTrad = idxAny('DISPLAY_NAME', 'SEM_TRADING_SYMBOL');
    const iCustom = idxAny('SYMBOL_NAME', 'SEM_CUSTOM_SYMBOL');
    const iExpiry = idxAny('SM_EXPIRY_DATE', 'SEM_EXPIRY_DATE');
    const iStrike = idxAny('STRIKE_PRICE', 'SEM_STRIKE_PRICE');
    const iOptType = idxAny('OPTION_TYPE', 'SEM_OPTION_TYPE');
    const iLot = idxAny('LOT_SIZE', 'SEM_LOT_UNITS');
    const iUnderlying = idxAny('UNDERLYING_SYMBOL', 'SM_SYMBOL_NAME');

    if (iSec === -1 || iExch === -1 || iSeg === -1 || iExpiry === -1) {
      throw new InstrumentError(
        'Instrument master CSV layout has changed and no known column name matched security id / exchange / segment / expiry.',
        { header: header.slice(0, 30) },
      );
    }

    const rows: ScripRow[] = [];
    for (let i = 1; i < lines.length; i++) {
      const line = lines[i];
      if (!line || !line.trim()) continue;
      const c = splitCsvLine(line);
      rows.push({
        exchangeId: (c[iExch] ?? '').trim(),
        segment: (c[iSeg] ?? '').trim(),
        securityId: (c[iSec] ?? '').trim(),
        instrumentName: (c[iInstr] ?? '').trim(),
        underlyingSymbol: (c[iUnderlying] ?? '').trim().toUpperCase(),
        tradingSymbol: (c[iTrad] ?? '').trim().toUpperCase(),
        customSymbol: (c[iCustom] ?? '').trim().toUpperCase(),
        expiryDate: (c[iExpiry] ?? '').trim(),
        strikePrice: toNum(c[iStrike]),
        optionType: (c[iOptType] ?? '').trim().toUpperCase(),
        lotSize: toNum(c[iLot]),
      });
    }

    this.rows = rows;
    this.fetchedAtMs = Date.now();
    this.log.info('Instrument master loaded', { rows: rows.length });
    return rows;
  }

  /** Free-text search, ranked by how completely the terms match. */
  async search(
    query: string,
    opts: { exchange?: string; limit?: number } = {},
  ): Promise<ScripRow[]> {
    const rows = await this.load();
    const terms = query.trim().toUpperCase().split(/\s+/).filter(Boolean);
    if (terms.length === 0) return [];
    const exch = opts.exchange?.trim().toUpperCase();

    const scored: { row: ScripRow; score: number }[] = [];
    for (const r of rows) {
      if (exch && r.exchangeId !== exch) continue;
      const hay = `${r.customSymbol} ${r.tradingSymbol} ${r.underlyingSymbol} ${r.instrumentName}`;
      let score = 0;
      for (const t of terms) if (hay.includes(t)) score++;
      if (score === terms.length) scored.push({ row: r, score });
    }

    scored.sort((a, b) => a.row.customSymbol.length - b.row.customSymbol.length);
    return scored.slice(0, opts.limit ?? 20).map((s) => s.row);
  }

  /**
   * Confirm a securityId really is what the registry claims.
   * This is how the NIFTY/BANKNIFTY "UNVERIFIED" warnings get retired.
   */
  async verify(securityId: number | string, expect: { exchange?: string; symbol?: string }): Promise<{
    found: boolean;
    row: ScripRow | null;
    matches: boolean;
    note: string;
  }> {
    const rows = await this.load();
    const id = String(securityId);
    const candidates = rows.filter(
      (r) => r.securityId === id && (!expect.exchange || r.exchangeId === expect.exchange),
    );

    if (candidates.length === 0) {
      return {
        found: false,
        row: null,
        matches: false,
        note: `securityId ${id} not present in the instrument master for the given exchange.`,
      };
    }

    const row = candidates[0]!;
    const symbolOk =
      !expect.symbol ||
      row.underlyingSymbol.includes(expect.symbol.toUpperCase()) ||
      row.customSymbol.includes(expect.symbol.toUpperCase()) ||
      row.tradingSymbol.includes(expect.symbol.toUpperCase());

    return {
      found: true,
      row,
      matches: symbolOk,
      note: symbolOk
        ? `securityId ${id} confirmed as ${row.customSymbol || row.tradingSymbol} ` +
          `(lot ${row.lotSize ?? 'unknown'}).`
        : `securityId ${id} exists but resolves to ${row.customSymbol || row.tradingSymbol}, ` +
          `not ${expect.symbol}. DO NOT USE.`,
    };
  }

  async refresh(): Promise<number> {
    const rows = await this.load(true);
    return rows.length;
  }
}
