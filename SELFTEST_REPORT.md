# DHAN QUANT ENGINE - OFFLINE SELF-TEST

Generated 2026-11-07 20:20:54 IST

PASS 57 | FAIL 0 | SKIP 0 | N/A 0

| # | Item | Status | Detail |
|---|---|---|---|
| 1 | Black-Scholes reference value (Hull: 10.4506) | PASS | 10.4506 |
| 2 | Put-call parity C-P = S - K e^-rT | PASS | 4.877058 |
| 3 | Black-76(F=S e^rT) == Black-Scholes | PASS |  |
| 4 | Greeks vs finite differences (delta, gamma, vega, theta) on 90-point grid | PASS | max abs err 3.95e-06 |
| 5 | Implied-vol solver round-trip (price error) | PASS | 179 cases, max price err 8.13e-08, failures 0 |
| 6 | IV solver rejects arbitrage-violating price | PASS |  |
| 7 | Put-call parity forward recovery | PASS | 101.0 |
| 8 | Max pain (known example) | PASS |  |
| 9 | Gamma P&L Taylor approximation | PASS | 3.5 |
| 10 | Bayes posterior | PASS |  |
| 11 | Wilson interval 8/10 | PASS | (0.8, 0.4901624691221323, 0.9433178490622331) |
| 12 | Benjamini-Hochberg FDR | PASS | [True, True, True, True, False] |
| 13 | OLS coefficients / R^2 | PASS | [ 1.497  1.997 -0.495], R2 0.9973 |
| 14 | Pearson correlation + p-value | PASS | r=0.968 |
| 15 | ADF: random walk non-stationary, AR(0.5) stationary | PASS | RW stat -1.60 p 0.49; AR stat -8.99 p 0.000 |
| 16 | OU half-life of AR(0.5) ~ ln2/ln2 = 1 | PASS | 0.94 |
| 17 | GARCH(1,1) MLE recovers alpha/beta (0.10/0.85) | PASS | alpha 0.103 beta 0.836 |
| 18 | Ledoit-Wolf shrinkage + PCA eigen-decomposition | PASS | delta 0.022, PC1 0.64, cond 9.9 |
| 19 | Min-variance weights (Lagrange) sum to 1 | PASS |  |
| 20 | Mean-variance frontier hits target | PASS |  |
| 21 | Jensen gap (convexity) positive | PASS |  |
| 22 | GBM Monte Carlo E[S_T] = S0 e^muT (Ito drift) | PASS | 105.02 |
| 23 | Kelly fraction (p=.6, 1:1) = 0.2 | PASS |  |
| 24 | Historical VaR95 / ES95 vs normal | PASS | VaR 0.0153 ES 0.0196 |
| 25 | Max drawdown | PASS | (-2.0, 3, 4) |
| 26 | Deflated Sharpe < Probabilistic Sharpe under multiple trials | PASS | DSR 0.477 PSR 0.999 |
| 27 | Precision / recall / F1 / confusion matrix | PASS | {'tp': 2, 'fp': 1, 'fn': 1, 'tn': 1, 'precision': 0.6666666666666666, 'recall': 0.6666666666666666, 'f1': 0.6666666666666666, 'accuracy': 0.6, 'base_rate': 0.6} |
| 28 | Square-root impact | PASS |  |
| 29 | Order-book imbalance / microprice | PASS |  |
| 30 | Walk-the-book fill price | PASS |  |
| 31 | VWAP / TWAP | PASS |  |
| 32 | Buildup classification uses price AND OI | PASS |  |
| 33 | Adaptive robust z-score (median/MAD) | PASS | z=6.03 |
| 34 | WebSocket binary decoding (Full+Quote+OI, concatenated) | PASS |  |
| 35 | WebSocket server-disconnect code decoding | PASS |  |
| 36 | Order endpoints blocked (read-only allow-list) | PASS |  |
| 37 | No order-placement calls anywhere in main.py | PASS |  |
| 38 | 3m / 30m resampling from 1m (Dhan serves 1/5/15/25/60 only) | PASS |  |
| 39 | Option-chain JSON mapping (strike/side/OI/prevOI/bid/ask/IV/greeks/security_id) | PASS | 102 legs |
| 40 | Per-strike CE/PE OI change computed over 5/15 min windows | PASS | min res CE dOI15 -736,302 |
| 41 | CE OI drop before jump flagged by adaptive threshold | PASS |  |
| 42 | Breakout event recorded with pre-event snapshots | PASS | kind UPSIDE_BREAKOUT sigma 3.0 lead 1.0 |
| 43 | Event analysis answers research questions (first strike, writers, IV, gamma, futures, lead) | PASS | first None, CE exit True |
| 44 | Event uses only pre-event data (no look-ahead) | PASS |  |
| 45 | Historical snapshots saved; DB survives restart (reopen + read) | PASS | 240 snapshots, events 1 |
| 46 | Post-event +5/+15/+30 capture and genuine/false classification | PASS |  |
| 47 | Forward labels use strictly later rows only (look-ahead check) | PASS |  |
| 48 | Walk-forward splits: no overlap, test after train, embargo | PASS |  |
| 49 | Look-ahead violation detector catches injected future feature | PASS |  |
| 50 | Leakage scan flags a label-copy feature | PASS |  |
| 51 | Pattern study: counts, Wilson CI, BH-FDR, MFE/MAE, precision/recall, net after cost | PASS | 90 hypotheses |
| 52 | Walk-forward backtest runs with transaction costs + embargo | PASS | status TOO FEW OOS TRADES, trades 0, cost 6.0bps |
| 53 | Signals recorded and labelled after horizon | PASS | 46 signals, 46 labelled |
| 54 | Gaussian HMM regime detection separates low/high-vol states | PASS |  |
| 55 | Isotonic calibration is monotone | PASS |  |
| 56 | Expiry timestamp 15:30 IST | PASS |  |
| 57 | Calendar: weekends & holidays respected | PASS |  |
