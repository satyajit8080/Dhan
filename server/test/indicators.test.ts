import { describe, it, expect } from 'vitest';
import {
  sma,
  ema,
  emaSeries,
  wilderSmooth,
  trueRanges,
  atr,
  adx,
  rsi,
  vwap,
  sessions,
  openingRange,
  swingRange,
  computeIndicators,
  istDateOf,
  istTimeOf,
} from '../src/indicators.js';
import type { Candle } from '../src/endpoints/historical.js';

/** Build a candle at a given IST wall-clock time. */
function bar(
  istDateTime: string,
  o: number,
  h: number,
  l: number,
  c: number,
  v: number | null = 1000,
): Candle {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(istDateTime)!;
  const [, y, mo, d, hh, mm] = m;
  const ms =
    Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh), Number(mm)) -
    (5 * 60 + 30) * 60 * 1000;
  return { timestampMs: ms, open: o, high: h, low: l, close: c, volume: v, openInterest: null };
}

describe('IST helpers', () => {
  it('reports the IST calendar date, not UTC', () => {
    // 2026-09-21 00:30 IST is 2026-09-20 19:00 UTC — the dates differ.
    const b = bar('2026-09-21 00:30', 1, 1, 1, 1);
    expect(new Date(b.timestampMs).toISOString().slice(0, 10)).toBe('2026-09-20');
    expect(istDateOf(b.timestampMs)).toBe('2026-09-21');
    expect(istTimeOf(b.timestampMs)).toBe('00:30');
  });
});

describe('sma / ema', () => {
  it('sma averages the last n values', () => {
    expect(sma([1, 2, 3, 4, 5], 5)).toBe(3);
    expect(sma([1, 2, 3, 4, 5], 2)).toBe(4.5);
  });

  it('returns null when there are too few points', () => {
    expect(sma([1, 2], 5)).toBeNull();
    expect(ema([1, 2], 5)).toBeNull();
  });

  it('ema on a constant series equals the constant', () => {
    expect(ema(new Array(50).fill(7), 20)).toBeCloseTo(7, 12);
  });

  it('ema is seeded with the SMA of the first period', () => {
    const s = emaSeries([1, 2, 3, 4, 5, 6], 3);
    expect(s[0]).toBeNull();
    expect(s[1]).toBeNull();
    expect(s[2]).toBeCloseTo(2, 12); // SMA(1,2,3)
    // then k = 2/(3+1) = 0.5
    expect(s[3]).toBeCloseTo(4 * 0.5 + 2 * 0.5, 12);
    expect(s[4]).toBeCloseTo(5 * 0.5 + 3 * 0.5, 12);
  });

  it('matches sma on a LINEAR ramp — both lag by (n-1)/2', () => {
    // A common misconception is that EMA always leads SMA. On a constant-slope
    // series they converge to the same value: last - (n-1)/2 * slope.
    const linear = Array.from({ length: 40 }, (_, i) => i + 1);
    expect(ema(linear, 10)!).toBeCloseTo(sma(linear, 10)!, 6);
    expect(ema(linear, 10)!).toBeCloseTo(40 - 4.5, 6);
  });

  it('leads sma on an ACCELERATING series, which is where it earns its keep', () => {
    const accel = Array.from({ length: 40 }, (_, i) => i * i);
    expect(ema(accel, 10)!).toBeGreaterThan(sma(accel, 10)!);
  });
});

describe('wilderSmooth is NOT an EMA', () => {
  it('uses 1/n, not 2/(n+1) — the classic ATR/ADX mistake', () => {
    const v = [10, 12, 14, 16, 18, 20];
    const w = wilderSmooth(v, 3);
    // seed = mean(10,12,14) = 12
    expect(w[2]).toBeCloseTo(12, 12);
    // then prev + (x - prev)/3
    expect(w[3]).toBeCloseTo(12 + (16 - 12) / 3, 12);
    // an EMA with k = 2/4 = 0.5 would give 14 — materially different
    expect(w[3]).not.toBeCloseTo(14, 6);
  });
});

