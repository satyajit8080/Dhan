# github.com/MrSpideyNihal/agents-trade — review and backtest (28 Oct 2026)

Paper research only; no orders; nothing from that repo was run.

**What it is:** an Angel One SmartAPI tool for NIFTY/BANKNIFTY option buying (`signal_engine.py` signal, `server.py` MCP
with a guarded order path, `webapp.py` with an AUTO-TRADE mode, `SKILL.md` discipline rules). Login is PIN + TOTP
secret from `.env` (`pyotp`), and live orders are possible when `LIVE_TRADING=1`. Its only published evidence is
"the planned trades were green" — no backtest.

**Signal:** a new day high (low), index >= 0.4 % above (below) the previous close, rising (falling) over ~1.5 min -> buy
the ATM CE (PE); exit +45 % / −25 % premium (medium), +30/−20 (low), +80/−35 (high); max 2 trades a day.

**Backtest** (`tools/backtest_agents_trade.py`, 1 year, real expired weekly ATM premiums, costs NIFTY 1.5 / SENSEX 5 pts):

| | default (0.4 %, max 2/day, medium) | variants tested | profitable in both halves |
|---|---|---|---|
| NIFTY | 156 trades, 24 % win, **−₹98,135/lot** | 36 | **0** (best −₹47,084) |
| SENSEX | 309 trades, 38 % win, **−₹64,864/lot** | 36 | **0** (best −₹36,903) |

Break-even win rate for +45/−25 is ~36 % before costs; NIFTY wins 24–33 %, SENSEX 33–42 %, all below what is needed
once costs are added. Verdict: same family as FIRST_TOUCH / day-extreme breakouts — loses. The discipline rules are
sensible (they limit losses) but do not create an edge.
