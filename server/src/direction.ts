/**
 * Direction verdict for SENSEX option buying. PURE MODULE — no I/O; the clock
 * comes in as `nowMs`.
 *
 * Implements RULES.md §6 (v3.1 thresholds, agreed 29 Oct 2026):
 *
 *   Setup A — breakout (trend-following)
 *     - a closed 1-min bar closes beyond a confirmed level by at least
 *       max(0.15 × 5-min ATR14, 8 pts)
 *     - that bar is decisive: body ≥ 40% of its range
 *     - its volume beats the 10-bar average
 *     - the next 1-min bar holds (does not close back inside)
 *     - 5-min ADX ≥ 23, ±DI agree by ≥ 3, ADX not falling
 *     - OI/PCR confirm (writer-dominated reading, see `oiConfirmation`)
 *     - entry on the retest (preferred) or the second bar
 *
 *   Setup B — rejection (counter-trend)
 *     - 2 consecutive closed 1-min bars wick into a level and close back away
 *     - 5-min ADX < 25, so a strong trend is not faded
 *
 * A fresh entry (second bar, rejection) is banned within 3 pts of the level.
 * A retest entry is exempt: it is only reachable after the hold bar confirmed.
 *
 * Fails closed: any input the rule needs and does not have is a blocker, never
 * a pass.
 */

import type { Candle } from './endpoints/historical.js';
import type { ChainStrike } from './types.js';
import { adx, atr, istTimeOf } from './indicators.js';
import { aggregateCandles, deriveLevels, type LevelConfig } from './levels.js';

export interface DirectionConfig {
  /** Break penetration = max(penetrationAtrMult × 5-min ATR14, penetrationFloorPts). */
  penetrationAtrMult: number;
  penetrationFloorPts: number;
  /** Break bar body as a fraction of its range. */
  minBodyPct: number;
  /** Break bar volume must exceed the average of this many prior bars. */
  volumeLookback: number;
  /** Setup A: 5-min ADX at or above this. */
  adxTrendMin: number;
  /** Below this the market is non-trending: no setup A, ever. */
  adxFloor: number;
  /** Setup A: |+DI − −DI| at least this, in the trade's direction. */
  diSpreadMin: number;
  /** ADX may dip this much from the previous 5-min bar and still count as flat. */
  adxFlatTolerance: number;
  /** Setup B: 5-min ADX must be below this. */
  rejectionAdxMax: number;
  /** Setup B: the rejecting wick as a fraction of the bar's range. */
  rejectionMinWickPct: number;
  /** Setup B: how close the wick must come to the level, in points. */
  rejectionTouchPts: number;
  /** Fresh entries are banned this close to the level. */
  noEntryWithinPts: number;
  /** Retest: the latest bar's extreme must come back within this of the level. */
  retestBandPts: number;
  /** How many bars back a confirmed break stays eligible for a retest entry. */
  retestLookbackBars: number;
  /** Stop buffer = clamp(stopBufferAtrMult × 5-min ATR14, min, max). */
  stopBufferAtrMult: number;
  stopBufferMinPts: number;
  stopBufferMaxPts: number;
  /** Target when no next level sits at least 1R away. */
  targetRiskMultiple: number;
  timeStopMinutes: number;
  /** Strikes on each side of the level checked for OI. */
  oiStrikesEachSide: number;
  /** IST HH:MM windows, [start, end). */
  noTradeWindows: [string, string][];
}

export const DEFAULT_DIRECTION_CONFIG: DirectionConfig = {
  penetrationAtrMult: 0.15,
  penetrationFloorPts: 8,
  minBodyPct: 0.4,
  volumeLookback: 10,
  adxTrendMin: 23,
  adxFloor: 20,
  diSpreadMin: 3,
  adxFlatTolerance: 0.5,
  rejectionAdxMax: 25,
  rejectionMinWickPct: 0.4,
  rejectionTouchPts: 5,
  noEntryWithinPts: 3,
  retestBandPts: 5,
  retestLookbackBars: 10,
  stopBufferAtrMult: 0.05,
  stopBufferMinPts: 2,
  stopBufferMaxPts: 4,
  targetRiskMultiple: 1.5,
  timeStopMinutes: 10,
  oiStrikesEachSide: 2,
  noTradeWindows: [
    ['09:15', '09:25'],
    ['11:30', '13:00'],
    ['14:50', '15:30'],
  ],
};

