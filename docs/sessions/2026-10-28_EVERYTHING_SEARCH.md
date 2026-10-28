# Broad rule search — "test everything" (28 Oct 2026; `tools/backtest_everything.py`)

Data: SENSEX 1-min index 27 Oct 2025 – 26 Oct 2026 (247 days) + real expired ATM option premiums and ATM open interest.
One decision per day per rule at a fixed entry time (every 15 min 09:30–14:45), held 5/15/30/60 min or to 15:25.
Rules (each FOLLOW and FADE): momentum 5/15/30/60/120 min, price vs day open, opening gap, previous day, position in
the day's range, ATM open-interest change (puts − calls, the NSE-analyzer idea), plus always-UP / always-DOWN.
Filters: none, each weekday, expiry day, day before expiry, high / low IV (09:30 straddle vs 20-day median).
Instruments: ATM option (real premium, ₹70 + 1.5 pts) or SENSEX future (index pts, ₹400 + 2 pts), 1 lot (20).
Honest check: best rules picked on the first half only, then shown on the second half; chance baseline = the same
search on outcomes randomly sign-flipped per day (no edge by construction).

## Result
- 41,988 combinations with at least 20 trades in each half.
- **Positive after costs in both halves: 1,436 (3.42 %). Chance baseline: 3.07 %, 3.61 %, 3.94 %.** The real data
  produces no more "winners" than random data does.
- Of the top 100 first-half picks, only 32 stayed positive in the second half (worse than a coin flip). The best
  first-half rule (60-min momentum follow, 11:45, futures to close: +₹1,97,632) lost ₹28,625 in the second half.
- Every surviving combination is a futures hold-to-close rule; no option-buying rule survives (premium decay + costs).

Raw output: `docs/exports/everything_search_output.txt`.

Conclusion: across every rule family the data supports, the number of profitable-looking rules equals what luck alone
produces. Nothing here is a reliable edge. If any are tracked further, it must be forward paper only, e.g. the three
most stable futures rules (60-min momentum follow on high-IV days at 12:00 to close; 5-min momentum fade on high-IV
days at 10:30 to close; Monday opening-gap fade) — treated as hypotheses, not strategies. No orders.
