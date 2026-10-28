# Strategy from "how options move": sell where decay is fastest (28 Oct 2026; `tools/backtest_theta_timing.py`)

Idea: index direction is a coin flip, but time value bleeds fastest overnight-into-the-open and on expiry afternoon
(`2026-10-28_OPTION_MOVE_ANATOMY.md`). So SELL the ATM straddle in those windows. Real expired premiums, 1 year,
4 pts slippage + ₹140 per trade, 1 lot (20). Halves split 28 Apr 2026.

| Version | Trades | Win % | Net H1 / H2 | Worst trade |
|---|---|---|---|---|
| Overnight: sell 15:20, buy back 09:20 next day | 194 | 72.7 | −₹14,792 / +₹61,524 | −1,599 pts (−₹32,000) |
| Overnight: sell 15:20, buy back 09:45 | 194 | 67.0 | −₹4,988 / +₹46,119 | −1,050 pts |
| **Expiry day: sell 14:30, buy back 15:20** | 52 | 71.2 | **+₹11,560 / +₹2,711** | −396 pts (−₹7,900) |
| Expiry day: sell 14:00, stop +25 %, buy back 15:20 | 52 | 48.1 | +₹10,888 / +₹1,073 | −115 pts |
| Expiry day: sell 13:00, stop +50 % | 52 | 67.3 | −₹5,547 / +₹29,151 | −223 pts |

Reading: the decay is real (overnight +14 to +19 pts per trade on average), but the overnight version loses the first
half and carries unlimited gap risk (one night −1,599 pts). The expiry-afternoon short is positive in both halves in
only a few of 15 variants, on 52 trades each — consistent with luck after testing 15 versions. Naked straddles also
need large margin (~₹2–3 lakh per lot) and have unlimited loss. Not a basis for real orders; at most a forward paper
test of "expiry day 14:30 short straddle (or iron fly with wings), exit 15:20". No orders.
