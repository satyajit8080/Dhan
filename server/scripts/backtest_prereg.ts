/**
 * Pre-registered tests H1a and H2, plus the corrected ORB baseline, exactly as
 * written in backtest/PREREGISTRATION.md (committed before this file).
 *
 *   npx tsx scripts/backtest_prereg.ts <data_dir[,dir]> <from> <to> <out_dir>
 *
 * Every number that defines a rule lives in PREREGISTRATION.md; the constants
 * below only transcribe it.
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Candle } from '../src/endpoints/historical.js';
import { istTimeOf } from '../src/indicators.js';
import { loadData, type OptRow } from './btData.js';

const [dataDir, fromDate, toDate, outDir] = process.argv.slice(2) as string[];
if (!dataDir || !fromDate || !toDate || !outDir) {
  console.error('usage: backtest_prereg.ts <data_dir> <from> <to> <out_dir>');
  process.exit(1);
}

const LOT = 20;
const STEP = 100;
const SPREAD_WIDTH = 200;
const EXPIRY_TIME_VALUE_MAX = 50;
const CONTROL_RUNS = 1000;

type Side = 'CE' | 'PE';
type Scenario = 'base' | 'stress';

const { opt, byDay, days } = loadData(dataDir);

// ---------------------------------------------------------------- costs
function halfSpread(price: number, zeroDte: boolean, s: Scenario): number {
  const pct = s === 'base' ? (zeroDte ? 0.01 : 0.006) : zeroDte ? 0.015 : 0.01;
  return Math.max(pct * price, 0.1);
}

/** Statutory charges + brokerage for one order of one lot. */
function orderCharges(price: number, kind: 'buy' | 'sell', date: string): number {
  const turnover = Math.max(price, 0) * LOT;
  const brokerage = 20;
  const exchange = 0.000325 * turnover;
  const sebi = (turnover * 10) / 1e7;
  const gst = 0.18 * (brokerage + exchange + sebi);
  const stamp = kind === 'buy' ? 0.00003 * turnover : 0;
  const stt = kind === 'sell' ? (date < '2026-04-01' ? 0.001 : 0.0015) * turnover : 0;
  return brokerage + exchange + sebi + gst + stamp + stt;
}

// ---------------------------------------------------------------- expiry days
const optAt = (t: number, code: number, side: Side, k: number): OptRow | undefined =>
  opt.get(t)?.get(code)?.[side].get(k);

/**
 * POST-HOC ROBUSTNESS ONLY (EXPIRY_RULE=otm): the registered rule misses expiry
 * days whose ITM leg is priced off the settlement price rather than the last
 * index print. This variant also counts a day whose cheaper ATM leg closes at
 * ≤ ₹1. It never changes the registered pass/fail verdict.
 */
const OTM_RULE = process.env['EXPIRY_RULE'] === 'otm';

const expiryInfo = new Map<string, { timeValue: number | null; isExpiry: boolean }>();
for (const d of days) {
  const bars = byDay.get(d)!;
  let tv: number | null = null;
  let cheapLeg: number | null = null;
  for (let i = bars.length - 1; i >= 0 && tv === null; i--) {
    const b = bars[i]!;
    if (istTimeOf(b.timestampMs) < '15:25') break;
    const k = Math.round(b.close / STEP) * STEP;
    const ce = optAt(b.timestampMs, 1, 'CE', k);
    const pe = optAt(b.timestampMs, 1, 'PE', k);
    if (ce && pe) {
      tv = ce.c + pe.c - Math.abs(b.close - k);
      cheapLeg = Math.min(ce.c, pe.c);
    }
  }
  const registered = tv !== null && tv <= EXPIRY_TIME_VALUE_MAX;
  const otm = cheapLeg !== null && cheapLeg <= 1;
  expiryInfo.set(d, { timeValue: tv, isExpiry: registered || (OTM_RULE && otm) });
}
const isExpiry = (d: string) => expiryInfo.get(d)?.isExpiry ?? false;

// ---------------------------------------------------------------- helpers
const minuteOf = (b: Candle) => {
  const t = istTimeOf(b.timestampMs);
  return Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5)) - (9 * 60 + 15);
};
/** Index of every 15-min candle close from 09:44 to 13:29. */
function candleCloses(bars: Candle[]): number[] {
  const out: number[] = [];
  bars.forEach((b, i) => {
    const m = minuteOf(b);
    if (m >= 29 && (m + 1) % 15 === 0 && istTimeOf(b.timestampMs) <= '13:30') out.push(i);
  });
  return out;
}
const flatTime = (d: string) => (isExpiry(d) ? '15:10' : '15:15');

