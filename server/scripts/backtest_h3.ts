/**
 * Pre-registered H3: hedged expiry-day iron fly, as written in
 * backtest/PREREGISTRATION.md (commit c65d51c, before this file).
 *
 *   npx tsx scripts/backtest_h3.ts <data_dir[,dir]> <from> <to> <out_dir>
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { istTimeOf } from '../src/indicators.js';
import { loadData, type OptRow } from './btData.js';

const [dataDir, fromDate, toDate, outDir] = process.argv.slice(2) as string[];
if (!dataDir || !fromDate || !toDate || !outDir) {
  console.error('usage: backtest_h3.ts <data_dir> <from> <to> <out_dir>');
  process.exit(1);
}

const LOT = 20;
const STEP = 100;
const MAX_WING = 500;
const CONTROL_RUNS = 1000;
type Side = 'CE' | 'PE';
type Scenario = 'base' | 'stress';

const { opt, byDay, days } = loadData(dataDir);
const optAt = (t: number, side: Side, k: number): OptRow | undefined => opt.get(t)?.get(1)?.[side].get(k);

/**
 * POST-HOC SENSITIVITY ONLY (HALF_SPREAD_PCT): overrides the base half-spread,
 * e.g. 0.0013 = the median measured in the 30 Oct 2026 live log (a non-expiry
 * day). It never changes the registered verdict.
 */
const OVERRIDE = process.env['HALF_SPREAD_PCT'] ? Number(process.env['HALF_SPREAD_PCT']) : null;
const halfSpread = (p: number, s: Scenario) =>
  Math.max((s === 'base' ? (OVERRIDE ?? 0.01) : 0.015) * p, 0.1);

function orderCharges(price: number, kind: 'buy' | 'sell', date: string): number {
  const turnover = Math.max(price, 0) * LOT;
  const exchange = 0.000325 * turnover;
  const sebi = (turnover * 10) / 1e7;
  const gst = 0.18 * (20 + exchange + sebi);
  const stamp = kind === 'buy' ? 0.00003 * turnover : 0;
  const stt = kind === 'sell' ? (date < '2026-04-01' ? 0.001 : 0.0015) * turnover : 0;
  return 20 + exchange + sebi + gst + stamp + stt;
}

// ---------------------------------------------------------------- expiry days
function isExpiry(d: string): boolean {
  const bars = byDay.get(d)!;
  for (let i = bars.length - 1; i >= 0; i--) {
    const b = bars[i]!;
    if (istTimeOf(b.timestampMs) < '15:25') return false;
    const k = Math.round(b.close / STEP) * STEP;
    const ce = optAt(b.timestampMs, 'CE', k);
    const pe = optAt(b.timestampMs, 'PE', k);
    if (ce && pe) return Math.min(ce.c, pe.c) <= 1 || ce.c + pe.c - Math.abs(b.close - k) <= 50;
  }
  return false;
}

// ---------------------------------------------------------------- the trade
interface Leg { side: Side; k: number; dir: -1 | 1 } // −1 short, +1 long
interface DayResult {
  day: string; entryTime: string; K: number; W: number; credit: number;
  exitTime: string; reason: string; net: number; gross: number;
}

/** Iron fly set up on the bar at `setupIdx`, entered on the next bar's open. */
function runFly(day: string, setupIdx: number, s: Scenario): DayResult | { skip: string } {
  const bars = byDay.get(day)!;
  const setup = bars[setupIdx];
  const entryBar = bars[setupIdx + 1];
  if (!setup || !entryBar) return { skip: 'no bars' };
  const K = Math.round(setup.close / STEP) * STEP;
  const ceS = optAt(setup.timestampMs, 'CE', K);
  const peS = optAt(setup.timestampMs, 'PE', K);
  if (!ceS || !peS) return { skip: 'no ATM price at setup' };
  const W = ceS.c + peS.c;
  const up = Math.round((K + W) / STEP) * STEP;
  const dn = Math.round((K - W) / STEP) * STEP;
  if (up - K > MAX_WING || K - dn > MAX_WING) return { skip: 'wing beyond data band' };

  const legs: Leg[] = [
    { side: 'CE', k: K, dir: -1 }, { side: 'PE', k: K, dir: -1 },
    { side: 'CE', k: up, dir: 1 }, { side: 'PE', k: dn, dir: 1 },
  ];
  const opens = legs.map((l) => optAt(entryBar.timestampMs, l.side, l.k)?.o);
  if (opens.some((x) => x === undefined || !(x > 0))) return { skip: 'leg missing at entry' };

  // Entry fills: shorts receive price − h, longs pay price + h.
  const entryFill = legs.map((l, i) => {
    const p = opens[i]!;
    return l.dir < 0 ? p - halfSpread(p, s) : p + halfSpread(p, s);
  });
  const credit = legs.reduce((a, l, i) => a + (l.dir < 0 ? entryFill[i]! : -entryFill[i]!), 0);
  let charges = legs.reduce((a, l, i) => a + orderCharges(entryFill[i]!, l.dir < 0 ? 'sell' : 'buy', day), 0);
  const last = [...opens] as number[];

  const exitAt = (prices: number[], t: number, reason: string): DayResult => {
    // Exit fills: shorts are bought back at price + h, longs sold at price − h.
    const exitFill = legs.map((l, i) => {
      const p = prices[i]!;
      return l.dir < 0 ? p + halfSpread(p, s) : Math.max(p - halfSpread(p, s), 0);
    });
    charges += legs.reduce((a, l, i) => a + orderCharges(exitFill[i]!, l.dir < 0 ? 'buy' : 'sell', day), 0);
    const closeCost = legs.reduce((a, l, i) => a + (l.dir < 0 ? exitFill[i]! : -exitFill[i]!), 0);
    const gross = (credit - closeCost) * LOT;
    return {
      day, entryTime: istTimeOf(entryBar.timestampMs), K, W: +W.toFixed(1), credit: +credit.toFixed(2),
      exitTime: istTimeOf(t), reason, net: Math.round(gross - charges), gross: Math.round(gross),
    };
  };

  for (let i = setupIdx + 1; i < bars.length; i++) {
    const b = bars[i]!;
    legs.forEach((l, j) => {
      const o = optAt(b.timestampMs, l.side, l.k);
      if (o) last[j] = o.c;
    });
    const time = istTimeOf(b.timestampMs);
    if (time >= '15:10' || i === bars.length - 1) return exitAt(last, b.timestampMs, 'flat');
    const markCost = legs.reduce((a, l, j) => a + (l.dir < 0 ? last[j]! : -last[j]!), 0);
    if (markCost - credit >= credit) {
      const nb = bars[i + 1]!;
      const px = legs.map((l, j) => optAt(nb.timestampMs, l.side, l.k)?.o ?? last[j]!);
      return exitAt(px, nb.timestampMs, 'stop');
    }
  }
  return { skip: 'ran out of bars' };
}

