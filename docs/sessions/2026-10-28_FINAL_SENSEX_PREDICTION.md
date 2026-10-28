# Final test: predicting SENSEX from all 30 stocks, charts and option data (28 Oct 2026)

Data (read-only, local after download): SENSEX 1-min index (1 year) and 3.4 years of SENSEX minute prices from rolling
option files; SENSEX nearest-weekly ATM CE/PE 1-min premiums and OI; all 30 SENSEX stocks (NSE price + nearest-monthly
ATM CE/PE premium, OI, volume), 10,980 Dhan `/charts/rollingoption` requests (15 failed), ids from Dhan's instrument
search. Raw data kept outside the repo.

## 1. Next 1-min / 5-min candle (`tools/predict_next_candle.py`)
Index direction looked predictable (58.5 % next minute out-of-sample), driven by the SENSEX ATM CE−PE change and NSE
stock prices leading the BSE-computed index by seconds. Buying the SENSEX ATM option on those predictions lost 2–5 pts
(1 min) and 5–11 pts (5 min) before costs. Three external reviews (uploaded 28 Oct) agree: index staleness, not
tradeable for retail.

## 2. Breadth / sectors at midday (`tools/test_breadth_sectors.py`, 30 stocks)
216 rule × time × horizon combinations; 9 were > 22 pts (futures cost) in both halves — the chance baseline (outcomes
randomly sign-flipped per day) gives 3–17. Contradictory survivors (follow at 12:30, fade at 11:00) confirm noise.
Midday momentum on 3.4 years (`tools/test_midday_momentum_3y*.py`): SENSEX > 0.6 % from the previous close at 12:30
continued to 15:25 in all three years (+19 / +41 / +49 pts, 56–64 % right), but buying the ATM option lost 17–48 pts
per trade every year; futures −3 / +14 / +26 pts after a 22-pt cost.

## 3. Final walk-forward model (`tools/final_predict_sensex.py`)
40 features every 5 min (16,215 decisions): bullish/weak state of each of the 30 stocks, SENSEX chart (momentum,
RSI, MA slope, opening range, new high/low, range position, volatility, FRESH signals), SENSEX ATM options
(CE−PE, straddle, OI), stock options (CE−PE, straddle, OI, CE/PE volume), sectors, breadth, dispersion. Entry one
minute after the signal (removes the index lag); monthly retraining on past months only; most confident 10 % traded.

| Horizon | Direction right (H1 / H2) | Buy SENSEX ATM option, net pts | Future, net pts |
|---|---|---|---|
| 15 min | 46.9–48.4 % / 49.6–51.3 % | −3.9 … −13.9 | −8.3 … −30.2 |
| 30 min | 46.6–46.7 % / 50.1–51.5 % | −5.4 … −26.0 | −8.7 … −48.2 |
| 60 min | 45.3–46.3 % / 50.9–51.3 % | −10.6 … −41.0 | −63.1 … +1.4 |
| to 15:25 | 48.9–49.3 % / 52.4–52.6 % | −37.8 … −66.3 | −29.8 … +34.0 |
| Chance baseline | 46.2–52.4 % | | |

Full output: `docs/exports/final_predict_sensex_output.txt`, `docs/exports/breadth_sectors_output.txt`.

## Conclusion
With the index-lag artifact removed and walk-forward validation, the bullish/weak state of the stocks, chart signals
and option-chain data (premium, straddle, OI, PCR-style, volume) do not predict SENSEX better than chance, and buying
SENSEX options on any of them loses money at every horizon. The only multi-year profitable approach found in this
project remains defined-risk premium selling (weekly condor, paper-tracked). No orders.
