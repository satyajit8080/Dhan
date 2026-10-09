# Bull50 — SENSEX options scanner

Read-only Dhan market data for SENSEX options scalping, with put-call-parity
forward recovery and an internal Black-76 pricing core. Never places orders.

**Read [RULES.md](RULES.md) first.**

## Layout

| Path | What |
|---|---|
| `plugin/` | Installable Claude plugin `bull50-dhan` v1.7.0: MCP server bundle, `/dhan-token`, `/sensex-snapshot`, `sensex-scalp` skill |
| `server/` | TypeScript source of the MCP server + 204 tests |
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

## MCP tools

`set_dhan_token`, `dhan_token_status`, `get_expiries`, `get_instrument`,
`get_option_chain`, `get_quote`, `get_futures_quote`, `get_market_snapshot`,
`get_candles`, `get_analysis`, `compute_levels`.

## Disclaimer

Analytics only, not investment advice. Projected premiums are model estimates.
