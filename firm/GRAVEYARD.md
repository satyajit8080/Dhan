# Graveyard — tested and failed (do not rehire)

All on real option premiums with costs unless noted. Details in `docs/sessions/2026-10-28_*`.

| Strategy | Result |
|---|---|
| FIRST_TOUCH breakout (+6/−11) | 5,105 trades, 50 % win, −₹6.1 lakh/lot/yr |
| FILTERED (trend + fresh + range) | 782 trades, 50 %, −₹93,955 (live 72 % = ~15–20 events, overshoot) |
| FILTERED + 16,720 filter combos (IV, PCR, OI, stocks) | 0 at 80 %+, 0 profitable in both halves |
| CONFIRMED_CLOSE, FRESH_EXTREME, FRESH_CALM, BREAK_RETEST | all negative |
| Scalping rules v3 + 4,096 variants | 0 profitable |
| 41,988-rule intraday search | equal to chance |
| Next-candle prediction (SENSEX lead/lag) | 58.5 % direction, options lose (index lags options) |
| Walk-forward 40-feature model (SENSEX, NIFTY) | 44–53 % vs chance 46–52 % |
| Midday momentum | direction 56–64 %, options lost every year |
| Premium spike buying | 1,584 variants, 0 profitable |
| Fable-style spike scalp | SENSEX −₹1.6 lakh, NIFTY −₹60k |
| Nearest-level chat pick | −₹3.06 lakh |
| ORB + retest + heavyweights | NIFTY −₹31.6k, SENSEX −₹22.1k |
| Retracement (pullback) buying | 2,520 combos, 0 profitable in both halves |
| agents-trade day-extreme break | NIFTY −₹98k, SENSEX −₹65k |
| Stock option buying | best rule failed on unseen stocks |
| Long straddles (buy volatility) | negative except 31 wide-open days |
| Condor adjustments (close / roll tested side) | −₹28k to −₹35k vs +₹75k without |
| Condor delayed / retracement entry | all below the 09:30 entry |
| CE only when green (any definition), PE only when red | win 49–51 %, all negative both halves; even look-ahead 'day closes green' 51.5 %, −₹1.56 lakh |