// ---------------------------------------------------------------------------
// OI / PCR view
// ---------------------------------------------------------------------------

export interface OiStrikeChange {
  strike: number;
  ceOiChange: number | null;
  peOiChange: number | null;
}

export interface OiView {
  /**
   * intraday: change against a scan 15–30 min ago (rolling history).
   * vs_previous_day: no intraday history yet, change against previous-day OI.
   */
  basis: 'intraday' | 'vs_previous_day';
  minutesBack: number | null;
  strikes: OiStrikeChange[];
  pcrNow: number | null;
  /** Null on the previous-day basis: PCR trend needs intraday history. */
  pcrPast: number | null;
}

export interface PastOiObservation {
  t: number;
  pcr: number | null;
  oi: { k: number; ce: number | null; pe: number | null }[];
}

/** OI change per strike, intraday when a past scan exists, else vs previous day. */
export function buildOiView(
  strikes: ChainStrike[],
  pcrNow: number | null,
  past: PastOiObservation | null,
  nowMs: number,
): OiView {
  if (past && past.oi.length > 0) {
    const then = new Map(past.oi.map((o) => [o.k, o]));
    const out: OiStrikeChange[] = [];
    for (const s of strikes) {
      const p = then.get(s.strike);
      if (!p) continue;
      out.push({
        strike: s.strike,
        ceOiChange: s.ce?.oi != null && p.ce != null ? s.ce.oi - p.ce : null,
        peOiChange: s.pe?.oi != null && p.pe != null ? s.pe.oi - p.pe : null,
      });
    }
    return {
      basis: 'intraday',
      minutesBack: Math.round((nowMs - past.t) / 60_000),
      strikes: out,
      pcrNow,
      pcrPast: past.pcr,
    };
  }

  return {
    basis: 'vs_previous_day',
    minutesBack: null,
    strikes: strikes.map((s) => ({
      strike: s.strike,
      ceOiChange: s.ce?.oi != null && s.ce.previousOi != null ? s.ce.oi - s.ce.previousOi : null,
      peOiChange: s.pe?.oi != null && s.pe.previousOi != null ? s.pe.oi - s.pe.previousOi : null,
    })),
    pcrNow,
    pcrPast: null,
  };
}

export interface OiConfirmation {
  confirmed: boolean;
  supporting: string[];
  contrary: string[];
  basis: OiView['basis'];
}

function sumOf(xs: (number | null)[]): number | null {
  const v = xs.filter((x): x is number => x !== null);
  return v.length === 0 ? null : v.reduce((a, b) => a + b, 0);
}

/**
 * Writer-dominated reading of OI around the level (SENSEX weeklies are mostly
 * written, not bought):
 *
 *   CE: put OI building at/below the level, call OI unwinding at/above it,
 *       PCR rising (put writing)
 *   PE: call OI building at/above the level, put OI unwinding at/below it,
 *       PCR falling
 *
 * Confirmed when at least one signal supports the side and supporters
 * outnumber contrary signals. Rising call OI above a CE breakout is contrary:
 * that is writers defending resistance, not buyers.
 */
