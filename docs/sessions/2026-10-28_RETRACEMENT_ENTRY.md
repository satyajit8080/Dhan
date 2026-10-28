# Retracement (pullback) entry — option buying and premium selling (28 Oct 2026)

Paper research only; local data; no orders.

## 1. Option buying — `tools/backtest_retrace_buying.py`

Impulse (index close moves >= X pts within 15/30 min) -> pullback of 38.2 / 50 / 61.8 % without breaking the start ->
buy the ATM option in the impulse direction at the pullback ("touch") or when the index resumes ("resume": close beyond
the previous minute's high/low within 10 min). With / without day-trend filter, entry lag 0/1, exits hold 5/10/15 min or
premium target/stop. Real expired weekly ATM premiums, 1 year. Costs SENSEX 5 pts, NIFTY 1.5 pts.

| | combinations | positive H1 | positive H2 | positive BOTH | avg win rate (gross) |
|---|---|---|---|---|---|
| SENSEX | 1,316 | 2 | 7 | **0** | 41.8 % (44.3 %) |
| NIFTY | 1,204 | 8 | 12 | **0** | 40.9 % (43.4 %) |

Best first-half SENSEX pick (impulse >= 220 pts / 15 min, 61.8 % pullback, buy at touch, hold 5 min): H1 +₹2,835,
H2 −₹6,614. Best NIFTY pick (>= 50 / 15 min, 61.8 %, resume, hold 5 min): H1 +₹2,392, H2 −₹2,798.
Verdict: same as BREAK_RETEST and ORB-retest — no retracement buying rule survives.

## 2. Premium selling — entry timing (`tools/backtest_premium_selling_dynamic.py --retrace`)

Entry one trading day before expiry; 3.4 years, 1 lot, d0.07 in high-vol weeks. Pullback = index moves >= X % from the
open then gives back half.

| entry | strangle net (worst / maxDD) | condor net (worst / maxDD) |
|---|---|---|
| **09:30 (base)** | **+₹1,24,610** (−9,991 / −9,991) | **+₹75,206** (−10,352 / −10,979) |
| 12:00 | +₹78,437 (−12,314 / −15,698) | +₹32,374 (−12,661 / −25,071) |
| 15:00 | +₹38,692 (−18,444 / −18,830) | −₹4,670 (−18,677 / −25,993) |
| pullback 0.3 %, skip if none (116 wk) | +₹61,234 (−14,114 / −19,627) | +₹25,564 (−14,471 / −25,695) |
| pullback 0.3 %, else 15:00 | +₹91,270 (−14,114 / −14,335) | +₹44,011 (−14,471 / −21,240) |
| pullback 0.5 %, skip if none (53 wk) | +₹34,262 | +₹16,580 |
| pullback 0.5 %, else 15:00 | +₹54,773 | +₹10,097 |
| straddle +10 % spike, skip if none (28 wk) | +₹26,064 (−5,210) | +₹15,862 (−5,393) |
| straddle +10 % spike, else 15:00 | +₹38,841 | −₹5,160 |

Verdict: every delayed / retracement entry earns less than the plain 09:30 entry, because selling later collects less
time decay. Keep 09:30. Wings beyond ±1,000 pts are model-priced.
