/**
 * Opening-range breakout backtest on SENSEX weekly options — the "15-min range
 * breakout" style, with rules fixed before any result was seen:
 *
 *   npx tsx scripts/backtest_orb.ts <data_dir[,dir]> <from> <to> <out_dir> [confirm] [expiryContract]
 *
 *   confirm         15m (default): a 15-min candle (09:30, 09:45, ...) closes
 *                   outside the range; 1m: any 1-min close outside it
 *   expiryContract  expiring (default): trade the expiring weekly on expiry
 *                   day (0DTE); next: the next weekly
 *
 * Rules:
 *   - range: high/low of 09:15–09:29
 *   - close above → buy ATM CE, below → ATM PE; signals until 13:30;
 *     at most one trade per side per day, one position at a time
 *   - entry at the option's next-minute open
 *   - stop −25% of premium; once a close reaches +25%, stop moves to
 *     breakeven and then trails 15% below the highest close
 *   - stop fills at the stop, or at the bar open if it gaps through
 *   - exit at 15:15 otherwise
 *   - costs: 0.3% slippage each side + ₹50 per round trip, 1 lot (20)
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { istTimeOf } from '../src/indicators.js';
import { loadData } from './btData.js';

const [dataDir, fromDate, toDate, outDir, confirmArg, expiryArg] = process.argv.slice(2) as string[];
if (!dataDir || !fromDate || !toDate || !outDir) {
  console.error('usage: backtest_orb.ts <data_dir> <from> <to> <out_dir> [15m|1m] [expiring|next]');
  process.exit(1);
}
const confirm = confirmArg === '1m' ? '1m' : '15m';
const expiryContract = expiryArg === 'next' ? 'next' : 'expiring';

const LOT = 20;
const SLIP = 0.003;
const CHARGES = 50;
const STEP = 100;
const MIN = 60_000;
const STOP_PCT = 0.25;
const TRAIL_TRIGGER = 0.25;
const TRAIL_PCT = 0.15;

const { opt, byDay, days, isExpiryDay } = loadData(dataDir);

interface Trade {
  date: string; expiryDay: boolean; side: 'CE' | 'PE'; strike: number;
  rangeHigh: number; rangeLow: number; signalTime: string; entryTime: string;
  entry: number; exitTime: string; exit: number; exitReason: string;
  peakPct: number; grossPerLot: number; netPerLot: number;
}
const trades: Trade[] = [];
let noRangeDays = 0;
let missingPrice = 0;

const inRange = days.filter((d) => d >= fromDate && d <= toDate);
for (const day of inRange) {
  const today = byDay.get(day)!;
  const expiry = isExpiryDay(day);
  const code = expiry && expiryContract === 'next' ? 2 : 1;
  const orBars = today.filter((b) => istTimeOf(b.timestampMs) < '09:30');
  if (orBars.length < 15) {
    noRangeDays++;
    continue;
  }
  const hi = Math.max(...orBars.map((b) => b.high));
  const lo = Math.min(...orBars.map((b) => b.low));
  const optBar = (t: number, side: 'CE' | 'PE', k: number) => opt.get(t)?.get(code)?.[side].get(k);

  const done = new Set<'CE' | 'PE'>();
  let pos: {
    side: 'CE' | 'PE'; strike: number; entry: number; entryT: number; signalT: number;
    stop: number; peak: number; trailing: boolean; lastClose: number;
  } | null = null;

  const close = (price: number, t: number, reason: string) => {
    const p = pos!;
    const gross = (price - p.entry) * LOT;
    const net = (price * (1 - SLIP) - p.entry * (1 + SLIP)) * LOT - CHARGES;
    trades.push({
      date: day, expiryDay: expiry, side: p.side, strike: p.strike, rangeHigh: hi, rangeLow: lo,
      signalTime: istTimeOf(p.signalT), entryTime: istTimeOf(p.entryT), entry: p.entry,
      exitTime: istTimeOf(t), exit: +price.toFixed(2), exitReason: reason,
      peakPct: +((p.peak / p.entry - 1) * 100).toFixed(1),
      grossPerLot: Math.round(gross), netPerLot: Math.round(net),
    });
    pos = null;
  };

  for (let i = 15; i < today.length; i++) {
    const bar = today[i]!;
    const time = istTimeOf(bar.timestampMs);

    if (pos) {
      if (bar.timestampMs < pos.entryT) continue;
      const o = optBar(bar.timestampMs, pos.side, pos.strike);
      if (o) {
        if (o.l <= pos.stop) {
          const reason = pos.trailing ? (pos.stop > pos.entry ? 'trail' : 'breakeven') : 'stop';
          close(Math.min(pos.stop, o.o), bar.timestampMs, reason);
          continue;
        }
        pos.lastClose = o.c;
        if (o.c > pos.peak) pos.peak = o.c;
        if (!pos.trailing && o.c >= pos.entry * (1 + TRAIL_TRIGGER)) pos.trailing = true;
        if (pos.trailing) pos.stop = Math.max(pos.entry, pos.peak * (1 - TRAIL_PCT));
      }
      if (time >= '15:15') close(o?.c ?? pos.lastClose, bar.timestampMs, 'eod');
      continue;
    }

    if (time > '13:30') continue;
    // A 15-min candle from 09:30 closes on its 15th minute: 09:44, 09:59, ...
    const minuteOfSession = Number(time.slice(0, 2)) * 60 + Number(time.slice(3, 5)) - (9 * 60 + 15);
    if (confirm === '15m' && (minuteOfSession + 1) % 15 !== 0) continue;

    const side = bar.close > hi ? 'CE' : bar.close < lo ? 'PE' : null;
    if (!side || done.has(side)) continue;
    const next = today[i + 1];
    if (!next) continue;
    const strike = Math.round(bar.close / STEP) * STEP;
    const fill = optBar(next.timestampMs, side, strike);
    if (!fill || !(fill.o > 0)) {
      missingPrice++;
      continue;
    }
    done.add(side);
    pos = {
      side, strike, entry: fill.o, entryT: next.timestampMs, signalT: bar.timestampMs,
      stop: fill.o * (1 - STOP_PCT), peak: fill.o, trailing: false, lastClose: fill.o,
    };
  }
  if (pos) {
    const last = today[today.length - 1]!;
    close(pos.lastClose, last.timestampMs, 'eod');
  }
}

// ---------------------------------------------------------------- report
function stats(ts: Trade[]) {
  const n = ts.length;
  const wins = ts.filter((t) => t.netPerLot > 0);
  const losses = ts.filter((t) => t.netPerLot <= 0);
  const net = ts.reduce((s, t) => s + t.netPerLot, 0);
  let peak = 0, eq = 0, dd = 0;
  for (const t of ts) {
    eq += t.netPerLot;
    peak = Math.max(peak, eq);
    dd = Math.min(dd, eq - peak);
  }
  const gw = wins.reduce((s, t) => s + t.netPerLot, 0);
  const gl = -losses.reduce((s, t) => s + t.netPerLot, 0);
  return {
    trades: n,
    winRatePct: n ? +((wins.length / n) * 100).toFixed(1) : 0,
    netPerLot: net,
    grossPerLot: ts.reduce((s, t) => s + t.grossPerLot, 0),
    expectancyPerTrade: n ? Math.round(net / n) : 0,
    avgWin: wins.length ? Math.round(gw / wins.length) : 0,
    avgLoss: losses.length ? Math.round(-gl / losses.length) : 0,
    profitFactor: gl > 0 ? +(gw / gl).toFixed(2) : null,
    maxDrawdown: dd,
  };
}
const group = (k: (t: Trade) => string) => {
  const m: Record<string, ReturnType<typeof stats>> = {};
  for (const key of [...new Set(trades.map(k))].sort()) m[key] = stats(trades.filter((t) => k(t) === key));
  return m;
};

const dayPnl = new Map<string, number>();
for (const t of trades) dayPnl.set(t.date, (dayPnl.get(t.date) ?? 0) + t.netPerLot);

const summary = {
  strategy: { confirm, expiryContract, stopPct: STOP_PCT, trailTrigger: TRAIL_TRIGGER, trailPct: TRAIL_PCT },
  period: { from: fromDate, to: toDate, sessions: inRange.length, noRangeDays },
  overall: stats(trades),
  byDayType: group((t) => (t.expiryDay ? 'expiry' : 'non-expiry')),
  bySide: group((t) => t.side),
  byExit: group((t) => t.exitReason),
  byMonth: group((t) => t.date.slice(0, 7)),
  days: { traded: dayPnl.size, green: [...dayPnl.values()].filter((v) => v > 0).length },
  signalsWithoutOptionPrice: missingPrice,
};

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2));
const cols = Object.keys(trades[0] ?? { date: '' }) as (keyof Trade)[];
writeFileSync(
  join(outDir, 'trades.csv'),
  [cols.join(','), ...trades.map((t) => cols.map((c) => JSON.stringify(t[c] ?? '')).join(','))].join('\n'),
);
console.log(JSON.stringify(summary, null, 2));
