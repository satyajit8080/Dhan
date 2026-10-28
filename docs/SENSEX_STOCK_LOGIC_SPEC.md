# SENSEX stock-breadth logic — spec for the SENSEX chat (read-only, paper only)

Source: back-tests in `docs/sessions/2026-10-28_*` (1 year of 1-min data, 30 stocks, SENSEX + stock options).
This spec only DESCRIBES the market and reports one tested rule. It must not place orders and must not present any
output as a buy/sell call. Tested result: no intraday direction rule beat chance after costs; option buying lost money.

## 1. Stocks and measured influence on SENSEX
Measured from 84,458 one-minute moves (non-negative regression of SENSEX % moves on the stocks' NSE % moves,
R² 0.78; lower than NIFTY's 0.97 because SENSEX is computed from BSE prints and lags by seconds). Use as weights.

| Stock | % | Stock | % | Stock | % |
|---|---|---|---|---|---|
| HDFCBANK | 13.8 | ITC | 3.1 | ADANIPORTS | 1.7 |
| RELIANCE | 10.6 | BAJFINANCE | 3.0 | HCLTECH | 1.5 |
| ICICIBANK | 9.5 | TCS | 2.9 | ASIANPAINT | 1.4 |
| LT | 6.3 | ETERNAL | 2.7 | TECHM | 1.4 |
| BHARTIARTL | 5.6 | HINDUNILVR | 2.2 | TITAN | 1.2 |
| SBIN | 5.2 | BEL | 2.2 | TMPV (Tata Motors PV) | 0.7 |
| INFY | 5.0 | POWERGRID | 2.0 | BAJAJFINSV | 0.3 |
| AXISBANK | 3.8 | MARUTI | 1.9 | TRENT | 0.0 |
| M&M | 3.4 | ULTRACEMCO | 1.9 | TATASTEEL | 0.0 |
| KOTAKBANK | 3.3 | SUNPHARMA | 1.8 | NTPC | 1.7 |

NSE_EQ security ids (verified via Dhan instrument search, 28 Oct 2026): HDFCBANK 1333, ICICIBANK 4963, RELIANCE 2885,
INFY 1594, BHARTIARTL 10604, LT 11483, ITC 1660, TCS 11536, AXISBANK 5900, KOTAKBANK 1922, SBIN 3045, M&M 2031,
BAJFINANCE 317, HINDUNILVR 1394, SUNPHARMA 3351, HCLTECH 7229, MARUTI 10999, NTPC 11630, ULTRACEMCO 11532,
TITAN 3506, TMPV 3456, POWERGRID 14977, BAJAJFINSV 16675, TATASTEEL 3499, ASIANPAINT 236, ADANIPORTS 15083,
TECHM 13538, ETERNAL 5097, TRENT 1964, BEL 383. SENSEX index: IDX_I 51. Re-check membership on bseindia.com.

Sectors: IT = INFY, TCS, HCLTECH, TECHM · BANKS = HDFCBANK, ICICIBANK, AXISBANK, KOTAKBANK, SBIN ·
FIN = BAJFINANCE, BAJAJFINSV · ENERGY = RELIANCE, NTPC, POWERGRID · OTHER = the rest.

## 2. Per-stock data and indicators (each refresh)
From one read-only quote call (NSE_EQ ids + IDX_I 51) and the stock's 1-min candles:
- change vs previous close (%), change vs day open (%), 15-min and 30-min change (%)
- state score = (+1/−1 above/below open) + (above/below prev close) + (up/down over 15 min) + (up/down over 30 min);
  BULLISH if score ≥ 3, WEAK if ≤ −3, else MIXED
- contribution to SENSEX (pts) = weight% / 100 × change vs prev close % / 100 × SENSEX level
Optional (stock ATM monthly CE/PE from the option chain): CE − PE premium change over 15 min, PE − CE OI change, CE
share of option volume. Tested: ~50 % (no information) — show only as context.

## 3. Aggregates to display
- SENSEX vs previous close (%, pts) and vs open
- breadth = stocks above previous close / 30; counts of BULLISH / WEAK / MIXED
- top 5 drags and top 5 supports by contribution (pts); sector contributions (IT, BANKS, FIN, ENERGY, OTHER)
- concentration = share of the absolute weighted move from the top 3 stocks (≥ 60 % = narrow move)
- check: weighted sum of stock moves vs actual SENSEX change (should be within ~0.1 %)

## 4. The one tested rule (paper observation only)
MIDDAY MOMENTUM: at 12:30 IST, if SENSEX is more than 0.6 % from the previous close, log a PAPER observation in that
direction, closed at 15:25. Tested over 3.4 years: direction right 64 % / 56 % / 58 % (2023-24 / 2024-25 / 2025-26),
average +19 / +41 / +49 SENSEX pts. Fragile (the same rule at 12:00 failed). Buying the ATM CE/PE on it LOST
17–48 pts per trade every year (time decay); a future netted −3 / +14 / +26 pts after costs. Show as
"observation, not a trade". Breadth ≥ 70 % / ≤ 30 % at 12:30–13:30 showed similar but unconfirmed behaviour.

## 5. What NOT to claim (tested and failed)
- next 1-min / 5-min candle prediction (index-lag artifact; options already moved)
- breakouts (FRESH_EXTREME, FRESH_CALM, break-retest, opening range), premium spikes, PCR/OI/max-pain signals
- any combination of the above in a walk-forward model: 44–52 % right, chance 46–52 %

## 6. Guardrails
Read-only endpoints only (quote, intraday candles, option chain). No order placement, modification or cancellation.
Never print or store the access token. No CE/PE recommendation; label every rule output "paper observation".
