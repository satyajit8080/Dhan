# Low-delta premium selling with volatility, trend and adjustment rules (28 Oct 2026; `tools/backtest_premium_selling_dynamic.py`)

3.4 years (26 May 2023 – 26 Oct 2026, 167 expiry weeks), SENSEX, 1 trading day before expiry at 09:30, lot 20.
Prices: rolling ATM±10 (= ±1,000 pts) 1-min premiums with a smile fit; **strikes beyond ±1,000 (wings, some rolled
strikes) are model-extrapolated, not traded prices** (external review flag). Costs: strangle ₹150 + 4 pts, condor
₹250 + 6 pts, each adjustment leg ₹75 + 2 pts. VIX proxy: entry straddle vs the previous 10 entries (no India VIX
history in the data). Trend: index change over the 3 days before entry, > 1.5 %.

| Variant | Weeks | Win % | Net 1 lot | H1 / H2 | Worst week | Max drawdown |
|---|---|---|---|---|---|---|
| Strangle d0.10 (base) | 167 | 90.4 | +₹1,31,473 | +62,025 / +69,448 | −₹11,517 | −₹16,831 |
| Strangle, d0.07 in high-vol weeks | 167 | 91.0 | +₹1,24,610 | +55,929 / +68,680 | −₹9,991 | −₹9,991 |
| Strangle, skip low-vol weeks | 137 | 92.0 | +₹1,21,571 | +59,782 / +61,789 | −₹11,517 | −₹16,831 |
| Strangle, trend side d0.05 | 167 | 91.0 | +₹1,17,344 | +57,784 / +59,559 | −₹12,091 | −₹17,405 |
| Strangle, only side against trend | 167 | 91.0 | +₹98,591 | | −₹12,395 | −₹17,709 |
| Strangle, skip high-vol weeks | 131 | 90.1 | +₹93,467 | | −₹9,991 | −₹9,991 |
| Strangle, skip trend weeks | 127 | 89.0 | +₹87,420 | | −₹9,991 | −₹10,714 |
| Strangle, roll tested side at short strike | 167 | 75.4 | +₹14,812 | +12,016 / +2,795 | −₹9,914 | −₹24,058 |
| Strangle, close tested side at short strike | 167 | 75.4 | +₹2,254 | | −₹10,441 | −₹25,938 |
| Strangle, close / roll halfway | 167 | 50–54 | −₹21,168 / −₹14,952 | | | |
| Condor d0.10 + 1,000 wings (base) | 167 | 90.4 | +₹78,035 | +37,081 / +40,954 | −₹11,961 | −₹17,781 |
| Condor, skip low vol / skip high vol / skip trend | 137 / 131 / 127 | 89–92 | +₹75,369 / +₹58,905 / +₹51,994 | | | |
| Condor, close or roll at short strike | 167 | 75.4 | −₹34,530 / −₹27,871 | | | −₹36,000 to −₹42,000 |

Findings: (1) intraday adjustments (closing or rolling the tested side) destroy the result — the index touches a short
strike and comes back far more often than it settles beyond it, so adjusting pays for losses that would not have
happened, plus costs; (2) vol/trend filters mostly cut weeks and profit; the useful one is selling further out
(delta 0.07) in high-vol weeks: similar profit, worst week −₹9,991 and max drawdown −₹9,991 (vs −₹16,831);
(3) all base and filter variants are positive in both halves and every year. Model-priced wings mean these figures
are simulations; the forward paper condor (real prices for all legs) is the check. No orders.

Follow-up: condor with delta 0.07 in high-vol weeks: 167 weeks, 91.0 %, +₹75,206 (H1 +32,701 / H2 +42,505), worst week
−₹10,352, max drawdown −₹10,979 (base condor −₹17,781). Adding "skip low vol": 137 weeks, 92.7 %, +₹72,539, max
drawdown −₹10,352. Best risk-adjusted defined-risk version: condor d0.10, d0.07 when the entry straddle is > 1.2× its
10-week median.
