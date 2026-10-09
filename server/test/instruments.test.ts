/**
 * Instrument and expiry resolution (Phase 3).
 *
 * Covers the EXISTING resolution code only: the hardcoded futures registry,
 * the live expiry cache and the scrip-master CSV parser. All data here is
 * synthetic. Nothing in this file claims what Dhan's live instrument master
 * contains: real column names and value formats are UNVERIFIED (see
 * docs/PHASE3_READINESS.md §6).
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { InstrumentRegistry } from '../src/instruments/registry.js';
import { ExpiryCache, isIsoDate } from '../src/instruments/expiries.js';
import { ScripMaster } from '../src/instruments/scripmaster.js';
import { createLogger, loadSettings } from '../src/config.js';
import { InstrumentError, TransportError } from '../src/errors.js';
import type { Transport } from '../src/transport.js';

const log = createLogger({ ...loadSettings(), logLevel: 'silent' });

/** Freeze only Date (the rate limiter is not involved in these tests). */
function atIst(isoIst: string) {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(`${isoIst}+05:30`));
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Hardcoded futures registry (SEP 2026-09-24, OCT 2026-10-29, NOV 2026-11-26)
// ---------------------------------------------------------------------------

describe('futures registry — expiry-day and missing-contract behaviour', () => {
  const reg = new InstrumentRegistry(log);

  it('cross-checks an option expiring ON the future\'s expiry day against that future', () => {
    expect(reg.futuresForExpiry('SENSEX', '2026-10-29').month).toBe('OCT');
  });

  it('moves to the next contract for an option expiring the day after', () => {
    expect(reg.futuresForExpiry('SENSEX', '2026-10-30').month).toBe('NOV');
  });

  it('refuses (does not guess) when no registered future covers the option expiry', () => {
    // First weekly after the last registered contract: this is the 26-Nov cliff.
    expect(() => reg.futuresForExpiry('SENSEX', '2026-12-03')).toThrow(InstrumentError);
    expect(() => reg.futuresForExpiry('SENSEX', '2026-12-03')).toThrow(/Add the next contract/);
  });

  it('front month stays live through its own expiry day', () => {
    atIst('2026-10-29T15:00:00');
    expect(reg.futures('SENSEX').month).toBe('OCT');
  });

  it('front month rolls the day after expiry', () => {
    atIst('2026-10-30T09:15:00');
    expect(reg.futures('SENSEX').month).toBe('NOV');
  });

  it('fails loudly once every registered future has expired', () => {
    atIst('2026-11-27T09:15:00');
    expect(() => reg.futures('SENSEX')).toThrow(/Every registered SENSEX future has expired/);
  });

  it('refuses an unknown underlying instead of defaulting', () => {
    expect(() => reg.underlying('SENSEX50')).toThrow(InstrumentError);
  });
});

// ---------------------------------------------------------------------------
// Live expiry list (cached)
// ---------------------------------------------------------------------------

function cacheReturning(data: unknown) {
  const transport = {
    post: vi.fn(async () => ({ data, receivedAtMs: Date.now() })),
  } as unknown as Transport;
  return new ExpiryCache(transport, 60_000, log);
}

