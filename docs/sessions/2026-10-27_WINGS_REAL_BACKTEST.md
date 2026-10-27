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

## Iron condor with a 2,000-pt hedge (sell +-1,000, buy +-2,000)

The +-2,000 strikes were not downloaded (the rolling series used reaches ATM+-10), so their prices are
extrapolated from the real smile: "flat" keeps the +-1,000 vol, "linear" keeps the ATM-to-wing slope (dearer hedges
on the put side). Costs Rs 250 + 6 pts per trade. Sold 2 days before expiry, held to expiry, 1 lot:

| Version | Win | Net | H1 / H2 | Worst trade | Max drawdown | Hedge cost | t |
|---|---|---|---|---|---|---|---|
| Naked strangle (for comparison) | 82.7% | Rs +1,15,423 | +66,672 / +48,752 | -872 pts | Rs -23,765 | 0 | 2.74 |
| Condor, flat extrapolation | 80.8% | Rs +52,144 | +18,777 / +33,367 | -616 pts | Rs -26,414 | 69 pts | 1.48 |
| Condor, linear extrapolation | 80.8% | Rs +43,726 | +13,113 / +30,613 | -622 pts | Rs -28,984 | 77 pts | 1.24 |

- The hedges cost about 27-30% of the credit, so profit roughly halves.
- This year no move went past +-2,000 by expiry, so the hedge never paid; it only caps a future gap (max loss about
  1,000 pts minus the credit per lot).
- A tighter condor inside real data (sell +-500, buy +-1,000) is flat: Rs -998 for the year.

## Robustness of the short strangle (tools/backtest_strangle_robustness.py)

Output: `docs/exports/strangle_robustness_output.txt`. 1 lot, Rs 150 + 4 pts per trade, held to expiry.

- Every variant tried (strike distance 600-1,500, delta 0.05-0.20, entry 09:30-15:00, entry 1-3 days before expiry)
  is positive in both halves of the year.
- Further strikes: higher win, lower profit, smaller worst trade. +-1,000: 82.7%, Rs +1,15,423, worst -17,580.
  Delta 0.10 (adaptive): 92.3%, Rs +81,265, worst -7,921, max drawdown -7,921.
- Entry at 09:30 was best; later entries roughly halve the result.
- Entering 3 trading days before expiry: Rs +2,01,485 (H1 +1,00,387 / H2 +1,01,098) but worst trade -32,708.
- Walk-forward: the best variant on H1 (delta 0.20) made Rs +36,989 on H2 (positive, smaller).
- Strikes beyond +-1,000 (delta 0.10 / 0.05, +-1,200, +-1,500) use extrapolated prices.
- The year contained no gap large enough to test the tail risk of a naked strangle.

## Older data, file B (Oct 2021 - Oct 2023): only May - Oct 2023 exists

Run of 27 Oct 2026: 4,376 windows, 195,831 bars, 4 refused days (rate limit; none needed), orders NONE. Dhan returned
no SENSEX weekly option data before 26 May 2023 (weekly SENSEX derivatives relaunched in 2023). Expiries then were on
Friday; expiry days are now found from the data (the ATM straddle more than doubles overnight when the weekly rolls),
which matches the Thursday rule week for week on Oct 2025 - Oct 2026.

15 tradable weeks, 1 lot (`docs/exports/strangle_robustness_2023.txt`): a low-volatility period, so +-1,000 earned
only ~55 pts of credit. Strangle +-1,000 sold 2 days before expiry: 86.7% win, Rs -431. Delta 0.10: 86.7%,
Rs +6,909, worst -4,574. Sold 1 day before: 100%, Rs +5,595. A fixed 1,000-pt distance depends on the volatility
regime; delta-based strikes adapt. The 2023-2025 file (A) holds the June 2024 election week.

## All downloaded years: 26 May 2023 - 26 Oct 2026 (tools/backtest_strangle_multiyear.py)

File A (Oct 2023 - Oct 2025) arrived in three pieces (over the 30 MB upload limit), joined before decoding: 4,356
windows, 1,054,083 bars, 24 days refused by the rate limit (about 12 trading days lost), orders NONE, secret check PASS.
823 trading days, 168 expiry weeks found from the data (Friday, Tuesday and Thursday eras). Lot 20 used for all years.
Output: `docs/exports/strangle_multiyear_output.txt`.

| Strategy (held to expiry) | Weeks | Win | Net 1 lot | Worst week | Max drawdown | t | By year (2023 / 2024 / 2025 / 2026) |
|---|---|---|---|---|---|---|---|
| Strangle +-1,000, 2 days before | 165 | 83.6% | Rs +2,17,703 | -40,258 | -60,232 | 2.35 | +13,487 / +37,497 / +70,612 / +96,106 |
| Strangle delta 0.10, 2 days before | 165 | 89.7% | Rs +1,24,915 | -24,575 | -43,058 | 2.37 | +13,503 / -1,664 / +45,377 / +67,700 |
| Condor +-1,000 / +-2,000, 2 days before | 165 | 81.2% | Rs +1,18,722 | -17,821 | -42,300 | 1.92 | +7,811 / +38,673 / +43,790 / +28,449 |
| Strangle +-1,000, 1 day before | 167 | 90.4% | Rs +1,63,140 | -21,612 | -38,181 | 3.55 | +13,753 / +50,882 / +66,806 / +31,699 |
| Strangle delta 0.20, 1 day before | 167 | 79.6% | Rs +2,02,456 | -20,895 | -35,784 | 3.64 | +20,786 / +65,742 / +84,301 / +31,626 |
| Strangle delta 0.10, 1 day before | 167 | 91.0% | Rs +1,39,043 | -11,477 | -16,751 | 6.04 | +13,107 / +46,860 / +49,702 / +29,375 |
| Condor delta 0.10 + 1,000 wings, 1 day before | 167 | 90.4% | Rs +88,419 | -11,901 | -17,661 | 3.94 | +8,622 / +31,770 / +31,114 / +16,913 |

- Every strangle/condor variant held to expiry is positive over the full period; most are positive every year.
- Selling 1 day before expiry at delta 0.10 is the steadiest: 91% win, worst week Rs -11,477, positive every year.
- Skipping election/budget weeks made results slightly worse (those weeks paid rich premiums).
- Worst week overall: sold Wed 5 Jun 2024 (day after the election crash, credit 1,082 pts), SENSEX rallied
  ~4,000 pts by Friday: Rs -40,258 (strangle +-1,000).
- Holding THROUGH the election day (sold Mon 3 Jun 2024, expiry Fri 7 Jun): the +-1,000 strangle was down about
  Rs 81,000 per lot at the low on 4 Jun, then finished +Rs 32,013 as the index recovered. Without wings that
  drawdown would likely have meant a margin call or a forced exit. The +-1,000 / +-2,000 condor was down at most about
  Rs 6,800 and finished +Rs 12,864.
- Prices beyond +-1,000 (delta 0.10 strikes in calm weeks, all +-2,000 wings) are extrapolated.
