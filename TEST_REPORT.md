# Test report — Dhan Quant Intelligence Engine v1.0.0

Environment: cloud build container (Python 3.13, no Dhan credentials, no Telegram bot, not the
target VPS). Offline: `python main.py selftest` → **57 PASS / 0 FAIL** (see `SELFTEST_REPORT.md`);
`python -m pytest -q tests` → **9 passed**. Live checks run with `python main.py verify --telegram`
on the VPS once `.env` holds credentials (writes `data/verify_report.md`).

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | DhanHQ authentication works | NOT VERIFIED (no credentials here) | `verify` → FAIL "DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set"; REST layer uses SDK 2.3.0 data mixins |
| 2 | Live WebSocket works | NOT VERIFIED (no credentials) | v2 binary decoder unit-tested (Full/Quote/OI/disconnect, concatenated frames) |
| 3 | Correct index instruments identified | PASS | live scrip master: NIFTY 13, BANKNIFTY 25, FINNIFTY 27, SENSEX 51, BANKEX 69, INDIA VIX 21 (IDX_I) |
| 4 | Option expiries discovered dynamically | PASS (master) / NOT VERIFIED (expirylist API) | expiries + per-expiry lot sizes parsed from live master; `/optionchain/expirylist` needs creds |
| 5 | Option-chain data correctly mapped | PASS (schema) | field mapping test on Dhan v2 payload shape; live check in `verify` |
| 6 | CE/PE OI changes calculated correctly | PASS | 1/5/15-min window deltas, wall deltas, price+OI buildup classes tested |
| 7 | Greeks numerically validated | PASS | Black-76 vs finite differences, 90-point grid, max err 4e-6 |
| 8 | IV numerically validated | PASS | 179-case round trip, max price err 8e-8, no failures; arbitrage bounds rejected |
| 9 | Historical snapshots saved | PASS | index/chain snapshots written in batches, re-read after restart |
| 10 | Breakout events recorded | PASS | synthetic 3σ jump → event with t-15/-10/-5/0 + post +5/+15/+30, genuine/false label |
| 11 | Statistical calculations pass tests | PASS | OLS, Pearson, Wilson, BH-FDR, ADF, OU half-life, GARCH MLE, LW shrinkage, PCA, VaR/ES, DSR/PSR, HMM, isotonic |
| 12 | Backtests include transaction costs | PASS | walk-forward OOS net = gross − cost asserted exactly (pytest) |
| 13 | Look-ahead bias checks implemented | PASS | causal features, forward labels from later rows only, embargoed splits, violation + leakage detectors |
| 14 | WebSocket reconnect works | NOT VERIFIED live | exponential-backoff reconnect loop; `verify` force-closes the socket and checks recovery |
| 15 | Database survives restart | PASS | WAL SQLite reopened, rows intact |
| 16 | Telegram alerts work | NOT VERIFIED (no bot token) | cooldown + dedupe implemented; `verify --telegram` sends a test message |
| 17 | systemd service starts | NOT VERIFIED (container is not the VPS) | unit + watchdog timer in `deploy/`; `deploy/install.sh` enables both |
| 18 | No automatic orders placed | PASS | order mixins never imported; endpoint allow-list; source scan in self-test |

Not implemented / limited by data: market breadth (needs `BREADTH_CONSTITUENTS`), Fama-French
(no reliable Indian factor feed), index volume (indices have none — futures used), calibrated
probabilities (shown only after OOS validation passes), XGBoost (enabled at ≥3000 labelled rows).
