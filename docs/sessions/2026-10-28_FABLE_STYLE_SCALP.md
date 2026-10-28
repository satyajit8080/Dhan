# "Fable-style" spike scalp — 1-year test (28 Oct 2026; `tools/backtest_spike_scalp.py`)

Reconstruction of the shared Fable trades (27–28 Oct): ATM CE/PE bought right after a fast index move or a level
break, sold 1–5 minutes later. Rule tested: index moved >= X pts in 1–2 min → buy ATM CE (up) / PE (down) at the close
of that minute ("same", generous) or the next ("next"); exit after 1/2/3/5 min or at a small premium target/stop;
all days / expiry day / day before expiry. Real expired nearest-weekly premiums, 1 year. Costs per trade: SENSEX 5 pts
(lot 20), NIFTY 1.5 pts (lot 65).

| | SENSEX | NIFTY |
|---|---|---|
| Combinations | 322 | 182 |
| Positive after costs in both halves | 1 | 0 |
| Fable-like (SENSEX >= 60 / NIFTY >= 18 pts in 1 min, same-minute entry, 1–2 min hold, all days) | 1,559 trades, 47 % win before costs, 34–38 % after; −₹1,62,626 / −₹1,66,402 per lot | 771 trades, 48–49 % win before costs, 37–39 % after; −₹59,839 / −₹60,892 per lot |
| Only "both halves" survivor | SENSEX >= 80 in 1 min, day before expiry, same-minute entry, 2-min hold: 116 trades, +₹2,090 / +₹2,183 (1 of 322; needs a fill at the spike minute's close) | — |

Conclusion: buying right after a fast move wins about half the time before costs and loses after them; Fable's
12-of-13 winning trades over two days is a streak inside a losing distribution. No edge; no orders.
