# Option buying built on the mechanism: long ATM straddle (28 Oct 2026; `tools/backtest_long_vol.py`)

A bought option wins only when the realised move beats the premium, so buy the ATM straddle (no direction bet) when it
looks cheap versus recent movement or when the open is unusually wide. Real expired premiums, 1 year, 4 pts slippage
+ ₹140 per trade, 1 lot. Entries 09:30 / 10:00 / 13:00; exits 15:20 or +20 %/+40 % target with −20 % stop.

- Buying every day: 31–35 % wins, −₹77,000 to −₹1,07,000 a year depending on entry time.
- "Cheap" filters (straddle below 0.35–0.55 × the 10-day average daily range): no version positive in both halves.
- 84 combinations: 7 positive in both halves, all "wide open" days (09:15–09:30 range > 1.5 × its 10-day average),
  e.g. buy at 10:00, +20 % / −20 %: 31 trades, 55 % wins, +₹4,033 / +₹4,288. About 1 trade every 2 weeks and ~₹8,000
  a year per lot — too few trades to separate from luck.

Conclusion: plain option buying loses; the only hint is buying the straddle on days that open unusually volatile
(big moves tend to continue that day). Small, unproven; forward paper only. No orders.