interface Fill { price: number; t: number }
interface Outcome { net: number; gross: number; entry: Fill; exit: Fill; reason: string }

/** Naked buy: stop −25%, breakeven then 15% trail after a +25% close. */
function runNaked(day: string, side: Side, k: number, entryIdx: number, s: Scenario): Outcome | null {
  const bars = byDay.get(day)!;
  const zero = isExpiry(day);
  const e = bars[entryIdx];
  if (!e) return null;
  const o0 = optAt(e.timestampMs, 1, side, k);
  if (!o0 || !(o0.o > 0)) return null;
  const raw = o0.o;
  const entry = raw + halfSpread(raw, zero, s);
  let stop = raw * 0.75;
  let peak = raw;
  let trailing = false;
  let last = raw;
  const flat = flatTime(day);
  for (let i = entryIdx; i < bars.length; i++) {
    const b = bars[i]!;
    const o = optAt(b.timestampMs, 1, side, k);
    if (o) {
      if (o.l <= stop) {
        const px = Math.min(stop, o.o);
        return finish(day, entry, px - halfSpread(px, zero, s), e.timestampMs, b.timestampMs,
          trailing ? (stop > raw ? 'trail' : 'breakeven') : 'stop');
      }
      last = o.c;
      if (o.c > peak) peak = o.c;
      if (!trailing && o.c >= raw * 1.25) trailing = true;
      if (trailing) stop = Math.max(raw, peak * 0.85);
    }
    if (istTimeOf(b.timestampMs) >= flat || i === bars.length - 1) {
      return finish(day, entry, last - halfSpread(last, zero, s), e.timestampMs, b.timestampMs, 'flat');
    }
  }
  return null;
}

function finish(day: string, entry: number, exit: number, te: number, tx: number, reason: string): Outcome {
  const gross = (exit - entry) * LOT;
  const net = gross - orderCharges(entry, 'buy', day) - orderCharges(exit, 'sell', day);
  return { net, gross, entry: { price: entry, t: te }, exit: { price: exit, t: tx }, reason };
}

/** Debit spread: long ATM, short ATM ± 200; exits on 1-min close, filled next open. */
function runSpread(day: string, side: Side, k: number, entryIdx: number, s: Scenario): Outcome | null {
  const bars = byDay.get(day)!;
  const zero = isExpiry(day);
  const kShort = side === 'CE' ? k + SPREAD_WIDTH : k - SPREAD_WIDTH;
  const e = bars[entryIdx];
  if (!e) return null;
  const l0 = optAt(e.timestampMs, 1, side, k);
  const s0 = optAt(e.timestampMs, 1, side, kShort);
  if (!l0 || !s0 || !(l0.o > 0) || !(s0.o > 0)) return null;
  const longIn = l0.o + halfSpread(l0.o, zero, s);
  const shortIn = s0.o - halfSpread(s0.o, zero, s);
  const debit = longIn - shortIn;
  const flat = flatTime(day);
  let lastL = l0.o, lastS = s0.o;

  const close = (i: number, reason: string): Outcome => {
    // Filled at the next minute's open where it exists, else the last closes.
    const nb = bars[i + 1];
    const nl = nb ? optAt(nb.timestampMs, 1, side, k) : undefined;
    const ns = nb ? optAt(nb.timestampMs, 1, side, kShort) : undefined;
    const pl = nl?.o ?? lastL, ps = ns?.o ?? lastS;
    const longOut = pl - halfSpread(pl, zero, s);
    const shortOut = ps + halfSpread(ps, zero, s);
    const gross = (longOut - longIn - (shortOut - shortIn)) * LOT;
    const charges =
      orderCharges(longIn, 'buy', day) + orderCharges(shortIn, 'sell', day) +
      orderCharges(longOut, 'sell', day) + orderCharges(shortOut, 'buy', day);
    return {
      net: gross - charges, gross,
      entry: { price: debit, t: e.timestampMs },
      exit: { price: longOut - shortOut, t: (nb ?? bars[i]!).timestampMs },
      reason,
    };
  };

  for (let i = entryIdx; i < bars.length; i++) {
    const b = bars[i]!;
    const lo = optAt(b.timestampMs, 1, side, k);
    const so = optAt(b.timestampMs, 1, side, kShort);
    if (lo) lastL = lo.c;
    if (so) lastS = so.c;
    const mark = lastL - lastS;
    if (lo && so) {
      if (mark <= 0.7 * debit) return close(i, 'stop');
      if (mark >= 0.8 * SPREAD_WIDTH) return close(i, 'target');
    }
    if (istTimeOf(b.timestampMs) >= flat || i === bars.length - 1) return close(i, 'flat');
  }
  return null;
}

