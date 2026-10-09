/**
 * The 18-Sep-2026 15:30 IST SENSEX close. Offline fixture — no network, ever.
 *
 * This is the snapshot that exposed the defect: the index LTP was 321 points
 * below the true forward, and the 74500 strike printed 16.0% IV on the call
 * against 8.6% on the put. Under put-call parity those must be equal.
 */
export const GM = {
  asOfIst: '2026-09-18T15:30:00',
  expiry: '2026-09-24',
  r: 0.065,
  futuresLtp: 74656.75,
  /** securityId 51 / IDX_I. Present ONLY so the gate can warn about it. */
  indexLtp: 74294.96,
  atmHint: 74300,
  chain: [
    { strike: 74100, callPrice: 713.6, putPrice: 208.3 },
    { strike: 74200, callPrice: 655.0, putPrice: 236.7 },
    { strike: 74300, callPrice: 588.0, putPrice: 270.05 },
    { strike: 74400, callPrice: 517.1, putPrice: 303.7 },
    { strike: 74500, callPrice: 461.55, putPrice: 345.1 },
  ],
  expected: {
    T: 0.01643835616438356,
    calendarDays: 6,
    parityForward: 74616.5745,
    perStrikeSpread: 12.907,
    divergenceVsFuture: 40.1755,
    indexDivergence: 321.6145,
    ce74500: {
      ivPct: 10.5167,
      delta: 0.54824,
      thetaPerDay: -33.0798,
      vega: 37.838,
    },
    maxCePeIvGapPct: 0.5,
  },
} as const;
