# Can FRESH_EXTREME reach 70 %? (27 Oct 2026; `tools/backtest_win_rate_scan.py`)

1-year SENSEX 1-min data, FRESH_EXTREME, Thursdays (expiry) skipped, +6/−11, index-only (delta 0.5).
Base without Thursdays: H1 57.3 % (445), H2 68.9 % (415). ~100 feature splits scanned (data-mining risk).

Best robust candidate — calm approach: the index moved at most N pts (in the trade direction) in the 3 minutes
before the breakout (`mom3 = close[j-1] − close[j-4]`, signed):

| Rule | H1 n | H1 win | H1 pts (slip 4) | H2 n | H2 win | H2 pts (slip 4) |
|---|---|---|---|---|---|---|
| mom3 ≤ 0 | 49 | 69.4 % | +39 (−21) | 33 | 69.7 % | +28 (−12) |
| mom3 ≤ 8 | 90 | 67.8 % | +47 (−69) | 80 | 73.8 % | +123 (+39) |
| **mom3 ≤ 15** | 119 | **69.7 %** | +102 (−42) | 115 | **70.4 %** | +112 (−24) |

≈ 70 % in both halves and positive before slippage; slightly negative with 4-pt stop slippage (break-even then ≈ 71 %).
Plausible reason: breakouts after a sudden 3-minute spike are exhaustion moves; a calm approach is more often real.
Status: candidate only; needs a forward paper test. RULES.md unchanged.

## Final cost check (27 Oct): FRESH_CALM ± close-back exit, both halves, ₹ net (1 lot ₹70/trade; 5 lots ₹155/trade)
| Version | H1 pts | H2 pts | 1 lot net H1 / H2 | 5 lots net H1 / H2 |
|---|---|---|---|---|
| FRESH_CALM, no slippage | +102 | +112 | −₹6,290 / −₹5,810 | −₹8,245 / −₹6,625 |
| FRESH_CALM, 4-pt slippage | −42 | −24 | −₹9,170 / −₹8,530 | −₹22,645 / −₹20,225 |
| + close-back exit, 4-pt slippage | +77 | −21 | −₹6,797 / −₹8,462 | −₹10,780 / −₹19,883 |
Negative after charges in both halves in every case. ChatGPT and Grok reviews (27 Oct) reached the same decision.
