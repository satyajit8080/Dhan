# Nifty 50 back-test, calendar 2025 (27 Oct 2026; `tools/backtest_nifty.py`)

Data: user CSV `nifty_1min_2025.csv`, 249 days, 1-min index bars (21 Oct 2025 = 1-hour Muhurat session, excluded).
Plausibility: price range 21,817–26,311 and the Muhurat session match 2025 Nifty; no Dhan Nifty data to compare bar by bar.
Same engine and rules as SENSEX (levels from 5-min candles, ±5 triggers rounded to 5, no-trade windows); option P&L
from the index with delta 0.5; premium points per strike BEFORE charges. Halves: Jan–Jun | Jul–Dec.

| Rule | +6 / −11, slip 4 (SENSEX rules) | Scaled +2 / −3.5, slip 1.3 |
|---|---|---|
| All breakouts | −1,565 / −625 | −943 / −322 |
| Fresh | −1,159 / −381 | −499 / −70 |
| Fresh + extreme | −759 / −326 | −300 / −66 |
| Fresh + extreme + calm (3-min ≤ 5) | −291 / −148 | −108 / −4 |
| Break-retest (user rule) | −280 / −56 | −120 / −43 |

Negative in both halves BEFORE charges for every rule. Not tradable. (Lot size and expiry day not used.)
