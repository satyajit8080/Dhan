# Trying VarunS2002/Python-NSE-Option-Chain-Analyzer on SENSEX (28 Oct 2026)

The tool is a Tkinter desktop app that scrapes the **NSE** website (NIFTY, BANKNIFTY, stocks). SENSEX is on BSE, so it
cannot read SENSEX; its formulas were re-applied to the Dhan chain instead. Its rules (from `set_values`), for a strike
the user picks:
- **Open Interest:** call_sum = day OI change of calls at the strike and next 2 up; put_sum = same for puts.
  Bearish if call_sum >= put_sum, else Bullish.
- **PCR:** total put OI ÷ total call OI; >= 1 Bullish. **Boundaries:** strikes with max call OI (upper) / max put OI (lower).
- **Call/Put exits, Call/Put ITM:** sign and ratio tests on OI change at strike+2, strike, strike+4 and strike−2.

Live reading 10:3x IST (spot 72,653, strike 72,700): call OI change +38.7 L vs put +14.5 L → **Bearish**; PCR 0.69 →
Bearish; upper boundary 74,000, lower 70,000; call exits No, put exits No, call ITM No, put ITM No.

Check on our logged scans (`tools/test_oi_change_signal.py`; 9 days 12–27 Oct, ATM strike, ATM±2 legs, ~1-min scans):

| Horizon | Readings | Signal right | Avg move in the signal direction |
|---|---|---|---|
| 15 min | 2,160 | 53.1 % | −0.05 pts |
| 30 min | 2,026 | 52.4 % | +0.35 pts |
| 60 min | 1,773 | 53.7 % | +8.1 pts |

The rule read "Bearish" most of the time (call writing above ATM is the normal state), and the per-day results swing
from +268 to −65 pts: about a coin flip, and only 9 days of correlated readings. No edge shown; no orders.
