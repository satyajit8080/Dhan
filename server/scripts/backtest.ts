/**
 * Minute-by-minute backtest of the v3.1 direction rules (RULES.md §6) on
 * SENSEX history fetched by backtest/fetch_data.py.
 *
 *   npx tsx scripts/backtest.ts <data_dir[,data_dir...]> <from YYYY-MM-DD> <to YYYY-MM-DD> <out_dir>
 *
 * Several data dirs (comma-separated) are merged; overlapping bars dedupe.
 *
 * Replays the server's own `decideDirection` — no re-implementation — on each
 * closed 1-min bar. Option premiums, IV and OI are Dhan's expired-weekly
 * history (ATM−5..ATM+5), so fills are real traded prices, not models.
 *
 * Assumptions, stated in the report:
 *   - entry at the option's next-minute open after the signal bar closes
 *   - ATM strike (delta ≈ 0.5, inside the 0.35–0.60 band), 1 lot = 20
 *   - nearest weekly; next weekly on expiry day (Thursday, else the last
 *     trading day before it)
 *   - stop: 1-min close through the stop level, filled at the next minute's
 *     option open; target / time stop / 15:15 at that minute's option close;
 *     stop wins if stop and target land in the same bar
 *   - costs: 0.3% slippage each side + ₹50 charges per round trip
 *   - one position at a time; one trade per level and side per day; done for
 *     the day after 3 losses
 *   - OI/PCR from the ±5 strikes the history carries, not the full chain
 *   - not replayable: depth / liquidity grade, quoted spread, parity gate
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { decideDirection, type OiView, type DirectionVerdict } from '../src/direction.js';
import { istTimeOf } from '../src/indicators.js';
import { loadData } from './btData.js';

const [dataDir, fromDate, toDate, outDir, configJson] = process.argv.slice(2) as [string, string, string, string, string?];
/** Optional 5th arg: JSON overrides for DirectionConfig, for testing variants. */
const configOverride = configJson ? (JSON.parse(configJson) as Record<string, unknown>) : undefined;
if (!dataDir || !fromDate || !toDate || !outDir) {
  console.error('usage: backtest.ts <data_dir> <from> <to> <out_dir>');
  process.exit(1);
}

const LOT = 20;
const SLIP = 0.003;
const CHARGES = 50;
const STEP = 100;
const MIN = 60_000;

// ---------------------------------------------------------------- data
const { opt, byDay, days, isExpiryDay } = loadData(dataDir);

function oiViewAt(t: number, code: number): OiView | null {
  const now = opt.get(t)?.get(code);
  if (!now) return null;
  let past: typeof now | undefined;
  let pastT = 0;
  for (let back = 20; back <= 30 && !past; back++) {
    past = opt.get(t - back * MIN)?.get(code);
    pastT = t - back * MIN;
  }
  for (let back = 19; back >= 15 && !past; back--) {
    past = opt.get(t - back * MIN)?.get(code);
    pastT = t - back * MIN;
  }
  const pcr = (m: typeof now) => {
    let ce = 0, pe = 0;
    for (const r of m.CE.values()) ce += r.oi;
    for (const r of m.PE.values()) pe += r.oi;
    return ce > 0 ? pe / ce : null;
  };
  const strikes = [...new Set([...now.CE.keys(), ...now.PE.keys()])].sort((a, b) => a - b);
  if (!past) return null;
  return {
    basis: 'intraday',
    minutesBack: Math.round((t - pastT) / MIN),
    strikes: strikes.map((k) => {
      const ce = now.CE.get(k), pe = now.PE.get(k);
      const pce = past!.CE.get(k), ppe = past!.PE.get(k);
      return {
        strike: k,
        ceOiChange: ce && pce ? ce.oi - pce.oi : null,
        peOiChange: pe && ppe ? pe.oi - ppe.oi : null,
      };
    }),
    pcrNow: pcr(now),
    pcrPast: pcr(past),
  };
}