// ---------------------------------------------------------------- signals
interface Signal { day: string; side: Side; strike: number; signalIdx: number }

/** Baseline ORB signals: one position at a time, using the naked trade's exit. */
function orbSignals(day: string): Signal[] {
  const bars = byDay.get(day)!;
  const or = bars.filter((b) => istTimeOf(b.timestampMs) < '09:30');
  if (or.length < 15) return [];
  const hi = Math.max(...or.map((b) => b.high));
  const lo = Math.min(...or.map((b) => b.low));
  const out: Signal[] = [];
  const done = new Set<Side>();
  let busyUntil = 0;
  for (const i of candleCloses(bars)) {
    const b = bars[i]!;
    if (b.timestampMs < busyUntil) continue;
    const side: Side | null = b.close > hi ? 'CE' : b.close < lo ? 'PE' : null;
    if (!side || done.has(side)) continue;
    const strike = Math.round(b.close / STEP) * STEP;
    const r = runNaked(day, side, strike, i + 1, 'base');
    if (!r) continue;
    done.add(side);
    busyUntil = r.exit.t;
    out.push({ day, side, strike, signalIdx: i });
  }
  return out;
}

const inRange = days.filter((d) => d >= fromDate && d <= toDate);
const signals = inRange.flatMap(orbSignals);

// ---------------------------------------------------------------- H1a gate
const expiryDays = days.filter(isExpiry);
const gate = new Map<string, { IM: number; RV: number; Q: number }>();
for (const d of expiryDays) {
  const prior = expiryDays.filter((x) => x < d).slice(-10);
  if (prior.length < 10) continue;
  const moves = prior.map((p) => {
    const bars = byDay.get(p)!;
    const at = (hm: string) => bars.find((b) => istTimeOf(b.timestampMs) === hm)?.close;
    const a = at('09:30'), b = at('15:15');
    return a !== undefined && b !== undefined ? Math.abs(b - a) : null;
  }).filter((x): x is number => x !== null).sort((a, b) => a - b);
  if (moves.length < 10) continue;
  const RV = (moves[4]! + moves[5]!) / 2;
  const bars = byDay.get(d)!;
  const b29 = bars.find((b) => istTimeOf(b.timestampMs) === '09:29');
  if (!b29) continue;
  const k = Math.round(b29.close / STEP) * STEP;
  const straddles = ['09:29', '09:30', '09:31'].map((hm) => {
    const b = bars.find((x) => istTimeOf(x.timestampMs) === hm);
    const ce = b && optAt(b.timestampMs, 1, 'CE', k);
    const pe = b && optAt(b.timestampMs, 1, 'PE', k);
    return ce && pe ? ce.c + pe.c : null;
  });
  if (straddles.some((x) => x === null) || RV <= 0) continue;
  const IM = (straddles as number[]).reduce((a, b) => a + b, 0) / 3;
  gate.set(d, { IM, RV, Q: IM / RV });
}

// ---------------------------------------------------------------- evaluation
interface Row { day: string; side: Side; strike: number; net: number; gross: number; reason: string }

function execute(sigs: Signal[], fn: typeof runNaked, s: Scenario): { rows: Row[]; skipped: number } {
  const rows: Row[] = [];
  let skipped = 0;
  for (const g of sigs) {
    const r = fn(g.day, g.side, g.strike, g.signalIdx + 1, s);
    if (!r) { skipped++; continue; }
    rows.push({ day: g.day, side: g.side, strike: g.strike, net: Math.round(r.net), gross: Math.round(r.gross), reason: r.reason });
  }
  return { rows, skipped };
}

/** Random-entry control: same day, side and exits; entry minute drawn at random. */
function control(sigs: Signal[], fn: typeof runNaked, actualNet: number): { p: number; meanRandomNet: number } {
  let seed = 20261101;
  const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const choices = new Map<string, number[]>();
  let k = 0, sum = 0;
  for (let run = 0; run < CONTROL_RUNS; run++) {
    let net = 0;
    for (const g of sigs) {
      const bars = byDay.get(g.day)!;
      let c = choices.get(g.day);
      if (!c) choices.set(g.day, (c = candleCloses(bars)));
      if (c.length === 0) continue;
      const i = c[Math.floor(rand() * c.length)]!;
      const strike = Math.round(bars[i]!.close / STEP) * STEP;
      const r = fn(g.day, g.side, strike, i + 1, 'base');
      if (r) net += r.net;
    }
    if (net >= actualNet) k++;
    sum += net;
  }
  return { p: +((k + 1) / (CONTROL_RUNS + 1)).toFixed(4), meanRandomNet: Math.round(sum / CONTROL_RUNS) };
}