describe('true range and ATR', () => {
  const candles = [
    bar('2026-09-21 09:15', 100, 105, 95, 102),
    bar('2026-09-21 09:20', 102, 108, 101, 107),
    bar('2026-09-21 09:25', 107, 110, 99, 100),
  ];

  it('first bar has no previous close, so TR is high-low', () => {
    expect(trueRanges(candles)[0]).toBe(10);
  });

  it('accounts for gaps through the previous close', () => {
    // bar 2: max(108-101, |108-102|, |101-102|) = 7
    expect(trueRanges(candles)[1]).toBe(7);
    // bar 3: max(110-99, |110-107|, |99-107|) = 11
    expect(trueRanges(candles)[2]).toBe(11);
  });

  it('ATR is null until there are enough bars', () => {
    expect(atr(candles, 14)).toBeNull();
  });

  it('ATR of a constant-range series equals that range', () => {
    const flat = Array.from({ length: 40 }, (_, i) =>
      bar(`2026-09-21 ${String(9 + Math.floor(i / 12)).padStart(2, '0')}:${String((i * 5) % 60).padStart(2, '0')}`, 100, 110, 100, 105),
    );
    expect(atr(flat, 14)).toBeCloseTo(10, 6);
  });
});

describe('ADX', () => {
  it('is null on too short a series', () => {
    const few = Array.from({ length: 10 }, (_, i) =>
      bar(`2026-09-21 09:${String(15 + i).padStart(2, '0')}`, 100, 101, 99, 100),
    );
    expect(adx(few, 14).adx).toBeNull();
  });

  it('a relentless uptrend gives +DI above -DI and a high ADX', () => {
    const up = Array.from({ length: 60 }, (_, i) => {
      const base = 100 + i * 2;
      return bar(
        `2026-09-2${1 + Math.floor(i / 30)} ${String(9 + Math.floor((i % 30) / 12)).padStart(2, '0')}:${String(((i % 30) * 5) % 60).padStart(2, '0')}`,
        base,
        base + 2,
        base - 0.5,
        base + 1.5,
      );
    });
    const r = adx(up, 14);
    expect(r.plusDi!).toBeGreaterThan(r.minusDi!);
    expect(r.adx!).toBeGreaterThan(40);
  });

  it('a relentless downtrend gives -DI above +DI', () => {
    const down = Array.from({ length: 60 }, (_, i) => {
      const base = 200 - i * 2;
      return bar(
        `2026-09-2${1 + Math.floor(i / 30)} ${String(9 + Math.floor((i % 30) / 12)).padStart(2, '0')}:${String(((i % 30) * 5) % 60).padStart(2, '0')}`,
        base,
        base + 0.5,
        base - 2,
        base - 1.5,
      );
    });
    const r = adx(down, 14);
    expect(r.minusDi!).toBeGreaterThan(r.plusDi!);
    expect(r.adx!).toBeGreaterThan(40);
  });

  it('a flat chop gives a low ADX', () => {
    const chop = Array.from({ length: 60 }, (_, i) => {
      const base = 100 + (i % 2);
      return bar(
        `2026-09-2${1 + Math.floor(i / 30)} ${String(9 + Math.floor((i % 30) / 12)).padStart(2, '0')}:${String(((i % 30) * 5) % 60).padStart(2, '0')}`,
        base,
        base + 0.5,
        base - 0.5,
        base,
      );
    });
    expect(adx(chop, 14).adx!).toBeLessThan(30);
  });
});

describe('RSI', () => {
  it('is 100 when every bar gains', () => {
    const up = Array.from({ length: 30 }, (_, i) => 100 + i);
    expect(rsi(up, 14)).toBeCloseTo(100, 6);
  });

  it('is 0 when every bar loses', () => {
    const down = Array.from({ length: 30 }, (_, i) => 100 - i);
    expect(rsi(down, 14)).toBeCloseTo(0, 6);
  });

  it('is null with too little data', () => {
    expect(rsi([1, 2, 3], 14)).toBeNull();
  });
});

describe('VWAP', () => {
  it('weights by volume, not by bar count', () => {
    const c = [
      bar('2026-09-21 09:15', 100, 100, 100, 100, 1),
      bar('2026-09-21 09:20', 200, 200, 200, 200, 99),
    ];
    // Heavily weighted to the second bar.
    expect(vwap(c)!).toBeCloseTo((100 * 1 + 200 * 99) / 100, 9);
  });

  it('returns NULL rather than a fake average when there is no volume', () => {
    const c = [
      bar('2026-09-21 09:15', 100, 100, 100, 100, 0),
      bar('2026-09-21 09:20', 200, 200, 200, 200, null),
    ];
    expect(vwap(c)).toBeNull();
  });
});

