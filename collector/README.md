# Bull50 historical stock-options collector

Downloads historical NSE stock-option chains from Dhan for backtesting and turns them into
09:30 / 15:20 IST snapshot CSVs. Research only: it calls one read-only endpoint,
`POST /v2/charts/rollingoption`, and has no order, position or funds code.

## What it collects

| Item | Value |
|---|---|
| Stocks | 60 NSE F&O names in `symbols_60.txt` (NIFTY 50 + 10). Edit the file to change them |
| History | Up to 5 years (Dhan's limit), in 30-day request windows |
| Bars | 1-minute |
| Strikes | ATM, ATM±1, ATM±2, ATM±3, CE and PE (Dhan's maximum for stock options) |
| Fields | close (used as LTP), OI, IV, volume, strike, spot |
| Output rows | 09:30 and 15:20 IST each trading day, 7 strikes each |

CSV columns:
`date,time,symbol,expiry,strike,ce_oi,pe_oi,ce_ltp,pe_ltp,spot,ce_iv,pe_iv,ce_volume,pe_volume,lot_size`

## Option A: run on Dhan Cloud, decode on your PC

Dhan Cloud can't save files, so the cloud script writes its data into the run log and a
decoder on your PC turns the log into the CSVs, ZIP and report.

1. Paste `cloud/stock_options_history_cloud.py` into Dhan Cloud `main.py`. Add **no** Env
   Variables: Dhan Cloud fills `{{CLIENT_ID}}` and `{{ACCESS_TOKEN}}` itself with your
   account's managed credentials (same as the SENSEX observer).
2. In the CONFIG block, try a short range first: `SYMBOLS = ["RELIANCE"]`,
   `FROM_DATE = "2026-09-01"`, `TO_DATE = "2026-11-01"`. Then run the full list (blank
   dates = 5 years).
3. Download the run log and, on your PC in this repo:
   ```
   python -m collector.decode_cloud_log path\to\run.log
   ```
   If a run stopped (token expired, daily request budget), it logs
   `START_AT_SYMBOL = '...' and START_AT_DATE = '...'`. Set both, run again, then decode all
   the logs together:
   `python -m collector.decode_cloud_log run1.log run2.log`.

The run starts with a probe that finds a request shape Dhan accepts. In practice
(3 Nov 2026) Dhan refused any stock-option window spanning more than one day (`fromDate`
to `toDate`, inclusive) with DH-905, despite its docs saying 30 days. So a full 5-year,
60-stock pull is about 657k requests: about 7 days at the ~95k/day budget, one run a day.

By default the cloud run keeps, per leg and day, only the last bar at or before 09:30 and
15:20 (at most 15 min old, `ASOF_WINDOW_MIN`), with values unchanged (`KEEP = "SNAPSHOTS"`).
`KEEP = "FULL"` (every 1-min bar) makes a log far too large for Dhan Cloud. Each record also
logs how many bars Dhan returned, for audit.

## Option B: run everything on your PC

Run on a Windows, Mac or Linux machine with Python 3.9+. It uses only the standard
library, so there's nothing to `pip install`. This keeps the complete raw 1-min responses.

1. Get the repo: `git clone https://github.com/satyajit8080/Dhan` (branch `claude/dhan-history-pull`).
2. Set credentials. These are the same variables the Bull50 MCP server uses. Pick one way:
   - **Environment** (PowerShell): `$env:DHAN_ACCESS_TOKEN="..."; $env:DHAN_CLIENT_ID="..."`
     (Command Prompt: `set DHAN_ACCESS_TOKEN=...`, Mac/Linux: `export DHAN_ACCESS_TOKEN=...`)
   - **Token file**: put the token in a file and set `DHAN_TOKEN_FILE` to its path.
   - **`server/.env`** (git-ignored): lines `DHAN_ACCESS_TOKEN=...` and `DHAN_CLIENT_ID=...`.
3. From the repo folder, do a short test run first:
   ```
   python -m collector.collect run --symbols RELIANCE --from 2026-09-01 --to 2026-11-01
   ```
   That's 2 windows × 14 legs = 28 requests. Open the validation report it prints and check
   the expiry-mapping line (see below).
4. Full run, all 60 stocks for 5 years:
   ```
   python -m collector.collect run
   ```

The token is never printed (only its last 4 characters), logged or written to disk.

### Size and time of a full run

- About 60 stocks × 61 windows × 14 legs ≈ **51,000 requests**. At the default 4 req/s
  that's about **3.5 hours**, inside Dhan's ~100k/day data limit.
- Raw responses take roughly **2–5 GB** (gzip JSON) in `data/options_history/raw/`.
- Dhan tokens last 24 hours. If the token expires mid-run, the run stops cleanly. Set a new
  token and run the same command again.

## Resume, retries and errors

- Every response is saved as soon as it arrives. A request with a saved response is never
  sent again, so you can stop (Ctrl+C) and re-run at any time.
- Rate limits (HTTP 429 / DH-904), 5xx errors and network errors are retried up to 6 times
  with exponential backoff. If a request still fails, it goes to `logs/errors.jsonl` and is
  retried on the next run. It's also listed as `REQUEST_NOT_FETCHED` in the missing-data report.
- A rejected token (401/403, DH-901/902) stops the run straight away.
- `--max-requests N` stops after N calls (a daily budget). `--rps` sets the request rate.
- When Dhan answers "no data" (for example, before a stock entered F&O), that answer is
  saved too. Use `--retry-empty` to ask again.

## Output

Everything is under `data/options_history/` (git-ignored):

```
raw/<SYMBOL>/<from>_<to>/ATM+1_CE.json.gz   raw response + request + fetch time (audit)
logs/errors.jsonl                          failed requests
instruments/scrip_master_<date>.csv        the instrument master used
instruments/resolved.csv                   security id, lot size, live expiries per stock
exports/<from>_<to>/all_symbols.csv        all rows
exports/<from>_<to>/csv/<SYMBOL>.csv       one file per stock
exports/<from>_<to>/missing_data.csv       every gap, one line each
exports/<from>_<to>/validation_report.md   data validation report
exports/<from>_<to>/summary.json
exports/bull50_stock_options_<from>_<to>.zip   all of the export folder
```

`python -m collector.collect build` rebuilds the exports from raw without calling the API.
Use it, for example, with a different `--tolerance`.

## Data rules

- **Nothing is made up.** A price, OI, IV or volume Dhan didn't return stays blank in the CSV
  and is listed in `missing_data.csv`. Nothing is interpolated or carried forward.
- **As-of snapshots.** Dhan only has a bar for minutes in which the option traded, and stock
  options often skip minutes. Each leg's 09:30 / 15:20 value is its last trade at or before
  that time, at most 15 minutes old (`--tolerance`, 0 = exact minute only). The `time`
  column is the snapshot time; the report shows how many legs came from an earlier minute
  and the oldest one. A leg with no trade in the window stays blank.
- Duplicate bars (same stock, minute, strike, side) are dropped. Duplicates whose values
  disagree are counted in the report.
- **Missing-data issues:** `NO_DATA_DAY`, `SNAPSHOT_MISSING`,
  `STRIKES_INCOMPLETE`, `CE/PE_LEG_MISSING`, `CE/PE_<FIELD>_MISSING`, `SPOT_MISSING`,
  `REQUEST_NOT_FETCHED`.

## Instruments and F&O eligibility

- Security ids and lot sizes come from Dhan's instrument master (detailed CSV, with the
  compact one as fallback). The underlying id on the stock's OPTSTK contracts is used, and
  cross-checked against the NSE equity id.
- Status per stock: `OK`, `NOT_IN_FNO` (no live option contracts), `NOT_FOUND`, `ID_MISMATCH`.
  Stocks that can't be resolved are skipped and listed.
- The master only shows **current** contracts. Historical eligibility is taken from the data:
  the report shows the first and last day Dhan returned bars for each stock.
- `lot_size` is today's lot size. NSE revises lot sizes, so older rows may have traded at a
  different lot.

## Expiry mapping

Dhan's rolling data has no expiry date, so it is derived:

- the last Thursday of the month, or the last Tuesday from Sep 2025 onwards (the NSE change);
- moved back to the previous market day when no stock traded on that date (a holiday).
  `--holidays file.txt` adds known holidays.

Two checks in the report:

1. **Expiry-day straddle.** At 15:20 on expiry day, the ATM CE + PE should be almost all
   intrinsic value (well under 1.5% of spot). If `expiryCode` 1 meant the *next* month, it
   would be several percent. Check this line after the first test run.
2. **Live expiries.** The rule is compared with the expiries currently listed in the master.

## Tests

```
python -m unittest collector.tests.test_collector collector.tests.test_cloud_roundtrip
```

The round-trip test runs the cloud script against the fake server, decodes its log and
checks that the cloud file has none of the patterns Dhan Cloud's scanner blocked
(`os`, `pathlib`, `os.environ`, `sys.exit(`, file `open(`).

The tests run offline against a fake Dhan server that serves synthetic bars, with injected
429/5xx/auth errors, a holiday, a missing 09:30 bar and a missing IV. They cover resume, the
expiry mapping, no-fabrication, dedup, the ZIP contents and that the token never reaches disk.
