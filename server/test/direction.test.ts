import { describe, it, expect } from 'vitest';
import {
  decideDirection,
  oiConfirmation,
  buildOiView,
  type OiView,
} from '../src/direction.js';
import { adx } from '../src/indicators.js';
import type { ChainStrike } from '../src/types.js';
import { trendThenBase, bar, OPEN_MS, MIN, BULLISH_OI } from './directionFixtures.js';

// Break of 75000 at 09:35 (bar 20), held at 09:36 (bar 21).
const BREAK = bar(20, 74975, 75030, 74974, 75025, 3000);
const HOLD = bar(21, 75025, 75035, 75015, 75030, 1500);

function run(tail = [BREAK, HOLD], over: Partial<Parameters<typeof decideDirection>[0]> = {}) {
  const bars = trendThenBase(tail);
  const lastOpen = bars[bars.length - 1]!.timestampMs;
  return decideDirection({ nowMs: lastOpen + MIN, bars1m: bars, oi: BULLISH_OI, ...over });
}

describe('setup A — breakout', () => {
  it('signals CE on a decisive, held, high-volume break with trend and OI agreeing', () => {
    const v = run();
    expect(v.signal).toBe('CE');
    expect(v.setup).toBe('breakout');
    expect(v.entryMode).toBe('second_bar');
    expect(v.level).toBe(75000);
    // Stop: close back through the level, minus the 2–4 pt buffer.
    expect(v.stopBufferPts).toBeGreaterThanOrEqual(2);
    expect(v.stopBufferPts).toBeLessThanOrEqual(4);
    expect(v.stopLevel).toBeCloseTo(75000 - v.stopBufferPts!, 6);
    expect(v.targetLevel!).toBeGreaterThan(v.entrySpot!);
    expect(v.timeStopMinutes).toBe(10);
    expect(v.reasons.join(' ')).toMatch(/Put writing/);
  });

  it('penetration is max(0.15 × 5-min ATR, 8 pts)', () => {
    const v = run();
    expect(v.penetrationPts).toBe(Math.max(0.15 * v.trend5m.atr14!, 8));
  });

  it('does not count a close only 5 pts through the level as a break', () => {
    const v = run([bar(20, 74975, 75008, 74974, 75005, 3000), bar(21, 75005, 75010, 75001, 75006, 1500)]);
    expect(v.signal).toBeNull();
  });

  it('rejects an indecisive break bar (body < 40% of range)', () => {
    const v = run([bar(20, 74995, 75060, 74960, 75015, 3000), HOLD]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/not decisive/);
  });

  it('rejects a break the next bar fails to hold', () => {
    const v = run([BREAK, bar(21, 75025, 75026, 74990, 74995, 1500)]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/failed to hold/);
  });

  it('rejects a break on volume at or below the 10-bar average', () => {
    const v = run([bar(20, 74975, 75030, 74974, 75025, 1000), HOLD]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/volume 1000 ≤ 10-bar avg/);
  });

  it('fails closed without volume, and accepts borrowed futures volume', () => {
    const noVol = trendThenBase([BREAK, HOLD]).map((c) => ({ ...c, volume: null }));
    const nowMs = noVol[noVol.length - 1]!.timestampMs + MIN;
    const blind = decideDirection({ nowMs, bars1m: noVol, oi: BULLISH_OI });
    expect(blind.signal).toBeNull();
    expect(blind.blockers.join(' ')).toMatch(/volume unavailable/);

    const volumeByTs = new Map(trendThenBase([BREAK, HOLD]).map((c) => [c.timestampMs, c.volume!]));
    const lent = decideDirection({ nowMs, bars1m: noVol, oi: BULLISH_OI, volumeByTs });
    expect(lent.signal).toBe('CE');
  });

  it('ignores a still-forming bar: the break bar alone is not a signal', () => {
    const bars = trendThenBase([BREAK, HOLD]);
    // 09:36:30 — the hold bar opened 30 s ago and has not closed.
    const v = decideDirection({ nowMs: OPEN_MS + 21.5 * MIN, bars1m: bars, oi: BULLISH_OI });
    expect(v.signal).toBeNull();
    expect(v.lastClosedBarIst).toBe('09:35');
  });
});

describe('entry rules — retest vs the 3-point ban', () => {
  it('bans a fresh second-bar entry within 3 pts of the level', () => {
    const v = run([BREAK, bar(21, 75025, 75026, 75000, 75002, 1500)]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/within 3 pts/);
  });

  it('waits for a retest once price has run past the second bar', () => {
    const v = run([BREAK, HOLD, bar(22, 75030, 75050, 75028, 75045)]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/waiting for a retest/);
  });

  it('takes the retest, even when it closes within 3 pts of the level', () => {
    const v = run([BREAK, HOLD, bar(22, 75030, 75050, 75028, 75045), bar(23, 75045, 75046, 75001, 75002)]);
    expect(v.signal).toBe('CE');
    expect(v.entryMode).toBe('retest');
  });

  it('kills the setup once a bar after the hold closes back through the stop', () => {
    const v = run([BREAK, HOLD, bar(22, 75030, 75031, 74985, 74990), bar(23, 74990, 75010, 74988, 75004)]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/closed back through/);
  });
});

