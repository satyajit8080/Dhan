# CE only when the market is green? (29 Oct 2026)

`tools/test_ce_when_green.py`; 1 year, real premiums, +6/−11, net after ₹70/trade.

```

==== FIRST_TOUCH (5105 trades)
  no filter (all CE and PE)                  trades 5105  win 49.8%  net Rs   -613980  (H1  -333311 / H2  -280670)  per trade  -120
  CE only, any day                           trades 2500  win 50.7%  net Rs   -293603  (H1  -167953 / H2  -125650)  per trade  -117
  CE only when above prev close              trades 1444  win 49.5%  net Rs   -175043  (H1   -97663 / H2   -77380)  per trade  -121
  CE only when above today's open            trades 1699  win 51.3%  net Rs   -195953  (H1  -111933 / H2   -84020)  per trade  -115
  CE only when above both                    trades 1248  win 49.5%  net Rs   -151183  (H1   -87153 / H2   -64030)  per trade  -121
  CE only after a green previous day         trades 1199  win 48.6%  net Rs   -149039  (H1   -92589 / H2   -56450)  per trade  -124
  CE when green + PE when red (both refs)    trades 2586  win 49.6%  net Rs   -313218  (H1  -179778 / H2  -133440)  per trade  -121
  LOOK-AHEAD: CE when day CLOSES green       trades 1365  win 51.5%  net Rs   -156418  (H1   -90408 / H2   -66010)  per trade  -115

==== FILTERED (780 trades)
  no filter (all CE and PE)                  trades  780  win 50.0%  net Rs    -93375  (H1   -60415 / H2   -32960)  per trade  -120
  CE only, any day                           trades  361  win 51.2%  net Rs    -41790  (H1   -30500 / H2   -11290)  per trade  -116
  CE only when above prev close              trades  256  win 50.4%  net Rs    -30380  (H1   -20730 / H2    -9650)  per trade  -119
  CE only when above today's open            trades  361  win 51.2%  net Rs    -41790  (H1   -30500 / H2   -11290)  per trade  -116
  CE only when above both                    trades  256  win 50.4%  net Rs    -30380  (H1   -20730 / H2    -9650)  per trade  -119
  CE only after a green previous day         trades  170  win 45.9%  net Rs    -22780  (H1   -16750 / H2    -6030)  per trade  -134
  CE when green + PE when red (both refs)    trades  605  win 49.9%  net Rs    -72545  (H1   -45875 / H2   -26670)  per trade  -120
  LOOK-AHEAD: CE when day CLOSES green       trades  243  win 49.8%  net Rs    -29330  (H1   -21220 / H2    -8110)  per trade  -121
```

Verdict: no green/red filter lifts the win rate above ~51 %; all lose in both halves. Even knowing in advance that the day closes green (look-ahead) gives only 51.5 %: a +6 / −11 scalp depends on the next few minutes, not on the day's direction.