// ---------------------------------------------------------------- run
const inRange = days.filter((d) => d >= fromDate && d <= toDate);
const expiryDays = inRange.filter(isExpiry);
const setupIdxOf = (d: string, hm: string) => byDay.get(d)!.findIndex((b) => istTimeOf(b.timestampMs) === hm);

function runAll(s: Scenario) {
  const rows: DayResult[] = [];
  const skips: Record<string, string> = {};
  for (const d of expiryDays) {
    const i = setupIdxOf(d, '09:44');
    const r = i < 0 ? { skip: 'no 09:44 bar' } : runFly(d, i, s);
    if ('skip' in r) skips[d] = r.skip;
    else rows.push(r);
  }
  return { rows, skips };
}

const base = runAll('base');
const stress = runAll('stress');
const sum = (rs: DayResult[]) => rs.reduce((a, r) => a + r.net, 0);
const net = sum(base.rows);

// Random-entry control: same days, entry minute uniform over 09:45–13:30.
let seed = 20261101;
const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
let k = 0, ctlSum = 0;
for (let run = 0; run < CONTROL_RUNS; run++) {
  let n = 0;
  for (const d of expiryDays) {
    const bars = byDay.get(d)!;
    const window = bars.map((b, i) => [istTimeOf(b.timestampMs), i] as const)
      .filter(([t]) => t >= '09:44' && t <= '13:29').map(([, i]) => i);
    if (window.length === 0) continue;
    const r = runFly(d, window[Math.floor(rand() * window.length)]!, 'base');
    if (!('skip' in r)) n += r.net;
  }
  if (n >= net) k++;
  ctlSum += n;
}

const byMonth = new Map<string, number>();
for (const r of base.rows) byMonth.set(r.day.slice(0, 7), (byMonth.get(r.day.slice(0, 7)) ?? 0) + r.net);
const best = [...byMonth.entries()].sort((a, b) => b[1] - a[1])[0];
const top5 = [...base.rows].map((r) => r.net).sort((a, b) => b - a).slice(0, 5).reduce((a, b) => a + b, 0);
const wins = base.rows.filter((r) => r.net > 0);
const losses = base.rows.filter((r) => r.net <= 0);
let peak = 0, eq = 0, dd = 0;
for (const r of base.rows) { eq += r.net; peak = Math.max(peak, eq); dd = Math.min(dd, eq - peak); }
const control = { p: +((k + 1) / (CONTROL_RUNS + 1)).toFixed(4), meanRandomNet: Math.round(ctlSum / CONTROL_RUNS) };
const checks = {
  netPositiveBase: net > 0,
  netNonNegativeStress: sum(stress.rows) >= 0,
  beatsRandomEntry: control.p < 0.1,
  positiveWithoutBestMonth: best ? net - best[1] > 0 : false,
  top5DaysUnderHalf: net > 0 ? top5 < 0.5 * net : false,
};

const summary = {
  preregistration: 'backtest/PREREGISTRATION.md, H3 (commit c65d51c)',
  period: { from: fromDate, to: toDate, expiryDays: expiryDays.length, traded: base.rows.length, skipped: base.skips },
  trades: base.rows.length,
  winRatePct: base.rows.length ? +((wins.length / base.rows.length) * 100).toFixed(1) : 0,
  avgWin: wins.length ? Math.round(sum(wins) / wins.length) : 0,
  avgLoss: losses.length ? Math.round(sum(losses) / losses.length) : 0,
  grossBase: base.rows.reduce((a, r) => a + r.gross, 0),
  netBase: net,
  netPerDay: base.rows.length ? Math.round(net / base.rows.length) : 0,
  netStress: sum(stress.rows),
  worstDay: [...base.rows].sort((a, b) => a.net - b.net)[0] ?? null,
  bestDay: [...base.rows].sort((a, b) => b.net - a.net)[0] ?? null,
  maxDrawdown: dd,
  stops: base.rows.filter((r) => r.reason === 'stop').length,
  bestMonth: best ? { month: best[0], net: best[1] } : null,
  netWithoutBestMonth: best ? net - best[1] : null,
  top5DaysNet: top5,
  byMonth: Object.fromEntries([...byMonth.entries()].sort()),
  control,
  checks,
  passes: Object.values(checks).every(Boolean),
};

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2));
const cols = Object.keys(base.rows[0] ?? { day: '' }) as (keyof DayResult)[];
writeFileSync(join(outDir, 'trades.csv'),
  [cols.join(','), ...base.rows.map((r) => cols.map((c) => r[c]).join(','))].join('\n'));
console.log(JSON.stringify(summary, null, 2));