describe('5-min trend filter (setup A only)', () => {
  it('blocks a PE breakdown when ±DI point the other way', () => {
    const v = run([bar(20, 74975, 74976, 74945, 74950, 3000), bar(21, 74950, 74955, 74940, 74945, 1500)]);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/DI disagree for PE/);
  });

  it('blocks below the ADX trend threshold', () => {
    const v = run(undefined, { config: { adxTrendMin: 101, adxFloor: 20 } });
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/ADX 100\.0 < 101/);
  });

  it('blocks when ADX is falling', () => {
    // Flat ADX with a negative tolerance reads as falling.
    const v = run(undefined, { config: { adxFlatTolerance: -0.1 } });
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/ADX falling/);
  });
});

describe('OI / PCR — writer-dominated reading', () => {
  it('rejects a CE breakout into call writing and put unwinding', () => {
    const bearish: OiView = {
      ...BULLISH_OI,
      strikes: [
        { strike: 74900, ceOiChange: 0, peOiChange: -3000 },
        { strike: 75000, ceOiChange: 4000, peOiChange: -1000 },
        { strike: 75100, ceOiChange: 6000, peOiChange: 0 },
      ],
      pcrNow: 0.9,
    };
    const v = run(undefined, { oi: bearish });
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/OI does not confirm/);
    expect(v.oi.CE!.contrary.join(' ')).toMatch(/Call writing/);
  });

  it('reads rising call OI above the level as contrary for CE, supporting for PE', () => {
    const view: OiView = {
      basis: 'vs_previous_day',
      minutesBack: null,
      strikes: [
        { strike: 74900, ceOiChange: 0, peOiChange: 0 },
        { strike: 75100, ceOiChange: 5000, peOiChange: 0 },
      ],
      pcrNow: 1,
      pcrPast: null,
    };
    expect(oiConfirmation('CE', 75000, view).confirmed).toBe(false);
    expect(oiConfirmation('CE', 75000, view).contrary[0]).toMatch(/Call writing/);
    expect(oiConfirmation('PE', 75000, view).confirmed).toBe(true);
  });

  it('needs supporters to outnumber contrary signals', () => {
    const view: OiView = {
      basis: 'intraday',
      minutesBack: 20,
      strikes: [
        { strike: 74900, ceOiChange: 0, peOiChange: 1000 },
        { strike: 75100, ceOiChange: 1000, peOiChange: 0 },
      ],
      pcrNow: 1,
      pcrPast: 1,
    };
    // Put writing (for) vs call writing (against): a tie is not confirmation.
    expect(oiConfirmation('CE', 75000, view).confirmed).toBe(false);
  });

  it('builds intraday OI change from a past scan, else falls back to previous-day OI', () => {
    const leg = (oi: number, previousOi: number) =>
      ({ oi, previousOi }) as unknown as ChainStrike['ce'];
    const strikes: ChainStrike[] = [{ strike: 75000, ce: leg(900, 1000), pe: leg(1500, 1000) }];
    const now = OPEN_MS + 60 * MIN;

    const intraday = buildOiView(strikes, 1.2, { t: now - 20 * MIN, pcr: 1.1, oi: [{ k: 75000, ce: 950, pe: 1200 }] }, now);
    expect(intraday.basis).toBe('intraday');
    expect(intraday.minutesBack).toBe(20);
    expect(intraday.strikes[0]).toEqual({ strike: 75000, ceOiChange: -50, peOiChange: 300 });
    expect(intraday.pcrPast).toBe(1.1);

    const daily = buildOiView(strikes, 1.2, null, now);
    expect(daily.basis).toBe('vs_previous_day');
    expect(daily.strikes[0]).toEqual({ strike: 75000, ceOiChange: -100, peOiChange: 500 });
    expect(daily.pcrPast).toBeNull();
  });
});

describe('setup B — rejection', () => {
  const WICKS = [bar(20, 74985, 74999, 74978, 74980), bar(21, 74982, 74998, 74977, 74979)];

  it('does not fade a strong trend (5-min ADX ≥ 25)', () => {
    const v = run(WICKS);
    expect(v.signal).toBeNull();
    expect(v.blockers.join(' ')).toMatch(/not fading a trend/);
  });

  it('signals PE on two rejecting wicks when the trend is weak', () => {
    const v = run(WICKS, { config: { rejectionAdxMax: 101 } });
    expect(v.signal).toBe('PE');
    expect(v.setup).toBe('rejection');
    expect(v.stopLevel!).toBeGreaterThan(v.level!);
  });
});

describe('session filters', () => {
  it('returns no signal inside a no-trade window', () => {
    const v = run(undefined, { nowMs: OPEN_MS + 5 * MIN });
    expect(v.signal).toBeNull();
    expect(v.blockers[0]).toMatch(/No-trade window 09:15–09:25/);
  });
});

describe('adx previous value', () => {
  it('reports the ADX one bar earlier', () => {
    const bars = trendThenBase();
    const now = adx(bars, 14);
    const before = adx(bars.slice(0, -1), 14);
    expect(now.adxPrevious).toBeCloseTo(before.adx!, 9);
  });
});
