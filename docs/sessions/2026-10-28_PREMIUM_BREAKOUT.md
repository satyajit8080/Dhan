# Buying when the CE/PE premium suddenly starts moving (28 Oct 2026; `tools/backtest_premium_breakout.py`)

Real expired nearest-weekly ATM CE/PE 1-min premiums, 1 year. Signal on the option's own price (same strike over the
lookback, so rolling strike switches are not counted): premium up >= 3/5/8/12 % in 1/2/3/5 min and/or above its
15-min high. Buy at the signal close; target +5/10/20 %, stop −5/10 %, time 5/10/20 min; 1.5 pts + ₹70 per trade.

- **1,584 buy combinations: 0 positive after costs in either half.** Average before costs −1.2 pts per trade.
- Why: after a sudden jump the premium usually falls back. Jump >= 8 % in 2 min → after 10 min avg −7.5 %, higher in
  only 27 % of 214 cases; jump >= 12 % in 3 min → after 10 min −12.2 %, higher in 20 %.
- The opposite (SELL the spiked option, stop +20 %, hold 20 min) is slightly positive in both halves in a few
  versions, e.g. jump >= 8 % in 2 min on non-expiry days: 135 trades, 46 % wins, +₹2,250 / +₹3,552 (~₹5,800 a year per
  lot) — small, a naked short needs ~₹1.5–2 lakh margin, and several versions were tried. Forward paper only.

Conclusion: chasing a sudden CE/PE premium breakout loses money; spikes tend to reverse. No orders.