export function oiConfirmation(
  side: 'CE' | 'PE',
  level: number,
  view: OiView,
  strikesEachSide = 2,
): OiConfirmation {
  const sorted = [...view.strikes].sort((a, b) => a.strike - b.strike);
  const below = sorted.filter((s) => s.strike <= level).slice(-strikesEachSide);
  const above = sorted.filter((s) => s.strike >= level).slice(0, strikesEachSide);

  const putBelow = sumOf(below.map((s) => s.peOiChange));
  const callAbove = sumOf(above.map((s) => s.ceOiChange));
  const pcrDelta =
    view.pcrNow !== null && view.pcrPast !== null ? view.pcrNow - view.pcrPast : null;

  const supporting: string[] = [];
  const contrary: string[] = [];
  const fmt = (n: number) => `${n > 0 ? '+' : ''}${Math.round(n)}`;
  const at = (xs: OiStrikeChange[]) => xs.map((s) => s.strike).join('/');

  const putLabel = `put OI ${fmt(putBelow ?? 0)} at ${at(below)}`;
  const callLabel = `call OI ${fmt(callAbove ?? 0)} at ${at(above)}`;
  const pcrLabel =
    pcrDelta === null ? '' : `PCR ${view.pcrPast!.toFixed(2)} → ${view.pcrNow!.toFixed(2)}`;

  if (side === 'CE') {
    if (putBelow !== null && putBelow > 0) supporting.push(`Put writing: ${putLabel}`);
    if (putBelow !== null && putBelow < 0) contrary.push(`Put unwinding: ${putLabel}`);
    if (callAbove !== null && callAbove < 0) supporting.push(`Call unwinding: ${callLabel}`);
    if (callAbove !== null && callAbove > 0) contrary.push(`Call writing: ${callLabel}`);
    if (pcrDelta !== null && pcrDelta > 0) supporting.push(`PCR rising: ${pcrLabel}`);
    if (pcrDelta !== null && pcrDelta < 0) contrary.push(`PCR falling: ${pcrLabel}`);
  } else {
    if (callAbove !== null && callAbove > 0) supporting.push(`Call writing: ${callLabel}`);
    if (callAbove !== null && callAbove < 0) contrary.push(`Call unwinding: ${callLabel}`);
    if (putBelow !== null && putBelow < 0) supporting.push(`Put unwinding: ${putLabel}`);
    if (putBelow !== null && putBelow > 0) contrary.push(`Put writing: ${putLabel}`);
    if (pcrDelta !== null && pcrDelta < 0) supporting.push(`PCR falling: ${pcrLabel}`);
    if (pcrDelta !== null && pcrDelta > 0) contrary.push(`PCR rising: ${pcrLabel}`);
  }

  return {
    confirmed: supporting.length >= 1 && supporting.length > contrary.length,
    supporting,
    contrary,
    basis: view.basis,
  };
}

// ---------------------------------------------------------------------------
// Verdict
// ---------------------------------------------------------------------------

export interface DirectionInput {
  nowMs: number;
  /** 1-minute bars, oldest first. A still-forming last bar is dropped here. */
  bars1m: Candle[];
  /**
   * Volume by bar timestamp, for when the bars themselves carry none (the
   * index series). Usually the futures series' 1-minute volume.
   */
  volumeByTs?: Map<number, number>;
  oi: OiView | null;
  levelConfig?: LevelConfig;
  config?: Partial<DirectionConfig>;
}

export interface TrendState {
  adx: number | null;
  adxPrevious: number | null;
  plusDi: number | null;
  minusDi: number | null;
  atr14: number | null;
}

export interface DirectionVerdict {
  signal: 'CE' | 'PE' | null;
  setup: 'breakout' | 'rejection' | null;
  entryMode: 'retest' | 'second_bar' | 'rejection' | null;
  /** The underlying level broken or rejected. */
  level: number | null;
  /** Latest closed 1-min close: the underlying at entry. */
  entrySpot: number | null;
  /** Exit on a 1-min close through this. */
  stopLevel: number | null;
  targetLevel: number | null;
  targetSource: string | null;
  riskPoints: number | null;
  timeStopMinutes: number;
  /** Confirmations that actually fired. */
  reasons: string[];
  /** Why no signal, or why one side was discarded. */
  blockers: string[];
  trend5m: TrendState;
  penetrationPts: number | null;
  stopBufferPts: number | null;
  oi: { CE: OiConfirmation | null; PE: OiConfirmation | null };
  lastClosedBarIst: string | null;
  configUsed: DirectionConfig;
}

function inWindow(nowMs: number, windows: [string, string][]): [string, string] | null {
  const t = istTimeOf(nowMs);
  return windows.find(([a, b]) => t >= a && t < b) ?? null;
}

function bodyPct(c: Candle): number {
  const range = c.high - c.low;
  return range > 0 ? Math.abs(c.close - c.open) / range : 0;
}

function volOf(c: Candle, byTs?: Map<number, number>): number | null {
  if (c.volume != null && c.volume > 0) return c.volume;
  return byTs?.get(c.timestampMs) ?? null;
}

interface Candidate {
  side: 'CE' | 'PE';
  setup: 'breakout' | 'rejection';
  entryMode: 'retest' | 'second_bar' | 'rejection';
  level: number;
  stopLevel: number;
  reasons: string[];
}

