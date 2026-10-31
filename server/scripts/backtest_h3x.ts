/**
 * Pre-registered H3T: expiry-day iron fly with a 30% profit target, on
 * multi-year NIFTY / SENSEX data (backtest/PREREGISTRATION.md, commit e6ea421).
 *
 *   npx tsx scripts/backtest_h3x.ts <data_dir> <NIFTY|SENSEX> <from> <to> <out_dir>
 *
 * Optional env HALF_SPREAD_PCT overrides the base half-spread (sensitivity only).
 */

import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { istTimeOf } from '../src/indicators.js';
import { loadData, type OptRow } from './btData.js';

const [dataDir, index, fromDate, toDate, outDir] = process.argv.slice(2) as string[];
if (!dataDir || !index || !fromDate || !toDate || !outDir) {
  console.error('usage: backtest_h3x.ts <data_dir> <NIFTY|SENSEX> <from> <to> <out_dir>');
  process.exit(1);
}
const SPEC = index === 'NIFTY'
  ? { step: 50, lot: 65, exchange: 0.0003503 }
  : { step: 100, lot: 20, exchange: 0.000325 };
const TARGET_PCT = 0.3;
const MAX_STRIKES = 10;
type Side = 'CE' | 'PE';
type Scenario = 'base' | 'stress';

const { opt, byDay } = loadData(dataDir);
const expiries = (JSON.parse(readFileSync(join(dataDir, 'expiries.json'), 'utf8')) as string[])
  .filter((d) => d >= fromDate && d <= toDate && byDay.has(d));
const optAt = (t: number, side: Side, k: number): OptRow | undefined => opt.get(t)?.get(1)?.[side].get(k);

const OVERRIDE = process.env['HALF_SPREAD_PCT'] ? Number(process.env['HALF_SPREAD_PCT']) : null;
const halfSpread = (p: number, s: Scenario) =>
  Math.max((s === 'base' ? (OVERRIDE ?? 0.01) : 0.015) * p, 0.1);

function sttRate(date: string): number {
  if (date < '2024-10-01') return 0.000625;
  if (date < '2026-04-01') return 0.001;
  return 0.0015;
}

function orderCharges(price: number, kind: 'buy' | 'sell', date: string): number {
  const turnover = Math.max(price, 0) * SPEC.lot;
  const exchange = SPEC.exchange * turnover;
  const sebi = (turnover * 10) / 1e7;
  const gst = 0.18 * (20 + exchange + sebi);
  const stamp = kind === 'buy' ? 0.00003 * turnover : 0;
  const stt = kind === 'sell' ? sttRate(date) * turnover : 0;
  return 20 + exchange + sebi + gst + stamp + stt;
}

interface Leg { side: Side; k: number; dir: -1 | 1 }
interface DayResult {
  day: string; K: number; W: number; credit: number; exitTime: string;
  reason: string; points: number; net: number;
}

function runFly(day: string, s: Scenario): DayResult | { skip: string } {
  const bars = byDay.get(day)!;
  const setupIdx = bars.findIndex((b) => istTimeOf(b.timestampMs) === '09:44');
  const setup = bars[setupIdx];
  const entryBar = bars[setupIdx + 1];
  if (setupIdx < 0 || !setup || !entryBar) return { skip: 'no 09:44/09:45 bar' };
  const K = Math.round(setup.close / SPEC.step) * SPEC.step;
  const ceS = optAt(setup.timestampMs, 'CE', K);
  const peS = optAt(setup.timestampMs, 'PE', K);
  if (!ceS || !peS) return { skip: 'no ATM price at setup' };
  const W = ceS.c + peS.c;
  const up = Math.round((K + W) / SPEC.step) * SPEC.step;
  const dn = Math.round((K - W) / SPEC.step) * SPEC.step;
  if (up - K > MAX_STRIKES * SPEC.step || K - dn > MAX_STRIKES * SPEC.step) return { skip: 'wing beyond data band' };

  const legs: Leg[] = [
    { side: 'CE', k: K, dir: -1 }, { side: 'PE', k: K, dir: -1 },
    { side: 'CE', k: up, dir: 1 }, { side: 'PE', k: dn, dir: 1 },
  ];
  const opens = legs.map((l) => optAt(entryBar.timestampMs, l.side, l.k)?.o);
  if (opens.some((x) => x === undefined || !(x > 0))) return { skip: 'leg missing at entry' };
  const entryFill = legs.map((l, i) => {
    const p = opens[i]!;
    return l.dir < 0 ? p - halfSpread(p, s) : p + halfSpread(p, s);
  });
  const credit = legs.reduce((a, l, i) => a + (l.dir < 0 ? entryFill[i]! : -entryFill[i]!), 0);
  let charges = legs.reduce((a, l, i) => a + orderCharges(entryFill[i]!, l.dir < 0 ? 'sell' : 'buy', day), 0);
  const last = [...opens] as number[];

  const exitAt = (prices: number[], t: number, reason: string): DayResult => {
    const exitFill = legs.map((l, i) => {
      const p = prices[i]!;
      return l.dir < 0 ? p + halfSpread(p, s) : Math.max(p - halfSpread(p, s), 0);
    });
    charges += legs.reduce((a, l, i) => a + orderCharges(exitFill[i]!, l.dir < 0 ? 'buy' : 'sell', day), 0);
    const closeCost = legs.reduce((a, l, i) => a + (l.dir < 0 ? exitFill[i]! : -exitFill[i]!), 0);
    const points = credit - closeCost;
    return {
      day, K, W: +W.toFixed(1), credit: +credit.toFixed(2), exitTime: istTimeOf(t), reason,
      points: +points.toFixed(2), net: Math.round(points * SPEC.lot - charges),
    };
  };
  const nextOpens = (i: number) => {
    const nb = bars[i + 1] ?? bars[i]!;
    return { px: legs.map((l, j) => optAt(nb.timestampMs, l.side, l.k)?.o ?? last[j]!), t: nb.timestampMs };
  };

  for (let i = setupIdx + 1; i < bars.length; i++) {
    const b = bars[i]!;
    legs.forEach((l, j) => {
      const o = optAt(b.timestampMs, l.side, l.k);
      if (o) last[j] = o.c;
    });
    if (istTimeOf(b.timestampMs) >= '15:10' || i === bars.length - 1) return exitAt(last, b.timestampMs, 'flat');
    const markCost = legs.reduce((a, l, j) => a + (l.dir < 0 ? last[j]! : -last[j]!), 0);
    if (credit - markCost >= TARGET_PCT * credit) { const n = nextOpens(i); return exitAt(n.px, n.t, 'target'); }
    if (markCost - credit >= credit) { const n = nextOpens(i); return exitAt(n.px, n.t, 'stop'); }
  }
  return { skip: 'ran out of bars' };
}

