# Back-test of the SENSEX-chat pick (73300 PE, breakout below 73,245) — 28 Oct 2026

Rule as the chat described it (`tools/backtest_nearest_level.py`): 5-min levels with >= 2 touches, trigger = level ± 5;
trade only the NEARER side; PE needs < 50 % of the 30 stocks above their previous close and SENSEX below its previous
close (CE the reverse); ATM option bought at the crossing; real expired premiums, 247 days, ₹70 per trade, lot 20.

| Version (no slippage) | Trades | Win % | Net 1 lot H1 / H2 | Total |
|---|---|---|---|---|
| Chat rule, +6 / −11, 10 min | 2,573 | 50.3 | −₹1,71,458 / −₹1,34,250 | −₹3,05,708 |
| Chat rule, +10 / −10, 15 min | 2,573 | 42.3 | −₹1,37,898 / −₹1,20,975 | −₹2,58,872 |
| Chat rule, +20 / −10, 30 min | 2,573 | 31.3 | −₹1,11,042 / −₹1,00,470 | −₹2,11,512 |
| Strong levels only (>= 4 touches), +6 / −11 | 1,169 | 50.4 | −₹79,033 / −₹59,440 | −₹1,38,473 |
| PE only, +6 / −11 | 1,378 | 51.0 | −₹88,355 / −₹72,360 | −₹1,60,715 |
| All crossings (no filters), +6 / −11 | 5,105 | 49.9 | −₹3,33,311 / −₹2,80,670 | −₹6,13,980 |

Win rate stays at ~50 % (break-even 64.7 % for +6/−11, 50 % for +10/−10, 33.3 % for +20/−10) with every filter
combination; all versions lose in both halves, more with 4-pt stop slippage. The single winning trade on 28 Oct is one
sample. No edge; no change. No orders.
