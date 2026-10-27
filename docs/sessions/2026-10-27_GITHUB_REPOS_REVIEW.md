# Review of 6 public option-chain repos and back-test of their ideas (27 Oct 2026)

Read-only clones (nothing executed): raghavs-stack/nse-oi-dashboard, 85599/Strike-and-Hedge,
darshkale/nse-options-data-pipeline, monikerprivacy-byte/dhan-options-analytics,
Mohit-Finance/Live-Option-Chain-Analysis__Nifty-BNF-Sensex, CApsUNlocked123/openmtops.

| Repo | What it does | Proven edge? | Notes |
|---|---|---|---|
| nse-oi-dashboard | localized PCR (±8 strikes, contrarian thresholds), max pain, OI score, RSI(14) 58/42 + VWAP, 0–100 scorer | No historical back-test (only same-day "EOD backtest", delta approximation) | Needs Shoonya password + TOTP secret in a file |
| Strike-and-Hedge | rule-based BULLISH/BEARISH/RANGE/AVOID from PCR, VIX, OI walls; LLM "desk" | No | Trade plans, not tested |
| nse-options-data-pipeline | NSE bhavcopy (end-of-day) + IV/Greeks + cost model | n/a | EOD only — cannot test intraday breakouts |
| dhan-options-analytics | Dhan-API analytics dashboard | No | Contains order-related client code |
| Live-Option-Chain-Analysis | live OI/PCR/IV plots | No | Visual only |
| openmtops | OI walls, max pain, PCR, Telegram signals; EXECUTES on Dhan | No | Places real orders — do not run |

## Back-tests of their ideas
- RSI(14) 58/42 agreement (1-year SENSEX, FRESH_EXTREME no Thursday): agrees with the break ~97 % of the time, so it
  filters almost nothing; H1 −₹48,750 / H2 −₹27,960 net (1 lot, slip 4, ₹70).
- OI ideas on live logs (158 FIRST_TOUCH breakouts, 10 days, real bid/ask; ATM±5 legs): PCR with/against trade,
  OI-change direction, room to the OI wall (≥ / < 200 pts), max pain direction — none positive after costs in either
  half; small sample, no consistent sign. Intraday OI history for a year is not available (bhavcopy is end-of-day).
Conclusion: nothing to add that improves the strategy after costs.
