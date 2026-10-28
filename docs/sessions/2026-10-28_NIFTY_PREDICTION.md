# NIFTY: same prediction tests with 52 stocks (28 Oct 2026; `tools/run_nifty_tests.py`)

Data (read-only Dhan): NIFTY 50 1-min index (IDX_I 13) 27 Oct 2025 – 27 Oct 2026, nearest-weekly NIFTY ATM CE/PE 1-min
premiums with volume and OI (366 requests, 1 failed), 52 stocks (30 SENSEX + 22 NIFTY), ids from Dhan instrument search.
Influence of each stock estimated by non-negative regression of NIFTY 1-min moves on the stocks (first half, R² 0.984;
49 stocks with weight > 0): HDFCBANK 12.0 %, RELIANCE 8.7, ICICIBANK 8.4, BHARTIARTL 4.9, INFY 4.3, LT 4.3, SBIN 3.9,
AXISBANK 3.4, ITC 3.0, M&M 2.8 … (`docs/exports/nifty_influence_weights.json`). NIFTY costs: lot 65, ~1.5 pts per ATM
option round trip, ~13 pts per future round trip.

| Test | NIFTY result |
|---|---|
| Next 1-min index direction (out-of-sample) | 53.3–53.8 % right (SENSEX 58.5 %: NIFTY is computed from NSE prices, so less lag) |
| Buying the NIFTY ATM option on those predictions | −0.6 to −1.8 pts per trade before the 1.5-pt cost |
| Walk-forward model, 40 features, entry at t+1, 15 / 30 / 60 min / close | 45.7–52.6 % right; chance baseline 48.5–51.8 % |
| Option net (most confident 10 %) | −0.0 … −30.1 pts in every horizon and half |
| Future net | negative except to-close in the second half (+4.6 / +7.1) while the first half was −16 / −20 |

Conclusion: identical to SENSEX — stocks, charts and option data do not predict NIFTY beyond chance once the index lag
is removed; NIFTY option buying on these signals loses. Full output: `docs/exports/nifty_prediction_output.txt`. No orders.
