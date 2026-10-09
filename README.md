# Bull50 — SENSEX options scanner

Read-only Dhan market data for SENSEX options scalping, with put-call-parity
forward recovery and an internal Black-76 pricing core. Never places orders.

**Read [RULES.md](RULES.md) first.**

## Layout

| Path | What |
|---|---|
| `plugin/` | Installable Claude plugin `bull50-dhan` v1.7.0: MCP server bundle, `/dhan-token`, `/sensex-snapshot`, `sensex-scalp` skill |
| `server/` | TypeScript source of the MCP server + 282 tests |
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
npm test          # 282 tests, offline
npm run build     # tsc -> dist/
npm run bundle    # rebuild plugin/server/mcpServer.cjs
```

Copy `server/.env.example` to `server/.env` for local config and export the
variables (the server does not load `.env` itself). Never commit `.env`.

## Run the refresh engine

```bash
cd engine
python3 run_refresh.py               # self-check on a saved 21 Sep snapshot
python3 -m unittest -v test_refresh_table   # regression tests, stdlib only
```

The plugin manifest (`plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json`)
and `.claude-plugin/marketplace.json` were reconstructed on 9 Oct 2026 after
the original dot-files were lost in a web upload. Verify them before relying
on the marketplace install.

## MCP tools

`set_dhan_token`, `dhan_token_status`, `get_expiries`, `get_instrument`,
`get_option_chain`, `get_quote`, `get_futures_quote`, `get_market_snapshot`,
`get_candles`, `get_analysis`, `compute_levels`.

## Disclaimer

Analytics only, not investment advice. Projected premiums are model estimates.

## Audit

Phase-1 audit, bug fixes and the Dhan Cloud automation assessment:
[docs/PHASE1_AUDIT.md](docs/PHASE1_AUDIT.md).
Phase-2 verification of Dhan Cloud, token lifecycle, instruments and open
decisions: [docs/PHASE2_VERIFICATION.md](docs/PHASE2_VERIFICATION.md).
Phase-3 readiness: [docs/PHASE3_READINESS.md](docs/PHASE3_READINESS.md), the manual
Dhan Cloud checklist [docs/PHASE3_CLOUD_CHECKLIST.md](docs/PHASE3_CLOUD_CHECKLIST.md)
and open strategy decisions [docs/PHASE3_STRATEGY_GAPS.md](docs/PHASE3_STRATEGY_GAPS.md).
Phase-4 Python port of the calculation layer (`engine/sensex/`, TypeScript parity
verified): [docs/PHASE4_PORT_PLAN.md](docs/PHASE4_PORT_PLAN.md),
[docs/PHASE4_PARITY_RESULTS.md](docs/PHASE4_PARITY_RESULTS.md).
Run `cd engine && python3 tests/parity_report.py`; regenerate fixtures with
`cd server && npx tsx scripts/export-parity-fixtures.ts`.
Phase-5 local read-only scanner (`engine/scan_local.py`; `--mock` runs offline):
[docs/PHASE5_DATA_CLIENT_DESIGN.md](docs/PHASE5_DATA_CLIENT_DESIGN.md),
[docs/PHASE5_DATA_CLIENT_RESULTS.md](docs/PHASE5_DATA_CLIENT_RESULTS.md).
`tools/dhan_cloud_probe.py` and `tools/dhan_cloud_probe_extended.py` are read-only,
credential-free runtime probes (`cd tools && python3 -m unittest test_dhan_cloud_probe`).
