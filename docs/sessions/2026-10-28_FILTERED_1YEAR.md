# FILTERED paper variant — 1-year test on real option premiums (28 Oct 2026; `tools/backtest_filtered.py`)

FILTERED = FIRST_TOUCH crossings with the day trend (index vs its first bar, a proxy for the live futures-vs-open
check), a fresh level (< 3 touches) and a session range >= 300 pts. ATM CE/PE at the crossing, +6 / −11 premium,
10-min time stop, real expired nearest-weekly premiums, 247 days (27 Oct 2025 – 26 Oct 2026), ₹70 per trade, lot 20.

| Version | Trades | Win % | Net 1 lot, no slippage (H1 / H2) | With 4-pt stop slippage, total |
|---|---|---|---|---|
| **FILTERED** | 782 | 50.0 | −₹60,995 / −₹32,960 | −₹1,25,235 |
| FIRST_TOUCH (no filter) | 5,125 | 49.8 | −₹3,36,731 / −₹2,80,670 | −₹8,22,600 |
| trend only | 3,427 | 50.2 | −₹2,25,924 / −₹1,82,360 | −₹5,44,524 |
| fresh only | 1,576 | 49.9 | −₹1,10,971 / −₹78,892 | −₹2,52,983 |
| range >= 300 only | 3,859 | 49.7 | −₹2,74,799 / −₹1,91,428 | −₹6,21,107 |
| FILTERED, range >= 200 / >= 400 | 1,038 / 525 | 49.7 / 49.0 | negative both halves | −₹1,67,455 / −₹86,310 |

The filters cut the number of trades (and so the total loss) but do not raise the win rate: ~50 % against a 64.7 %
break-even for +6/−11. Today's 70 % (10 trades, 28 Oct morning) is a small-sample result. No edge; no change; paper only.
