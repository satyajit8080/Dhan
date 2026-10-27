# Paper iron condor on Dhan Cloud (`cloud/dist/condor_paper/main.py`)

PAPER ONLY. The file places no orders. It contains no order functions and uses only the read-only allow-list
(expiry list, option chain, 1-min candles). User decision 28 Oct 2026: "Paper only".

## What it does
- **Day before the weekly expiry** (e.g. Wednesday for a Thursday expiry): waits for the 09:30 IST candle, then logs
  `condor_entry`:
  - the call and put above / below SENSEX whose delta is about 0.10 (Black-76 on the 09:30 candle closes, forward
    from put-call parity);
  - the protection legs 1,000 points further out;
  - prices, net credit, break-evens, max profit and max loss for 13 lots x 20 (the Rs 20 lakh plan).
- **Expiry day**: rebuilds the same entry from the previous day's 09:30 candles (no file needs to survive between
  runs) and logs `condor_settle`:
  - result from the average index close 15:00-15:29;
  - gross and estimated net (6 pts slippage, Rs 250 per lot).
  - Before 15:30 the result is marked PROVISIONAL.
- **Other days**: logs `condor_plan` with the next entry day and does nothing else.

## How to run (you schedule it; nothing here changes your Dhan Cloud schedule)
1. New Dhan Cloud project, paste `cloud/dist/condor_paper/main.py` as `main.py`.
2. Run it on the day before expiry any time from 09:00 (it waits up to 90 minutes for 09:30).
3. Run it again on expiry day after 15:30 IST.
4. Upload both logs; `condor_entry` / `condor_settle` lines hold the result.

## Settings (CONFIG block)
`CONDOR_LOTS = 13`, `CONDOR_LOT_SIZE = 20` (re-verify; reported to become 25 for January 2027 expiries),
`CONDOR_DELTA = 0.10`, `CONDOR_WING_PTS = 1000`, `HOLIDAYS = []` (add exchange holidays so the entry day moves back).

## Limits
- The delta here can differ slightly from the Dhan app's delta column.
- Entry and exit prices are 1-min candle closes, not live bid/ask; real fills will differ.
- Settlement is an approximation of the exchange's settlement price.
- Back-test basis: `tools/backtest_strangle_multiyear.py`, condor delta 0.10 with 1,000-point wings, sold 1 day
  before expiry: 90.4% weekly win, about Rs 26,000 per lot per year over May 2023 - Oct 2026, worst week
  -Rs 11,901 per lot.
