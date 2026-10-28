# Can FILTERED reach 80 %+? Option-data, breakout and weighted-stock filters (28 Oct 2026; `tools/search_filtered_plus.py`)

Base: FILTERED crossings (day trend + fresh level + range >= 300; 763 in 1 year) and all FIRST_TOUCH crossings (4,894).
Extra yes/no filters, alone and in pairs/triples, each both ways:
- option data: high IV (ATM straddle vs 20-day median), IV rising (15 min), local PCR (ATM PE OI / CE OI) on the trade
  side, ATM OI flow (put vs call writing), expiry day, day before expiry (gamma/theta regime). Delta is ~0.5 at ATM.
  True PCR and max pain need every strike's OI history, which is not available for the year (ATM only).
- breakout: calm approach, new session extreme, crossing minute closed beyond the trigger (confirm), before 11:30,
  exactly 2 touches.
- weighted stocks (measured SENSEX influence): breadth on the trade side, 5-min weighted basket move, top-3 heavyweights.
Exits +6/−11, +4/−11, +3/−11, +6/−15 (10-min stop). Real premiums, ₹70 per trade, lot 20. 16,720 combinations;
selected on the first half, checked on the second.

**Look-ahead found and fixed.** The first run produced 7 combinations at >= 80 % in both halves, all using `confirm`
(the crossing minute's close) while entering inside that minute — information not yet known. With confirm-filtered
trades entering at the close of the crossing minute:

| | Result |
|---|---|
| Combinations >= 80 % win in the first half | **0** |
| >= 70 % win in both halves | 1 |
| Net positive in both halves (any win rate) | **0** |
| Best first-half pick (76.7 %) → second half | 55.9 %, −₹1,800 / −₹4,160 |
| FILTERED alone, +6/−11 | 44.0 % / 57.7 %, −₹58,775 / −₹32,390 |

Conclusion: no combination of option Greeks/IV/PCR/OI, breakout quality and weighted-stock filters lifts FILTERED to
80 % honestly, and none is profitable in both halves. Small targets raise the win rate but not the profit.
Full output: `docs/exports/filtered_plus_search_output.txt`. No orders.