describe('expiry resolution', () => {
  it('on expiry day: nearest() keeps today, nextAfter() moves to the next weekly', async () => {
    const c = cacheReturning(['2026-10-15', '2026-10-08', '2026-10-22']);
    expect(await c.nearest(51, 'IDX_I', '2026-10-08')).toBe('2026-10-08');
    expect(await c.nextAfter(51, 'IDX_I', '2026-10-08')).toBe('2026-10-15');
  });

  it('sorts future expiries regardless of the order Dhan returns them', async () => {
    const c = cacheReturning(['2027-01-28', '2026-12-31', '2026-10-15']);
    expect((await c.get(51, 'IDX_I')).expiries).toEqual(['2026-10-15', '2026-12-31', '2027-01-28']);
    expect(await c.nextAfter(51, 'IDX_I', '2026-12-01')).toBe('2026-12-31');
  });

  it('ignores malformed entries instead of selecting them as the expiry', async () => {
    // Pre-fix, "N/A" sorted after every date and was returned as "nearest".
    const c = cacheReturning(['2026-10-01', 'N/A', '15-10-2026', 42, null, '2026-02-30']);
    expect((await c.get(51, 'IDX_I')).expiries).toEqual(['2026-10-01']);
    await expect(c.nearest(51, 'IDX_I', '2026-10-09')).rejects.toThrow(InstrumentError);
    await expect(c.nextAfter(51, 'IDX_I', '2026-10-09')).rejects.toThrow(InstrumentError);
  });

  it('throws when the list has no usable date at all', async () => {
    await expect(cacheReturning(['N/A']).get(51, 'IDX_I')).rejects.toThrow(/no expiries/);
    await expect(cacheReturning({ not: 'a list' }).get(51, 'IDX_I')).rejects.toThrow(/no expiries/);
  });

  it('rejects an explicit expiry that is not listed', async () => {
    const c = cacheReturning(['2026-10-15']);
    await expect(c.assertValid(51, 'IDX_I', '2026-10-16')).rejects.toThrow(/not listed/);
  });

  it('isIsoDate accepts only real calendar dates', () => {
    expect(isIsoDate('2026-10-15')).toBe(true);
    expect(isIsoDate('2028-02-29')).toBe(true);
    for (const bad of ['2026-02-30', '2026-13-01', '15-10-2026', '2026-10-15 15:30:00', '', 'N/A', 20261015, null]) {
      expect(isIsoDate(bad)).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// Scrip-master CSV parser (network stubbed; synthetic rows)
// ---------------------------------------------------------------------------

function csvResponse(text: string, status = 200) {
  return vi.fn(async () => new Response(text, { status }));
}

describe('scrip-master parsing of malformed or changed data', () => {
  // Synthetic rows. The ids and values are invented test data, NOT real
  // Dhan instruments.
  const legacyHeader =
    'SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,' +
    'SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_LOT_UNITS,SM_SYMBOL_NAME';

  it('parses the legacy SEM_* layout, including quoted commas', async () => {
    vi.stubGlobal(
      'fetch',
      csvResponse(
        `${legacyHeader}\n` +
          'BSE,D,900001,FUTIDX,TESTIDX-FUT,"TESTIDX, FUT",2099-01-29,,XX,20,TESTIDX\n' +
          '\n',
      ),
    );
    const sm = new ScripMaster(60_000, log);
    const rows = await sm.search('TESTIDX', { exchange: 'BSE' });
    expect(rows).toHaveLength(1);
    expect(rows[0]!.securityId).toBe('900001');
    expect(rows[0]!.customSymbol).toBe('TESTIDX, FUT');
    expect(rows[0]!.lotSize).toBe(20);
    expect(rows[0]!.strikePrice).toBeNull(); // blank strike is null, not 0
  });

  it('refuses a layout with no recognisable security-id column (no -1 index poisoning)', async () => {
    vi.stubGlobal('fetch', csvResponse('EXCH,SEG,ID,EXPIRY\nBSE,D,1,2099-01-29\n'));
    await expect(new ScripMaster(60_000, log).search('X')).rejects.toThrow(InstrumentError);
  });

  it('survives short / ragged rows and a non-numeric lot size', async () => {
    vi.stubGlobal(
      'fetch',
      csvResponse(`${legacyHeader}\nBSE,D,900002\nBSE,D,900003,FUTIDX,TESTIDX-FUT2,TESTIDX FUT2,2099-02-26,,XX,abc,TESTIDX\n`),
    );
    const rows = await new ScripMaster(60_000, log).search('TESTIDX FUT2');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.lotSize).toBeNull();
  });

  it('surfaces an HTTP failure as a TransportError', async () => {
    vi.stubGlobal('fetch', csvResponse('server error', 503));
    await expect(new ScripMaster(60_000, log).search('X')).rejects.toThrow(TransportError);
  });

  it('verify() flags an id that resolves to a different symbol', async () => {
    vi.stubGlobal(
      'fetch',
      csvResponse(`${legacyHeader}\nBSE,D,900001,FUTIDX,OTHER-FUT,OTHER FUT,2099-01-29,,XX,20,OTHER\n`),
    );
    const v = await new ScripMaster(60_000, log).verify(900001, { exchange: 'BSE', symbol: 'SENSEX' });
    expect(v.found).toBe(true);
    expect(v.matches).toBe(false);
    expect(v.note).toMatch(/DO NOT USE/);
  });
});

// ---------------------------------------------------------------------------
// Read-only guard vs every account-mutating path in the official SDK
// ---------------------------------------------------------------------------

describe('read-only guard covers every order/account DhanHQ-py v2.3.0 endpoint', async () => {
  const { assertReadOnlyPath } = await import('../src/transport.js');
  // Enumerated from dhan-oss/DhanHQ-py @ 8c6583e (all dhan_http.post/put/delete targets).
  const mutating = [
    '/orders', '/orders/1', '/super/orders', '/super/orders/1/TARGET_LEG', '/forever/orders',
    '/forever/orders/1', '/alerts/orders', '/alerts/orders/1', '/globalstocks/orders',
    '/globalstocks/transEstimate', '/killswitch', '/killswitch?killSwitchStatus=ACTIVATE',
    '/pnlExit', '/positions/convert', '/ip/setIP', '/ip/modifyIP', '/edis/form', '/edis/tpin',
    '/RenewToken', '/margincalculator', '/margincalculator/multi',
  ];
  it.each(mutating)('refuses %s', (p) => {
    expect(() => assertReadOnlyPath(p)).toThrow();
  });
  it.each(['/optionchain', '/optionchain/expirylist', '/marketfeed/quote', '/charts/intraday', '/charts/historical', '/profile'])(
    'still allows read-only %s',
    (p) => {
      expect(() => assertReadOnlyPath(p)).not.toThrow();
    },
  );
});
