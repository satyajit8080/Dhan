/**
 * Shared data loading for the backtest scripts: SENSEX 1-min index bars and
 * Dhan expired-weekly option history, as written by backtest/fetch_data.py.
 * Several data dirs (comma-separated) are merged; overlapping bars dedupe.
 */

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import type { Candle } from '../src/endpoints/historical.js';
import { istDateOf } from '../src/indicators.js';

export interface OptRow { o: number; h: number; l: number; c: number; oi: number; iv: number }
/** minute → expiry code → side → strike → row */
export type OptBook = Map<number, Map<number, { CE: Map<number, OptRow>; PE: Map<number, OptRow> }>>;

export interface BacktestData {
  allBars: Candle[];
  opt: OptBook;
  byDay: Map<string, Candle[]>;
  days: string[];
  isExpiryDay: (d: string) => boolean;
}

interface Cols { [k: string]: number[] }

export function loadData(dataDir: string): BacktestData {
  const dirs = dataDir.split(',');
  const load = (dir: string, f: string): Cols => JSON.parse(readFileSync(join(dir, f), 'utf8')) as Cols;

  const barMap = new Map<number, Candle>();
  for (const dir of dirs) {
    const idx = load(dir, 'index_1m.json');
    idx.timestamp!.forEach((t, i) =>
      barMap.set(t * 1000, {
        timestampMs: t * 1000,
        open: idx.open![i]!,
        high: idx.high![i]!,
        low: idx.low![i]!,
        close: idx.close![i]!,
        volume: idx.volume![i] ?? null,
        openInterest: null,
      }),
    );
  }
  const allBars = [...barMap.values()].sort((a, b) => a.timestampMs - b.timestampMs);

  const opt: OptBook = new Map();
  for (const dir of dirs) for (const code of [1, 2]) {
    for (const [typ, side] of [['CALL', 'CE'], ['PUT', 'PE']] as const) {
      for (let off = -10; off <= 10; off++) {
        const f = `opt_${code}_${typ}_${off}.json`;
        if (!existsSync(join(dir, f))) continue;
        const d = load(dir, f);
        const ts = d.timestamp ?? [];
        for (let i = 0; i < ts.length; i++) {
          const k = d.strike![i];
          if (!k) continue;
          const t = ts[i]! * 1000;
          let m = opt.get(t);
          if (!m) opt.set(t, (m = new Map()));
          let c = m.get(code);
          if (!c) m.set(code, (c = { CE: new Map(), PE: new Map() }));
          c[side].set(k, {
            o: d.open![i]!, h: d.high![i]!, l: d.low![i]!, c: d.close![i]!,
            oi: d.oi![i] ?? 0, iv: d.iv![i] ?? 0,
          });
        }
      }
    }
  }

  const byDay = new Map<string, Candle[]>();
  for (const b of allBars) {
    const d = istDateOf(b.timestampMs);
    if (!byDay.has(d)) byDay.set(d, []);
    byDay.get(d)!.push(b);
  }
  const days = [...byDay.keys()].sort();

  /** Weekly expiry: Thursday, or the last trading day before a Thursday holiday. */
  function isExpiryDay(d: string): boolean {
    const dow = new Date(`${d}T00:00:00Z`).getUTCDay();
    if (dow === 4) return true;
    if (dow === 0 || dow === 6 || dow === 5) return false;
    const thu = new Date(`${d}T00:00:00Z`);
    thu.setUTCDate(thu.getUTCDate() + (4 - dow));
    const thuStr = thu.toISOString().slice(0, 10);
    if (byDay.has(thuStr)) return false;
    // Thursday is a holiday: expiry is the last trading day before it.
    for (let x = new Date(thu); ; ) {
      x.setUTCDate(x.getUTCDate() - 1);
      const s = x.toISOString().slice(0, 10);
      if (byDay.has(s)) return s === d;
      if (s < d) return false;
    }
  }

  return { allBars, opt, byDay, days, isExpiryDay };
}
