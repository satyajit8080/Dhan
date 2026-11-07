# Dhan Quant Intelligence Engine — deployment

All engine logic is in `main.py`. Read-only: no order endpoint is imported or callable.

## 1. Install (Ubuntu 22.04 / 24.04, Python 3.11+)

```bash
git clone https://github.com/satyajit8080/Dhan.git && cd Dhan
```

```bash
sudo bash deploy/install.sh
```

```bash
sudo nano /opt/dhan-quant/.env
```

```bash
sudo systemctl restart dhan-quant-engine
```

The installer creates user `dhan`, `/opt/dhan-quant`, a venv, initialises the DB, runs the
offline self-test, enables the service (starts on boot) and a health-check timer (every 2 min).

## 2. Daily token

The Dhan access token expires daily. Either update `DHAN_ACCESS_TOKEN` in `.env` and restart, or
set `DHAN_TOKEN_FILE=/opt/dhan-quant/data/token` and overwrite that file each morning — it is
re-read on every call, so no restart is needed.

## 3. Operate

| Task | Command |
|---|---|
| Live dashboard (attaches to running service) | `sudo -u dhan /opt/dhan-quant/.venv/bin/python /opt/dhan-quant/main.py watch` |
| Live verification (auth, instruments, expiries, chain, WS, reconnect) | `sudo -u dhan /opt/dhan-quant/.venv/bin/python /opt/dhan-quant/main.py verify --telegram` |
| Offline self-test | `/opt/dhan-quant/.venv/bin/python /opt/dhan-quant/main.py selftest` |
| Run research now | `... main.py research` |
| Validation report | `... main.py report` |
| Backfill candles | `... main.py backfill --days 60` |
| Health | `curl -s 127.0.0.1:8787/health` |
| Logs | `journalctl -u dhan-quant-engine -f` and `/opt/dhan-quant/logs/engine.jsonl` |

## 4. Behaviour

* **Sessions**: active 20 min before 09:15 IST to 35 min after 15:30 IST on trading days; idle
  otherwise (no WebSocket, no polling). Weekends + `holidays.txt` respected; a warning is logged if
  no index ticks arrive 20 minutes after the open.
* **Recovery**: WebSocket reconnect with exponential backoff; REST retry with backoff + jitter;
  crashed loops restart in-process; systemd `Restart=always`; heartbeat watchdog restarts a hung
  process; SQLite WAL survives restarts; unlabelled signals are labelled from stored snapshots on
  start-up.
* **Single instance**: `data/engine.lock` (flock) prevents duplicate WebSocket connections.
* **Rate limits**: option chain 1 request / 3.2 s (round-robin across indices), quote 1/s,
  data APIs 5/s, WebSocket ≤ 5000 instruments, 100 per subscribe message.

## 5. Data limitations (documented, not fabricated)

| Requested | Status |
|---|---|
| 3-min / 30-min candles | Dhan serves 1/5/15/25/60; 3/30 are resampled from 1-minute bars |
| Index volume / VWAP | Indices carry no volume; VWAP = futures ATP, volume features use futures |
| Market breadth | Only if `BREADTH_CONSTITUENTS` is configured; otherwise N/A |
| Fama-French India | No reliable Dhan source; disabled. CAPM uses NIFTY as market |
| Vendor IV / Greeks | Stored for reference; analytics use own Black-76 on the parity forward |
| Holiday calendar | No Dhan endpoint; `holidays.txt` + runtime detection |
| Participant intent | Not inferable from public data; classifications are price + OI behaviour only |

## 6. PostgreSQL / TimescaleDB migration

Every time-series table has `ts` (epoch seconds), `ts_utc` and `ts_ist`; JSON lives in TEXT
columns. Map `ts` to `TIMESTAMPTZ` via `to_timestamp(ts)`, TEXT JSON to `JSONB`,
`INTEGER PRIMARY KEY` to `BIGSERIAL`, then `SELECT create_hypertable('chain_snapshots','ts')`.
