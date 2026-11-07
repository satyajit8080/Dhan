# Dhan Cloud bundle

The full engine (`../main.py`) cannot run on Dhan Cloud. It writes SQLite and log files, opens a
WebSocket plus an HTTP port and reads environment variables. Dhan Cloud allows none of these
(constraints in `../RULES.md` §7). This folder holds the cloud version of the **indicator strategy**.
It uses the same formulas and rules as `main.py`, and `tests/test_indicators.py` checks that both
give the same results to within 1e-7.

| File | Lines | Purpose |
|---|---|---|
| `main.py` | entry | sleep-paced loop; on every closed 5m bar computes all 5 indices, prints the card, Telegram on new LONG/SHORT |
| `config.py` | | `{{VAR}}` placeholders, index ids, rule parameters |
| `indicators.py` | | VWAP, ATR, Supertrend, ADX/DI, RSI, Bollinger BW, ER, Z, pivots (pure Python) |
| `rules.py` | | trend / mean-reversion / squeeze rules, level filter, sizing, charges, Black-76 |
| `signal_card.py` | | strike pick (ATM/ITM, spread-checked), entry/SL/targets, Telegram text |
| `data.py` | | read-only Dhan v2 REST (allow-listed), instrument master streamed in memory |

Every file has ≤ 200 lines. The code has no `os`, no file writes, no subprocess and no sockets. Dependencies are pinned in
`requirements.txt`.

## Deploy at cloud.dhanhq.co (done by you: Claude cannot log in to your account)

1. Create a new Python 3.11 project/strategy.
2. Upload every `.py` file in this folder plus `requirements.txt`, and set `main.py` as the entry file.
3. Add the Variables `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
   `config.py` reads them as `{{NAME}}`. Update `DHAN_ACCESS_TOKEN` each day.
4. Start the project. The logs print `instruments: ...` once a day, then one line per index after every
   closed 5-minute bar between 09:30 and 15:10 IST.

Before you deploy, check that the Dhan Cloud console uses the `{{NAME}}` syntax and the entry-file
name assumed above. I took both from `RULES.md` and could not confirm them in the console.
