# Option buying on NSE stock options (28 Oct 2026; `tools/backtest_stock_option_buying.py`)

Data fetched read-only from Dhan `/charts/rollingoption` (OPTSTK, nearest MONTH, ATM CALL and PUT, 1-min, with
volume and the stock price), 27 Oct 2025 – 27 Oct 2026, 2,928 requests, 0 errors. Stocks (NSE_EQ ids from Dhan's
instrument search): RELIANCE 2885, HDFCBANK 1333, ICICIBANK 4963, SBIN 3045, INFY 1594, TCS 11536, AXISBANK 5900,
BHARTIARTL 10604. Raw data kept outside the repo.

Rules: (A) the option's own premium jumps >= 5/10/15 % in 1/3/5 min (± volume spike, ± 15-min high); (B) the stock
breaks its 09:15–09:30 range → buy ATM CE (up) / PE (down); (C) wide-open day → buy the straddle at 09:45. Exits
+10/20/40 %, −10/20 %, 10/30/60 min or 15:15. Cost 1.2 % of premium per trade (bid-ask + statutory; brokerage
excluded). Results in % of premium per trade. Two exit models: intrabar fills at the level, and the stricter
close-only exits.

| Check | Result |
|---|---|
| 936 combinations, 8 stocks, intrabar exits | 16 positive in both halves |
| Same, close-only exits | 5 positive in both halves |
| Best on the first 6 stocks: (B) opening-range break, +40 % / −10 %, 10 min | +0.79 % / +0.55 % per trade (2,094 trades) |
| **Same rule on the 2 stocks not used to find it (AXISBANK, BHARTIARTL)** | **−0.54 % / −0.12 % (close exits), −0.16 % / +0.23 % (intrabar); 724 trades** |
| Premium jump with a volume spike, follow-through | −1 % to −2.8 % over the next 5–30 min (spikes reverse, as on SENSEX) |

Conclusion: stock option buying looks marginally better than SENSEX (the premium is a larger % move per stock move),
but the one rule that survived both halves failed on fresh stocks, and it was also negative on TCS. No reliable
edge; +0.5 % per trade would in any case be erased by brokerage on small lots. No orders.
Full output: `docs/exports/stock_option_buying_output.txt`.