// ---------------------------------------------------------------- replay
interface Trade {
  date: string; side: 'CE' | 'PE'; setup: string; entryMode: string; strike: number;
  level: number; entryTime: string; entry: number; exitTime: string; exit: number;
  exitReason: string; holdMin: number; grossPerLot: number; netPerLot: number;
  reasons: string;
}
const trades: Trade[] = [];
const blockEvents = new Map<string, number>(); // category → distinct events
const seenBlocks = new Set<string>();
let signalsNoPrice = 0;

function categorize(b: string): string | null {
  if (/No-trade window|Need at least|waiting for a retest|No breakout or rejection/.test(b)) return null;
  if (/not decisive/.test(b)) return 'A: break bar not decisive (body < 40%)';
  if (/volume/.test(b)) return 'A: volume not above 10-bar avg';
  if (/failed to hold/.test(b)) return 'A: next bar did not hold';
  if (/closed back through/.test(b)) return 'A: closed back through after hold';
  if (/within 3 pts/.test(b)) return 'Entry within 3 pts of level';
  if (/OI does not confirm|no OI/.test(b)) return 'A: OI/PCR did not confirm';
  if (/not fading a trend/.test(b)) return 'B: rejection blocked, ADX ≥ 25';
  if (/rejection/.test(b)) return 'B: other';
  if (/ADX|DI/.test(b)) return 'A: 5-min trend filter (ADX/DI)';
  if (/conflict/.test(b)) return 'CE and PE conflict';
  return 'other';
}

const inRange = days.filter((d) => d >= fromDate && d <= toDate);
const t0 = Date.now();

for (const day of inRange) {
  const di = days.indexOf(day);
  const history = days.slice(Math.max(0, di - 2), di).flatMap((d) => byDay.get(d)!);
  const today = byDay.get(day)!;
  const code = isExpiryDay(day) ? 2 : 1;
  let losses = 0;
  const traded = new Set<string>();
  let pos: {
    side: 'CE' | 'PE'; strike: number; entry: number; entryT: number; v: DirectionVerdict;
  } | null = null;

  const optBar = (t: number, side: 'CE' | 'PE', k: number) => opt.get(t)?.get(code)?.[side].get(k);

  const close = (i: number, price: number, reason: string, t: number) => {
    const p = pos!;
    const entryCost = p.entry * (1 + SLIP);
    const exitNet = price * (1 - SLIP);
    const gross = (price - p.entry) * LOT;
    const net = (exitNet - entryCost) * LOT - CHARGES;
    trades.push({
      date: day, side: p.side, setup: p.v.setup!, entryMode: p.v.entryMode!, strike: p.strike,
      level: p.v.level!, entryTime: istTimeOf(p.entryT), entry: p.entry,
      exitTime: istTimeOf(t), exit: price, exitReason: reason,
      holdMin: Math.round((t - p.entryT) / MIN), grossPerLot: Math.round(gross),
      netPerLot: Math.round(net), reasons: p.v.reasons.join(' | '),
    });
    if (net < 0) losses++;
    pos = null;
    void i;
  };

  for (let i = 0; i < today.length; i++) {
    const bar = today[i]!;
    const nowMs = bar.timestampMs + MIN; // bar i has just closed
    const time = istTimeOf(bar.timestampMs);

    if (pos) {
      const p = pos;
      const v = p.v;
      const cur = optBar(bar.timestampMs, p.side, p.strike);
      const nxt = today[i + 1] ? optBar(today[i + 1]!.timestampMs, p.side, p.strike) : undefined;
      const dir = p.side === 'CE' ? 1 : -1;
      const stopHit = dir * (bar.close - v.stopLevel!) <= 0;
      const targetHit = dir > 0 ? bar.high >= v.targetLevel! : bar.low <= v.targetLevel!;
      if (stopHit) {
        close(i, nxt?.o ?? cur?.c ?? p.entry, 'stop', nxt ? today[i + 1]!.timestampMs : bar.timestampMs);
      } else if (targetHit && cur) {
        close(i, cur.c, 'target', bar.timestampMs);
      } else if (nowMs - p.entryT >= v.timeStopMinutes * MIN && cur) {
        close(i, cur.c, 'time', bar.timestampMs);
      } else if (time >= '15:15' && cur) {
        close(i, cur.c, 'eod', bar.timestampMs);
      }
      continue;
    }

    if (losses >= 3 || time < '09:25' || time >= '14:50') continue;
    if (time >= '11:30' && time < '13:00') continue;

    const v = decideDirection({
      nowMs,
      bars1m: [...history, ...today.slice(0, i + 1)],
      oi: oiViewAt(bar.timestampMs, code),
      config: configOverride,
    });

    for (const b of v.blockers) {
      const cat = categorize(b);
      if (!cat) continue;
      const lvl = b.match(/\d{5}/)?.[0] ?? '';
      const key = `${day}|${b.slice(0, 2)}|${cat}|${lvl}`;
      if (seenBlocks.has(key)) continue;
      seenBlocks.add(key);
      blockEvents.set(cat, (blockEvents.get(cat) ?? 0) + 1);
    }

    if (!v.signal) continue;
    const tradeKey = `${v.signal}|${Math.round(v.level!)}`;
    if (traded.has(tradeKey)) continue;

    const next = today[i + 1];
    if (!next) continue;
    const strike = Math.round(bar.close / STEP) * STEP;
    const fill = optBar(next.timestampMs, v.signal, strike);
    if (!fill || !(fill.o > 0)) {
      signalsNoPrice++;
      continue;
    }
    traded.add(tradeKey);
    pos = { side: v.signal, strike, entry: fill.o, entryT: next.timestampMs, v };
  }

  if (pos) {
    const last = today[today.length - 1]!;
    const c = optBar(last.timestampMs, pos.side, pos.strike);
    close(today.length - 1, c?.c ?? pos.entry, 'eod', last.timestampMs);
  }
}

