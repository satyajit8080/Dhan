# Hedged iron fly: provisional test (27 Oct 2026)

Paper research only. No orders, no Cloud deployment, no schedule change.

## What was tested

- Sell the ATM call and put; buy a call and a put about 1,000 to 1,100 pts away (the wings).
- Rules from the research report: enter 09:30, exit 15:00, stop at credit +25%, take profit at 30% of credit.
- Costs: Rs 250 plus 8 pts slippage per 4-leg trade, lot 20.
- One year of nearest-weekly SENSEX options (247 days).

The ATM legs use real expired-option prices. The wings are **model prices**: flat volatility, no skew. Dhan's history
for the wing strikes is not downloaded yet, so treat these numbers as approximate.

## Results (tools/model_iron_fly.py)

| Days | Wing | Trades | Win | Points | Net 1 lot | First half / second half |
|---|---|---|---|---|---|---|
| Mon/Tue/Wed/Fri (report's plan) | 1,000 | 198 | 55.1% | +7 | Rs -49,356 | -23,608 / -25,748 |
| Mon/Tue/Wed/Fri (report's plan) | 1,100 | 198 | 56.6% | +2 | Rs -49,460 | -21,920 / -27,540 |
| Thursday (expiry day) | 1,000 | 48 | 66.7% | +1,183 | Rs +11,665 | +5,698 / +5,967 |
| Thursday (expiry day) | 1,100 | 48 | 68.8% | +1,497 | Rs +17,935 | +11,072 / +6,862 |
| All days | 1,100 | 246 | 58.9% | +1,499 | Rs -31,526 | -10,848 / -20,678 |

## Reading

- The report's Mon/Tue/Wed/Fri iron fly earns about zero points; costs of about Rs 250 per trade make it lose.
- Thursday is positive in both halves, but it rests on only 48 trades, model-priced wings and a pattern found after
  looking at the data. It is not evidence of an edge.
- Next step: download the real wing prices with `cloud/dist/history_wings/main.py` (rolling ATM-10 and ATM+10,
  nearest weekly, 1-day windows, read-only) and re-run on real premiums. Any survivor still needs forward paper
  testing.
