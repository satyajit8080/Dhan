# Other option-chain strategies on real expired options, 27 Oct 2026

**Data and method:**
- **Data:** the same 1-year rolling-ATM history (expiry number 1, nearest weekly).
- **Script:** `tools/backtest_short_straddle.py`. It is paper research only.
- **Pricing:** the fixed-strike straddle is rebuilt from the rolling ATM with a Bachelier fit around the forward (K + CE − PE). Rolling options are priced off the forward, which can sit about 150 pts above spot. At strike switches the fit is accurate to 0.7% median (p90 2.8%).
- **Costs:** 1 lot (20), 4 pts slippage and ₹200 charges per trade.

**Short ATM straddle, enter 09:20, exit 15:15:**
- All days: 246 trades, 65.4% win, +689 pts, **−₹35,415 net** (worst day −1,118 pts).
- With a 25% stop on all days: **−₹24,655 net**.
- Best cell, **Wednesday with a 25% stop: 51 trades, 68.6% win, +870 pts, +₹7,202 net (H1 +₹5,331 / H2 +₹1,871)**.
  - This is the only positive cell in both halves, out of 17 tried, so it may be luck.
- Thursday (expiry) is negative without a stop. With a 40% stop it is about −₹460.

**OI signal** (ATM put OI change minus call OI change after 09:20 → direction to close): 46–51% correct, which is a coin flip.

**Conclusion:**
- Selling premium performs better than buying, because time decay works for the seller.
- Still, only one weekday cell is positive in both halves. The worst losses of 250–1,100 pts (₹5,000–22,000 per lot) and the large margin make it unsuitable for real money on this evidence.
- No strategy change. A Wednesday short-straddle paper observer could be added only if requested.
