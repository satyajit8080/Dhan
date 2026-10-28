# Firm rules (paper only)

Read before every decision. Nothing here places, modifies or cancels an order.

1. **Paper only.** No live order code, no auto-trading, no unattended login. Real money is the owner's own decision,
   made manually in Dhan.
2. **Market:** SENSEX weekly options (BSE, lot 20). NIFTY (lot 65) for research only.
3. **Hiring rule:** a strategy joins only after ALL of:
   - positive after costs in both halves of the history (chosen on the first half, checked on the second);
   - better than the chance baseline (same search on sign-flipped outcomes);
   - entry one minute after the signal still positive (no look-ahead);
   - 2+ weeks of forward paper results inside its backtest range.
4. **Firing rule:** paper results below the backtest's worst case (worst week or max drawdown) → moved to
   `GRAVEYARD.md`, never rehired without a new, different test.
5. **Risk limits (per ₹1 lakh of capital):** max 3 condor lots; keep the backtest's worst week (₹10,352/lot) in reserve;
   stop for the month after a loss of 2 × the worst week.
6. **Costs always included:** SENSEX option ₹70/order + slippage (~5 pts per round trip); condor ₹250 + 6 pts.
7. **No strategy change after a single trade.** Changes need a new backtest.
8. **Security:** no tokens, PINs or TOTP secrets in files, logs or chat.

## Hired
| Strategy | Rule | Backtest (3.4 yrs, 1 lot) |
|---|---|---|
| SENSEX weekly condor | one trading day before expiry, 09:30: sell delta-0.10 CE and PE (0.07 when the entry straddle > 1.2 × its 10-week median), buy wings 1,000 pts further out, hold to expiry, no adjustments | 167 weeks, 91 % win, +₹75,206, worst week −₹10,352, max drawdown −₹10,979 (wings beyond ±1,000 model-priced: needs forward paper confirmation) |