/**
 * Turn price action, 5-min trend and OI into CE, PE or nothing.
 *
 * Returns `signal: null` with blockers when nothing qualifies, and also when
 * both sides qualify at once — a conflict is not a signal.
 */
export function decideDirection(input: DirectionInput): DirectionVerdict {
  const cfg: DirectionConfig = { ...DEFAULT_DIRECTION_CONFIG, ...input.config };
  const blockers: string[] = [];

  // A bar is closed once a full minute has elapsed since it opened.
  const bars = input.bars1m.filter((b) => b.timestampMs + 60_000 <= input.nowMs);
  const last = bars[bars.length - 1];

  const fiveMin = aggregateCandles(bars, 5);
  const a = adx(fiveMin, 14);
  const trend: TrendState = {
    adx: a.adx,
    adxPrevious: a.adxPrevious,
    plusDi: a.plusDi,
    minusDi: a.minusDi,
    atr14: atr(fiveMin, 14),
  };

  const penetration =
    trend.atr14 !== null
      ? Math.max(cfg.penetrationAtrMult * trend.atr14, cfg.penetrationFloorPts)
      : null;
  const stopBuffer =
    trend.atr14 !== null
      ? Math.min(
          Math.max(cfg.stopBufferAtrMult * trend.atr14, cfg.stopBufferMinPts),
          cfg.stopBufferMaxPts,
        )
      : null;

  const verdict = (c: Candidate | null): DirectionVerdict => {
    let targetLevel: number | null = null;
    let targetSource: string | null = null;
    let risk: number | null = null;
    if (c && last) {
      risk = Math.abs(last.close - c.stopLevel);
      const now = deriveLevels(bars, last.close, input.levelConfig);
      const next = c.side === 'CE' ? now.resistance : now.support;
      const nextDist = next ? Math.abs(next.price - last.close) : 0;
      if (next && nextDist >= risk) {
        targetLevel = next.price;
        targetSource = `Next confirmed ${next.kind} (${next.touches} touches).`;
      } else {
        const d = cfg.targetRiskMultiple * risk;
        targetLevel = c.side === 'CE' ? last.close + d : last.close - d;
        targetSource = `No level at least 1R away; ${cfg.targetRiskMultiple}× the stop distance.`;
      }
    }
    return {
      signal: c?.side ?? null,
      setup: c?.setup ?? null,
      entryMode: c?.entryMode ?? null,
      level: c?.level ?? null,
      entrySpot: c && last ? last.close : null,
      stopLevel: c?.stopLevel ?? null,
      targetLevel,
      targetSource,
      riskPoints: risk,
      timeStopMinutes: cfg.timeStopMinutes,
      reasons: c?.reasons ?? [],
      blockers,
      trend5m: trend,
      penetrationPts: penetration,
      stopBufferPts: stopBuffer,
      oi: oiBySide,
      lastClosedBarIst: last ? istTimeOf(last.timestampMs) : null,
      configUsed: cfg,
    };
  };
  const oiBySide: DirectionVerdict['oi'] = { CE: null, PE: null };

  const window = inWindow(input.nowMs, cfg.noTradeWindows);
  if (window) {
    blockers.push(`No-trade window ${window[0]}–${window[1]} IST.`);
    return verdict(null);
  }
  if (!last || bars.length < 30) {
    blockers.push(`Need at least 30 closed 1-minute bars; have ${bars.length}.`);
    return verdict(null);
  }
  if (penetration === null || stopBuffer === null) {
    blockers.push('5-minute ATR14 unavailable: not enough 5-minute bars.');
    return verdict(null);
  }

  const candidates: Candidate[] = [];

  // --- Setup A: breakout -------------------------------------------------
  const trendOk = (side: 'CE' | 'PE'): string | null => {
    if (trend.adx === null || trend.plusDi === null || trend.minusDi === null) {
      return '5-min ADX/DI unavailable.';
    }
    if (trend.adx < cfg.adxFloor) {
      return `5-min ADX ${trend.adx.toFixed(1)} < ${cfg.adxFloor}: non-trending.`;
    }
    if (trend.adx < cfg.adxTrendMin) {
      return `5-min ADX ${trend.adx.toFixed(1)} < ${cfg.adxTrendMin}.`;
    }
    const spread = side === 'CE' ? trend.plusDi - trend.minusDi : trend.minusDi - trend.plusDi;
    if (spread < cfg.diSpreadMin) {
      return `DI disagree for ${side}: +DI ${trend.plusDi.toFixed(1)}, −DI ${trend.minusDi.toFixed(1)}.`;
    }
    if (
      trend.adxPrevious !== null &&
      trend.adx < trend.adxPrevious - cfg.adxFlatTolerance
    ) {
      return `5-min ADX falling (${trend.adxPrevious.toFixed(1)} → ${trend.adx.toFixed(1)}).`;
    }
    return null;
  };

  const n = bars.length;
  for (const side of ['CE', 'PE'] as const) {
    const dir = side === 'CE' ? 1 : -1;
    let found: Candidate | null = null;
    let sideBlocker: string | null = null;

    // Most recent break first. Bar i breaks, bar i+1 holds.
    for (let i = n - 2; i >= Math.max(n - 1 - cfg.retestLookbackBars, 20) && !found; i--) {
      const pre = deriveLevels(bars.slice(0, i), bars[i - 1]!.close, input.levelConfig);
      const lvl = side === 'CE' ? pre.resistance : pre.support;
      if (!lvl) continue;
      const L = lvl.price;
      const brk = bars[i]!;
      if (dir * (brk.close - L) < penetration) continue;

      const hold = bars[i + 1]!;
      if (dir * (hold.close - L) <= 0) {
        sideBlocker = `${side} break of ${L.toFixed(0)} at ${istTimeOf(brk.timestampMs)} failed to hold.`;
        continue;
      }
      // Anything after the hold bar that closed back through the stop kills it.
      const stopLevel = L - dir * stopBuffer;
      let failed = false;
      for (let j = i + 2; j < n; j++) {
        if (dir * (bars[j]!.close - stopLevel) <= 0) failed = true;
      }
      if (failed) {
        sideBlocker = `${side} break of ${L.toFixed(0)} at ${istTimeOf(brk.timestampMs)} closed back through the level.`;
        continue;
      }
      if (bodyPct(brk) < cfg.minBodyPct) {
        sideBlocker = `${side} break bar at ${istTimeOf(brk.timestampMs)} not decisive: body ${(bodyPct(brk) * 100).toFixed(0)}% of range.`;
        continue;
      }
      const v = volOf(brk, input.volumeByTs);
      const prior = bars
        .slice(Math.max(0, i - cfg.volumeLookback), i)
        .map((b) => volOf(b, input.volumeByTs))
        .filter((x): x is number => x !== null);
      if (v === null || prior.length < cfg.volumeLookback) {
        sideBlocker = `${side} break at ${istTimeOf(brk.timestampMs)}: volume unavailable.`;
        continue;
      }
      const avg = prior.reduce((s, x) => s + x, 0) / prior.length;
      if (v <= avg) {
        sideBlocker = `${side} break at ${istTimeOf(brk.timestampMs)}: volume ${v} ≤ 10-bar avg ${avg.toFixed(0)}.`;
        continue;
      }

      let entryMode: Candidate['entryMode'];
      if (i + 1 === n - 1) {
        if (dir * (last.close - L) <= cfg.noEntryWithinPts) {
          sideBlocker = `${side} second-bar entry within ${cfg.noEntryWithinPts} pts of ${L.toFixed(0)}.`;
          continue;
        }
        entryMode = 'second_bar';
      } else {
        const extreme = side === 'CE' ? last.low : last.high;
        const retested = dir * (extreme - L) <= cfg.retestBandPts && dir * (last.close - L) > 0;
        if (!retested) {
          sideBlocker = `${side} break of ${L.toFixed(0)} confirmed at ${istTimeOf(brk.timestampMs)}; waiting for a retest.`;
          continue;
        }
        entryMode = 'retest';
      }

      const t = trendOk(side);
      if (t) {
        sideBlocker = `${side} breakout: ${t}`;
        continue;
      }
      if (!input.oi) {
        sideBlocker = `${side} breakout: no OI data.`;
        continue;
      }
      const oc = oiConfirmation(side, L, input.oi, cfg.oiStrikesEachSide);
      oiBySide[side] = oc;
      if (!oc.confirmed) {
        sideBlocker = `${side} breakout: OI does not confirm (${oc.contrary.join('; ') || 'no supporting OI/PCR change'}).`;
        continue;
      }

      found = {
        side,
        setup: 'breakout',
        entryMode,
        level: L,
        stopLevel,
        reasons: [
          `1-min close ${dir > 0 ? 'above' : 'below'} ${lvl.kind} ${L.toFixed(0)} (${lvl.touches} touches) by ${Math.abs(brk.close - L).toFixed(0)} pts at ${istTimeOf(brk.timestampMs)}, next bar held`,
          `Decisive bar: body ${(bodyPct(brk) * 100).toFixed(0)}% of range; volume ${(v / avg).toFixed(1)}× 10-bar avg`,
          `5-min ADX ${trend.adx!.toFixed(1)}, +DI ${trend.plusDi!.toFixed(1)} / −DI ${trend.minusDi!.toFixed(1)}`,
          ...oc.supporting,
          entryMode === 'retest' ? 'Retest of the level held' : 'Second-bar entry',
        ],
      };
    }
    if (found) candidates.push(found);
    else if (sideBlocker) blockers.push(sideBlocker);
  }

  // --- Setup B: rejection ------------------------------------------------
  const levelsNow = deriveLevels(bars, last.close, input.levelConfig);
  const prev = bars[n - 2]!;
  const rejections: [Candidate['side'], typeof levelsNow.resistance][] = [
    ['PE', levelsNow.resistance],
    ['CE', levelsNow.support],
  ];
  for (const [side, lvl] of rejections) {
    if (!lvl) continue;
    const L = lvl.price;
    // Wicking into resistance means a PE; into support, a CE.
    const rejects = (c: Candle) => {
      const range = c.high - c.low;
      if (range <= 0) return false;
      if (side === 'PE') {
        const wick = c.high - Math.max(c.open, c.close);
        return c.high >= L - cfg.rejectionTouchPts && c.close < L && wick / range >= cfg.rejectionMinWickPct;
      }
      const wick = Math.min(c.open, c.close) - c.low;
      return c.low <= L + cfg.rejectionTouchPts && c.close > L && wick / range >= cfg.rejectionMinWickPct;
    };
    if (!(rejects(prev) && rejects(last))) continue;

    if (trend.adx === null) {
      blockers.push(`${side} rejection at ${L.toFixed(0)}: 5-min ADX unavailable.`);
      continue;
    }
    if (trend.adx >= cfg.rejectionAdxMax) {
      blockers.push(`${side} rejection at ${L.toFixed(0)}: 5-min ADX ${trend.adx.toFixed(1)} ≥ ${cfg.rejectionAdxMax}, not fading a trend.`);
      continue;
    }
    if (Math.abs(last.close - L) <= cfg.noEntryWithinPts) {
      blockers.push(`${side} rejection entry within ${cfg.noEntryWithinPts} pts of ${L.toFixed(0)}.`);
      continue;
    }
    candidates.push({
      side,
      setup: 'rejection',
      entryMode: 'rejection',
      level: L,
      stopLevel: side === 'PE' ? L + stopBuffer : L - stopBuffer,
      reasons: [
        `2 consecutive 1-min wicks rejecting ${lvl.kind} ${L.toFixed(0)} (${lvl.touches} touches)`,
        `5-min ADX ${trend.adx.toFixed(1)} < ${cfg.rejectionAdxMax}: no strong trend to fade`,
      ],
    });
  }

  // Prefer a breakout over a rejection on the same side.
  const pick = (side: 'CE' | 'PE') =>
    candidates.find((c) => c.side === side && c.setup === 'breakout') ??
    candidates.find((c) => c.side === side) ??
    null;
  const ce = pick('CE');
  const pe = pick('PE');

  if (ce && pe) {
    blockers.push('Both CE and PE qualified at once: conflict, no signal.');
    return verdict(null);
  }
  const chosen = ce ?? pe;
  if (!chosen && blockers.length === 0) blockers.push('No breakout or rejection setup on the latest closed bars.');
  return verdict(chosen);
}