function judge(name: string, sigs: Signal[], fn: typeof runNaked) {
  const base = execute(sigs, fn, 'base');
  const stress = execute(sigs, fn, 'stress');
  const net = base.rows.reduce((a, r) => a + r.net, 0);
  const stressNet = stress.rows.reduce((a, r) => a + r.net, 0);
  const byMonth = new Map<string, number>();
  const byDayNet = new Map<string, number>();
  for (const r of base.rows) {
    byMonth.set(r.day.slice(0, 7), (byMonth.get(r.day.slice(0, 7)) ?? 0) + r.net);
    byDayNet.set(r.day, (byDayNet.get(r.day) ?? 0) + r.net);
  }
  const bestMonth = [...byMonth.entries()].sort((a, b) => b[1] - a[1])[0];
  const top5 = [...byDayNet.values()].sort((a, b) => b - a).slice(0, 5).reduce((a, b) => a + b, 0);
  const ctl = control(sigs, fn, net);
  const n = base.rows.length;
  const wins = base.rows.filter((r) => r.net > 0).length;
  const checks = {
    netPositiveBase: net > 0,
    netNonNegativeStress: stressNet >= 0,
    beatsRandomEntry: ctl.p < 0.1,
    positiveWithoutBestMonth: bestMonth ? net - bestMonth[1] > 0 : false,
    top5DaysUnderHalf: net > 0 ? top5 < 0.5 * net : false,
  };
  return {
    name,
    trades: n,
    skipped: base.skipped,
    winRatePct: n ? +((wins / n) * 100).toFixed(1) : 0,
    grossBase: base.rows.reduce((a, r) => a + r.gross, 0),
    netBase: net,
    netPerTradeBase: n ? Math.round(net / n) : 0,
    netStress: stressNet,
    bestMonth: bestMonth ? { month: bestMonth[0], net: bestMonth[1] } : null,
    netWithoutBestMonth: bestMonth ? net - bestMonth[1] : null,
    top5DaysNet: top5,
    control: ctl,
    byMonth: Object.fromEntries([...byMonth.entries()].sort()),
    checks,
    passes: Object.values(checks).every(Boolean),
    rows: base.rows,
  };
}

const t0 = Date.now();
const baseline = judge('Baseline naked ORB (corrected)', signals, runNaked);

const eligible = inRange.filter((d) => gate.has(d));
const h1Sigs = signals.filter((g) => gate.has(g.day) && gate.get(g.day)!.Q <= 1.0);
const h1 = judge('H1a gated expiry-day ORB (Q <= 1.00)', h1Sigs, runNaked);
const ungatedSigs = signals.filter((g) => gate.has(g.day));
const ungated = judge('H1a control: ungated expiry-day ORB, eligible days', ungatedSigs, runNaked);

// Mechanism: Spearman(Q, ungated daily net), one-sided permutation p for r < 0.
const dayNet = new Map<string, number>();
for (const r of ungated.rows) dayNet.set(r.day, (dayNet.get(r.day) ?? 0) + r.net);
const pairs = eligible.map((d) => [gate.get(d)!.Q, dayNet.get(d) ?? 0] as [number, number]);
const rank = (xs: number[]) => {
  const idx = xs.map((x, i) => [x, i] as [number, number]).sort((a, b) => a[0] - b[0]);
  const r = new Array<number>(xs.length);
  for (let i = 0; i < idx.length; ) {
    let j = i;
    while (j + 1 < idx.length && idx[j + 1]![0] === idx[i]![0]) j++;
    for (let m = i; m <= j; m++) r[idx[m]![1]] = (i + j) / 2;
    i = j + 1;
  }
  return r;
};
const pearson = (a: number[], b: number[]) => {
  const n = a.length, ma = a.reduce((x, y) => x + y, 0) / n, mb = b.reduce((x, y) => x + y, 0) / n;
  let sab = 0, saa = 0, sbb = 0;
  for (let i = 0; i < n; i++) { sab += (a[i]! - ma) * (b[i]! - mb); saa += (a[i]! - ma) ** 2; sbb += (b[i]! - mb) ** 2; }
  return saa > 0 && sbb > 0 ? sab / Math.sqrt(saa * sbb) : 0;
};
const rq = rank(pairs.map((p) => p[0]));
const rp = rank(pairs.map((p) => p[1]));
const spearman = pearson(rq, rp);
let seed2 = 7, below = 0;
const rnd = () => ((seed2 = (seed2 * 1103515245 + 12345) % 2147483648) / 2147483648);
for (let i = 0; i < 10000; i++) {
  const sh = [...rp];
  for (let j = sh.length - 1; j > 0; j--) { const m = Math.floor(rnd() * (j + 1)); [sh[j], sh[m]] = [sh[m]!, sh[j]!]; }
  if (pearson(rq, sh) <= spearman) below++;
}
const mechanism = { eligibleDays: pairs.length, spearman: +spearman.toFixed(3), pOneSided: +((below + 1) / 10001).toFixed(4) };
const qSorted = [...pairs].sort((a, b) => a[0] - b[0]);
const third = Math.ceil(qSorted.length / 3);
const terciles = [0, 1, 2].map((t) => {
  const xs = qSorted.slice(t * third, (t + 1) * third);
  return { qFrom: +xs[0]![0].toFixed(2), qTo: +xs[xs.length - 1]![0].toFixed(2), days: xs.length, net: xs.reduce((a, b) => a + b[1], 0) };
});
h1.passes = h1.passes && mechanism.spearman < 0 && mechanism.pOneSided < 0.1;

