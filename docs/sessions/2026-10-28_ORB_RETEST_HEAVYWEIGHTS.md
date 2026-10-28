# Opening-range breakout + retest + heavyweight confirmation (external-review strategy) — 28 Oct 2026

Tool: `tools/backtest_orb_retest.py`. 1 year, real nearest-weekly ATM premiums (strike rolls bridged with delta 0.5).
Rules as proposed: 09:15–09:30 range; a 5-min close beyond it; a later 5-min candle retests the level and closes beyond
again → buy ATM CE/PE at that close; >= 70 % of the top-10 basket's weight above its own average price since the open
(VWAP stand-in, no stock volume available) and the basket's 15-min weighted return on the trade side; >= 3 trading days
to expiry; stop = index through the retest candle's low/high, target = 2R, time stop = 3 five-min candles without a new
favourable extreme, flat 15:15; max 2 trades a day, stop after 2 losses. Costs: NIFTY 1.5 pts (lot 65), SENSEX 5 (lot 20).
Not tested: the option-chain check (only the ATM strike's history exists).

| Index | Version | Trades | Win % | Net per lot H1 / H2 | Total |
|---|---|---|---|---|---|
| NIFTY | price only, all days | 200 | 23.5 | −₹48,318 / −₹29,858 | −₹78,176 |
| NIFTY | price only, >= 3 days to expiry | 113 | 26.5 | −₹28,782 / −₹11,697 | −₹40,479 |
| NIFTY | + heavyweights, all days | 114 | 20.2 | −₹30,017 / −₹21,359 | −₹51,376 |
| NIFTY | **as proposed** | 69 | 21.7 | −₹16,646 / −₹14,947 | **−₹31,593** |
| SENSEX | price only, all days | 397 | 30.7 | −₹41,833 / −₹35,361 | −₹77,194 |
| SENSEX | price only, >= 3 days to expiry | 139 | 29.5 | −₹12,613 / −₹14,408 | −₹27,021 |
| SENSEX | + heavyweights, all days | 229 | 33.2 | −₹20,831 / −₹27,387 | −₹48,218 |
| SENSEX | **as proposed** | 81 | 27.2 | −₹14,175 / −₹7,948 | **−₹22,123** |

Most trades end at the structural stop or the time stop; the 2R target is reached in 10–20 % of trades. The
heavyweight filter cuts the trade count but does not raise the win rate. Negative in both halves for every version.
No edge; no orders.
