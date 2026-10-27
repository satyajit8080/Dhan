# Iron fly and short strangle on REAL wing prices (27 Oct 2026)

Paper research only. No orders, no deployment, no schedule change. Data: `history_wings` run of 27 Oct 2026
(1,460 windows, 371,723 bars, 0 refused days, orders NONE, secret check PASS) plus the earlier ATM history.
Rolling ATM+10 / ATM-10 sit exactly 1,000 pts from the ATM strike (9,060 of 9,060 checked minutes).

Method (`tools/backtest_wings.py`): a held strike is priced each minute from three real smile points (ATM straddle,
+1,000 call, -1,000 put; Bachelier sigma*sqrt(T), interpolated by distance from the forward). Entry credits are real
premiums. Check: when the rolling wing strike switches, the predicted price of the new strike has a median absolute
error of 2.1%. Expiry legs settle at the average spot of the last 30 minutes. Costs per trade: strangle Rs 150 +
4 pts, iron fly Rs 250 + 8 pts (+4 pts on stops). 1 lot = 20. Full output: `docs/exports/wings_backtest_output.txt`.

## Results, 1 lot, 27 Oct 2025 - 26 Oct 2026

| Strategy | Trades | Win | Net | H1 / H2 | Worst trade | Max drawdown |
|---|---|---|---|---|---|---|
| Research-report iron fly, intraday Mon/Tue/Wed/Fri | 199 | 43.7% | Rs -1,20,355 | -57,625 / -62,730 | | -1,20,355 |
| Research-report iron fly, intraday expiry day | 53 | 52.8% | Rs -10,132 | -11,761 / +1,629 | | -17,078 |
| Short strangle +-1,000, sold 2 days before expiry, held to expiry, no stop | 52 | 82.7% | **Rs +1,15,423** | +66,672 / +48,752 | Rs -17,580 | -23,765 |
| Same, take profit at 50% | 52 | 90.4% | Rs +84,876 | +53,428 / +31,449 | Rs -17,580 | -23,765 |
| Short strangle sold the day after expiry, held to expiry | 51 | 70.6% | Rs +98,741 | +59,745 / +38,996 | Rs -35,001 | -46,814 |
| Short strangle sold 1 day before expiry | 52 | 92.3% | Rs +38,760 | +2,612 / +36,148 | Rs -21,612 | -38,181 |
| Iron fly (ATM, wings +-1,000) sold on expiry morning, held to expiry | 52 | 69.2% | Rs +22,230 | -13,713 / +35,943 | | -31,946 |

Stops on the strangle (at 50% or 100% above the credit) cut the result in every entry timing.

Best row (2 days before expiry): mean Rs 2,220 per trade, t = 2.74, bootstrap chance of a losing year 0.4%.
Losing months: Apr 2026 (-12,819), Jul 2026 (-7,505), Oct 2026 so far (-4,169).

## Cautions

- About 48 variants were tried; the best one is expected to look better than it will trade.
- One year only, and it included a sharp fall (Mar 2026). A naked strangle has unlimited risk; a gap bigger than
  this year's could exceed every loss above. A hedged version (iron condor) needs strikes beyond +-1,000, which
  were not downloaded.
- Far strikes can print stale 1-minute prices; slippage at 10 lots will likely be larger than 4 pts.
- Margin for a naked SENSEX strangle was not checked here.
- The research-report intraday iron fly loses on real prices; the earlier model estimate for Thursday (+Rs 11,665)
  does not hold (real: Rs -9,857).
- Next step is forward paper tracking only. No live trading.