describe('session structure', () => {
  const twoDays = [
    bar('2026-09-21 09:15', 100, 110, 99, 105, 500),
    bar('2026-09-21 09:20', 105, 112, 104, 108, 600),
    bar('2026-09-21 15:25', 108, 115, 107, 112, 700),
    bar('2026-09-22 09:15', 113, 118, 112, 117, 800),
    bar('2026-09-22 09:20', 117, 120, 111, 119, 900),
  ];

  it('groups by IST date, oldest first', () => {
    const s = sessions(twoDays);
    expect(s.map((x) => x.date)).toEqual(['2026-09-21', '2026-09-22']);
  });

  it('computes session OHLC and summed volume', () => {
    const [d1, d2] = sessions(twoDays);
    expect(d1!.open).toBe(100);
    expect(d1!.high).toBe(115);
    expect(d1!.low).toBe(99);
    expect(d1!.close).toBe(112);
    expect(d1!.volume).toBe(1800);
    expect(d2!.high).toBe(120);
    expect(d2!.low).toBe(111);
  });

  it('opening range covers only the most recent session', () => {
    const or = openingRange(twoDays, 15);
    // 22-Sep only. Both its 09:15 and 09:20 bars fall inside the 15-minute
    // window, so the range spans them; 21-Sep is excluded entirely.
    expect(or!.bars).toBe(2);
    expect(or!.high).toBe(120);
    expect(or!.low).toBe(111);
    expect(or!.fromIst).toBe('09:15');
  });

  it('opening range EXCLUDES bars past the window', () => {
    const withLate = [
      bar('2026-09-22 09:15', 113, 118, 112, 117),
      bar('2026-09-22 09:20', 117, 120, 111, 119),
      // 09:35 is beyond 09:15 + 15min — its extremes must not leak in.
      bar('2026-09-22 09:35', 119, 999, 1, 500),
    ];
    const or = openingRange(withLate, 15);
    expect(or!.bars).toBe(2);
    expect(or!.high).toBe(120);
    expect(or!.low).toBe(111);
  });

  it('swingRange looks back over the last n bars regardless of session', () => {
    const r = swingRange(twoDays, 3);
    expect(r!.bars).toBe(3);
    expect(r!.high).toBe(120);
    expect(r!.low).toBe(107);
  });

  it('returns null on an empty series instead of guessing', () => {
    expect(openingRange([], 15)).toBeNull();
    expect(swingRange([], 20)).toBeNull();
    expect(computeIndicators([])).toBeNull();
  });
});

describe('computeIndicators', () => {
  const series = Array.from({ length: 80 }, (_, i) => {
    const base = 74000 + i * 5;
    const hh = String(9 + Math.floor(i / 12)).padStart(2, '0');
    const mm = String((i * 5) % 60).padStart(2, '0');
    return bar(`2026-09-21 ${hh}:${mm}`, base, base + 8, base - 6, base + 3, 1000 + i);
  });

  const ind = computeIndicators(series)!;

  it('reports bar count and the last close', () => {
    expect(ind.bars).toBe(80);
    expect(ind.lastClose).toBe(series[79]!.close);
  });

  it('fills the EMAs it has enough data for, and nulls the rest', () => {
    expect(ind.ema.p9).not.toBeNull();
    expect(ind.ema.p20).not.toBeNull();
    expect(ind.ema.p50).not.toBeNull();
    expect(ind.ema.p200).toBeNull(); // only 80 bars
  });

  it('produces ATR, ADX and RSI', () => {
    expect(ind.atr14).not.toBeNull();
    expect(ind.adx.adx).not.toBeNull();
    expect(ind.rsi14).not.toBeNull();
  });

  it('computes momentum as points, not percent', () => {
    expect(ind.momentum.roc5).toBeCloseTo(25, 6); // 5 bars * 5 points
    expect(ind.momentum.roc15).toBeCloseTo(75, 6);
  });

  it('reports relative volume', () => {
    expect(ind.volume.lastBar).toBe(1079);
    expect(ind.volume.relative).not.toBeNull();
  });

  it('exposes structure without emitting a verdict', () => {
    expect(ind.structure.currentSession!.date).toBe('2026-09-21');
    expect(ind.structure.openingRange15).not.toBeNull();
    expect(ind.structure.last20Bars!.bars).toBe(20);
    // No trend label, no bias, no breakout level anywhere in the output.
    expect(JSON.stringify(ind)).not.toMatch(/breakout|bullish|bearish|signal/i);
  });

  it('flags a volumeless series rather than faking VWAP', () => {
    const noVol = series.map((c) => ({ ...c, volume: 0 }));
    const r = computeIndicators(noVol)!;
    expect(r.vwap).toBeNull();
    expect(r.vwapNote).toMatch(/no volume/i);
    expect(r.volume.relative).toBeNull();
  });
});