function runAll(s: Scenario) {
  const rows: DayResult[] = [];
  const skips: Record<string, string> = {};
  for (const d of expiries) {
    const r = runFly(d, s);
    if ('skip' in r) skips[d] = r.skip;
    else rows.push(r);
  }
  return { rows, skips };
}

const base = runAll('base');
const stress = runAll('stress');
const sum = (rs: DayResult[]) => rs.reduce((a, r) => a + r.net, 0);
const net = sum(base.rows);
const group = (key: (r: DayResult) => string) => {
  const m = new Map<string, { trades: number; wins: number; net: number }>();
  for (const r of base.rows) {
    const g = m.get(key(r)) ?? { trades: 0, wins: 0, net: 0 };
    g.trades++; if (r.net > 0) g.wins++; g.net += r.net;
    m.set(key(r), g);
  }
  return Object.fromEntries([...m.entries()].sort());
};
const byYear = group((r) => r.day.slice(0, 4));
const best = Object.entries(byYear).sort((a, b) => b[1].net - a[1].net)[0];
const wins = base.rows.filter((r) => r.net > 0);
const losses = base.rows.filter((r) => r.net <= 0);
const top5 = base.rows.map((r) => r.net).sort((a, b) => b - a).slice(0, 5).reduce((a, b) => a + b, 0);
let peak = 0, eq = 0, dd = 0;
for (const r of base.rows) { eq += r.net; peak = Math.max(peak, eq); dd = Math.min(dd, eq - peak); }
const winRate = base.rows.length ? wins.length / base.rows.length : 0;
const yearsPositive = Object.values(byYear).filter((y) => y.net > 0).length;
const checks = {
  netPositiveBase: net > 0,
  netNonNegativeStress: sum(stress.rows) >= 0,
  winRateAtLeast60: winRate >= 0.6,
  positiveWithoutBestYear: best ? net - best[1].net > 0 : false,
  atLeast60PctYearsPositive: yearsPositive >= 0.6 * Object.keys(byYear).length,
  top5DaysUnderHalf: net > 0 ? top5 < 0.5 * net : false,
};
const summary = {
  preregistration: 'backtest/PREREGISTRATION.md, H3T (commit e6ea421)',
  index, lot: SPEC.lot, halfSpreadOverride: OVERRIDE,
  period: { from: fromDate, to: toDate, expiryDays: expiries.length, traded: base.rows.length, skipped: Object.keys(base.skips).length },
  winRatePct: +(winRate * 100).toFixed(1),
  avgWin: wins.length ? Math.round(sum(wins) / wins.length) : 0,
  avgLoss: losses.length ? Math.round(sum(losses) / losses.length) : 0,
  netBase: net,
  netPerExpiry: base.rows.length ? Math.round(net / base.rows.length) : 0,
  netStress: sum(stress.rows),
  worstDay: [...base.rows].sort((a, b) => a.net - b.net)[0] ?? null,
  maxDrawdown: dd,
  top5DaysNet: top5,
  byYear,
  exits: group((r) => r.reason),
  checks,
  passes: Object.values(checks).every(Boolean),
  skippedDays: base.skips,
};
mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2));
const cols = Object.keys(base.rows[0] ?? { day: '' }) as (keyof DayResult)[];
writeFileSync(join(outDir, 'trades.csv'), [cols.join(','), ...base.rows.map((r) => cols.map((c) => r[c]).join(','))].join('\n'));
const { skippedDays, ...brief } = summary;
void skippedDays;
console.log(JSON.stringify(brief, null, 2));
