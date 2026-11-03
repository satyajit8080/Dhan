# Bull50 — SENSEX options scanner

Read-only Dhan market data for SENSEX options scalping, with put-call-parity
forward recovery and an internal Black-76 pricing core. Never places orders.

**Read [RULES.md](RULES.md) first.**

## Layout

| Path | What |
|---|---|
| `plugin/` | Installable Claude plugin `bull50-dhan` v1.7.0: MCP server bundle, `/dhan-token`, `/sensex-snapshot`, `sensex-scalp` skill |
| `server/` | TypeScript source of the MCP server + 204 tests |
| `collector/` | Python historical stock-options collector (Dhan rolling options → 09:30/15:20 CSV, ZIP, validation report). See [collector/README.md](collector/README.md) |
| `engine/` | Python `refresh_table.py` (option breakout premiums), `commodity_engine.py` (MCX), sample data |
| `docs/` | Signal log |
| `.claude-plugin/marketplace.json` | Lets Claude install the plugin straight from this repo |

## Install the plugin

From this repo as a marketplace, or copy `plugin/` and point Claude at it.
Needs Node 20+. Then each morning: `/dhan-token <token> <client_id>`.

## Develop the server

```bash
cd server
npm ci
npm test          # 204 tests, offline
npm run build     # tsc -> dist/
npm run bundle    # rebuild plugin/server/mcpServer.cjs
```

Copy `server/.env.example` to `server/.env` for local config. Never commit `.env`.

## Run the refresh engine

```bash
cd engine
python3 run_refresh.py   # self-check on a saved 21 Sep snapshot
```

## Pull stock-option history for backtests

Full collector (60 stocks, 5 years, 1-min, resume, reports): `python -m collector.collect run`, see [collector/README.md](collector/README.md). Quick single-file version:

```bash
cd engine
export DHAN_ACCESS_TOKEN=...  DHAN_CLIENT_ID=...
python3 dhan_history_pull.py --from 2025-11-01 --to 2026-11-01 --zip   # NIFTY 50, ATM±3, 09:30 + 15:20
```

Writes `date,time,symbol,expiry,strike,ce_oi,pe_oi,ce_ltp,pe_ltp,spot` plus
IV, volume and lot size. `--all-bars --interval 1` for 1-min bars. Dhan caps
stock options at ATM±3. Raw responses are cached in `.dhan_cache/`, so an
interrupted run resumes where it stopped.

## MCP tools

`set_dhan_token`, `dhan_token_status`, `get_expiries`, `get_instrument`,
`get_option_chain`, `get_quote`, `get_futures_quote`, `get_market_snapshot`,
`get_candles`, `get_analysis`, `compute_levels`.

## Disclaimer

Analytics only, not investment advice. Projected premiums are model estimates.
