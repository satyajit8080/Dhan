# Paper condor entry, 28 Oct 2026 (expiry 29 Oct). NO orders.

Source: user's Dhan Cloud log of `condor_paper/main.py` (MODE CONDOR_PAPER), 09:21-09:31 IST. It waited for the
09:30 candle, then logged `condor_entry`. There were 23 rate-limit retries (DH-904), all recovered. Token not logged.

SENSEX 09:30 close 72,825.05, forward 72,813.25.

| Leg | Strike | 09:30 close | Delta |
|---|---|---|---|
| Sell CE | 74,300 | 52.60 | 0.099 |
| Sell PE | 71,400 | 48.45 | 0.094 |
| Buy CE | 75,300 | 9.45 | |
| Buy PE | 70,400 | 8.45 | |

- Net credit: 83.15 pts. Break-evens: 71,316.85 / 74,383.15.
- 13 lots x 20 = 260 per leg: max profit Rs 21,619, max loss Rs 2,38,381, estimated charges Rs 3,250.
- Per lot: max profit about Rs 1,663, max loss about Rs 18,337.
- Result: run the same file again after 15:30 IST on 29 Oct (`condor_settle`).
