# Do the SENSEX heavyweights and their option chains predict SENSEX? (28 Oct 2026; `tools/test_heavyweights_lead_sensex.py`)

Data: SENSEX 1-min index (1 year) + the 8 heavyweights fetched for the stock-option test (HDFCBANK, ICICIBANK,
RELIANCE, INFY, BHARTIARTL, TCS, AXISBANK, SBIN ≈ 60 % of SENSEX): stock price and ATM monthly CE/PE premium, OI and
volume per minute. Every 5 min (09:30–14:30), 29,955 readings; target = SENSEX move over the next 5/15/30 min.

| Signal (bullish = positive) | Correlation with next SENSEX move (H1 / H2) | Sign right |
|---|---|---|
| SENSEX own momentum | −0.05 … +0.02 | 48–49 % |
| Heavyweight basket momentum | −0.04 … +0.02 | 49–50 % |
| Basket move minus SENSEX move (lag) | up to +0.05 | 51–52 % |
| Stock options CE − PE premium change | −0.01 … +0.02 | 48–51 % |
| Stock options OI change (put writing) | −0.01 … +0.02 | 50–52 % |
| Stock options CE vs PE volume | −0.03 … +0.01 | 49–51 % |
| Breadth (stocks above their open) | −0.02 … +0.01 | 51–52 % |

Weak breadth (0–1 of 8 above the open) was followed by −15 to −37 pts over 5–30 min across overlapping readings, but
taken once per day (independent trades) it disappears: short on breadth ≤ 1, 30 min: −26.6 / +7.6 pts (H1 / H2),
−48.6 / −14.4 after a 22-pt futures cost; breadth 0 of 8 or ≥ 7 of 8: also negative after costs in at least one half.
The averages were driven by a few big down days, not a repeatable signal.

Conclusion: the heavyweights and their option chains move WITH SENSEX (it is computed from them) but do not lead it
by a tradeable margin at 5–30 min; every correlation is within ±0.05. No orders.
Full table: `docs/exports/heavyweights_lead_output.txt`.