const h2 = judge('H2 debit spread on baseline signals', signals, runSpread);

// Paired difference per signal (spread − naked), base fills.
const key = (r: Row) => `${r.day}|${r.side}|${r.strike}`;
const nakedBy = new Map(baseline.rows.map((r) => [key(r), r.net]));
const diffs = h2.rows.filter((r) => nakedBy.has(key(r))).map((r) => ({ day: r.day, d: r.net - nakedBy.get(key(r))! }));
const meanDiff = diffs.reduce((a, b) => a + b.d, 0) / (diffs.length || 1);
const sdDiff = Math.sqrt(diffs.reduce((a, b) => a + (b.d - meanDiff) ** 2, 0) / Math.max(1, diffs.length - 1));
const dayDiff = new Map<string, number>();
for (const x of diffs) dayDiff.set(x.day, (dayDiff.get(x.day) ?? 0) + x.d);
const top5Days = new Set([...dayDiff.entries()].sort((a, b) => b[1] - a[1]).slice(0, 5).map((e) => e[0]));
const paired = {
  signals: diffs.length,
  meanPerSignal: Math.round(meanDiff),
  tStat: +(meanDiff / (sdDiff / Math.sqrt(diffs.length || 1))).toFixed(2),
  totalWithoutTop5Days: diffs.filter((x) => !top5Days.has(x.day)).reduce((a, b) => a + b.d, 0),
};

const strip = (j: ReturnType<typeof judge>) => { const { rows, ...rest } = j; void rows; return rest; };
const thursdayMismatch = inRange.filter((d) => {
  const thu = new Date(`${d}T00:00:00Z`).getUTCDay() === 4;
  return thu !== isExpiry(d);
});
const summary = {
  preregistration: 'backtest/PREREGISTRATION.md (commit 64fc218)',
  expiryRule: OTM_RULE ? 'POST-HOC: registered OR cheaper ATM leg <= 1' : 'registered: time value <= 50',
  period: { from: fromDate, to: toDate, sessions: inRange.length },
  expiryDays: {
    detected: inRange.filter(isExpiry).length,
    unknown: inRange.filter((d) => expiryInfo.get(d)?.timeValue === null).length,
    differFromThursdayRule: thursdayMismatch,
  },
  baseline: strip(baseline),
  h1a: { ...strip(h1), eligibleExpiryDays: eligible.length, gatedDays: eligible.filter((d) => gate.get(d)!.Q <= 1).length, mechanism, terciles },
  h1aControl: strip(ungated),
  h2: { ...strip(h2), paired },
  stopRuleTriggered: baseline.netPerTradeBase < -50 && !h1.passes && !h2.passes,
  runtimeSec: Math.round((Date.now() - t0) / 1000),
};

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'summary.json'), JSON.stringify(summary, null, 2));
for (const [f, j] of [['baseline', baseline], ['h1a', h1], ['h2', h2]] as const) {
  writeFileSync(join(outDir, `${f}_trades.csv`),
    ['day,side,strike,net,gross,reason', ...j.rows.map((r) => `${r.day},${r.side},${r.strike},${r.net},${r.gross},${r.reason}`)].join('\n'));
}
console.log(JSON.stringify(summary, null, 2));