// ---------------------------------------------------------------- report
function stats(ts: Trade[]) {
  const n = ts.length;
  const wins = ts.filter((t) => t.netPerLot > 0);
  const losses = ts.filter((t) => t.netPerLot <= 0);
  const net = ts.reduce((s, t) => s + t.netPerLot, 0);
  const gross = ts.reduce((s, t) => s + t.grossPerLot, 0);
  let peak = 0, eq = 0, dd = 0;
  for (const t of ts) {
    eq += t.netPerLot;
    peak = Math.max(peak, eq);
    dd = Math.min(dd, eq - peak);
  }
  const avg = (xs: Trade[]) => (xs.length ? xs.reduce((s, t) => s + t.netPerLot, 0) / xs.length : 0);
  const gw = wins.reduce((s, t) => s + t.netPerLot, 0);
  const gl = -losses.reduce((s, t) => s + t.netPerLot, 0);
  return {
    trades: n,
    winRatePct: n ? +((wins.length / n) * 100).toFixed(1) : 0,
    netPerLot: net,
    grossPerLot: gross,
    expectancyPerTrade: n ? Math.round(net / n) : 0,
    avgWin: Math.round(avg(wins)),
    avgLoss: Math.round(avg(losses)),
    profitFactor: gl > 0 ? +(gw / gl).toFixed(2) : null,
    maxDrawdown: dd,
  };
}

const group = (k: (t: Trade) => string) => {
  const m: Record<string, ReturnType<typeof stats>> = {};
  for (const key of [...new Set(trades.map(k))].sort()) m[key] = stats(trades.filter((t) => k(t) === key));
  return m;
};

const summary = {
  configOverride: configOverride ?? null,
  period: { from: fromDate, to: toDate, sessions: inRange.length },
  overall: stats(trades),
  bySide: group((t) => t.side),
  bySetup: group((t) => `${t.setup}/${t.entryMode}`),
  byExit: group((t) => t.exitReason),
  byMonth: group((t) => t.date.slice(0, 7)),
  blockedEvents: Object.fromEntries([...blockEvents.entries()].sort((a, b) => b[1] - a[1])),
  signalsWithoutOptionPrice: signalsNoPrice,
  runtimeSec: Math.round((Date.now() - t0) / 1000),
};

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2));
const cols = Object.keys(trades[0] ?? { date: '' }) as (keyof Trade)[];
writeFileSync(
  join(outDir, 'trades.csv'),
  [cols.join(','), ...trades.map((t) => cols.map((c) => JSON.stringify(t[c] ?? '')).join(','))].join('\n'),
);
console.log(JSON.stringify(summary, null, 2));
