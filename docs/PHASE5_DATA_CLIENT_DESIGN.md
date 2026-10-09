# Phase 5 — Local Read-Only Dhan Integration: Design

Branch `claude/eloquent-carson-o3l0po` (base `b87f65a`). Local development
only: no Dhan Cloud, no deployment, no orders, no login or token automation,
no CE/PE signals. `RULES.md`, `server/src/**` and `plugin/**` are unchanged.

## 1. Pipeline

```
DhanClient (read-only HTTP)      engine/sensex/dhan_client.py
   ├─ allow-list guard, rate limits, bounded retries, error classes, redaction
   └─ returns raw payload + receive time + envelope shape
validation                        engine/sensex/validation.py
   └─ schema / completeness / consistency / duplicate-snapshot checks
Phase-4 engine (unchanged)       engine/sensex/{normalize,pricing_bridge,levels,scan}.py
   └─ normalise → skew rule → parity forward → gate → Black-76 IV → levels
existing refresh_table.py         engine/refresh_table.py (unchanged)
Scanner + CLI                     engine/sensex/scanner.py, engine/scan_local.py
   └─ timestamped record, JSON-lines log, table on stdout
```

Transport, normalisation and calculation are separate modules. The client
returns raw payloads; nothing in it computes a price.

## 2. Client choice: small stdlib adapter, not the official SDK

The SDK (`dhan-oss/DhanHQ-py` v2.3.0, read at `8c6583e`) is the reference for
request shapes, but it is not used at runtime, because:
- it pulls in pandas and numpy (a reported dependency conflict on Dhan Cloud);
- it turns HTTP status codes into `remarks`, which hides 401/429 from the caller;
- its error logging can expose credentials (Phase 3, tested on `generate_token`);
- the adapter is about 300 lines of stdlib code with every behaviour unit-tested.

## 3. Endpoints and schemas: what is verified

| Call | Request | Source | Status |
|---|---|---|---|
| Expiry list | `POST /v2/optionchain/expirylist` `{UnderlyingScrip, UnderlyingSeg}` | SDK `_option_chain.py` | **VERIFIED (official source)** |
| Option chain | `POST /v2/optionchain` `{UnderlyingScrip, UnderlyingSeg, Expiry}` | SDK `_option_chain.py` | **VERIFIED** |
| Quotes (LTP, bid/ask, 5-level depth, OI, OHLC, last trade time) | `POST /v2/marketfeed/quote` `{SEGMENT: [ids]}`, ≤ 1000 ids | SDK `_market_feed.py`; skills `market-data.md` | **VERIFIED** |
| Intraday candles | `POST /v2/charts/intraday` `{securityId, exchangeSegment, instrument, interval, oi, fromDate, toDate}`; interval 1/5/15/25/60 | SDK `_historical_data.py` | **VERIFIED** |
| Daily candles | `POST /v2/charts/historical` `{…, expiryCode, oi, fromDate, toDate}` | SDK `_historical_data.py` | **VERIFIED** |
| Token/plan check | `GET /v2/profile`, header `dhanClientId` | SDK `auth.py` | **VERIFIED** |
| Headers | `access-token`, `client-id`, JSON content type | SDK `dhan_http.py` | **VERIFIED** |
| SENSEX underlying | `51` / `IDX_I`; derivatives `BSE_FNO`; index instrument `INDEX` | skills SKILL.md quick reference; annexure values in skills | **VERIFIED** |
| Chain fields used | `data.last_price`, `data.oc["<strike>"].ce/pe.{last_price, top_bid_price, top_ask_price, oi, previous_oi, security_id, implied_volatility, greeks}` | skills `option-chain.md` (all but `previous_*`) | VERIFIED except `previous_oi` / `previous_close_price` (**UNVERIFIED**; used only for OI buildup, not by the refresh path) |
| Response envelope `{status, data}` | Option chain: shown wrapped in skills `option-chain.md`. `/charts` and `/marketfeed`: sources conflict | — | **UNVERIFIED.** The client accepts both shapes and logs which one it received (`envelopes` in each record) |
| SDK adds `dhanClientId` to every JSON body | Our client, like the live-used TS client, does not | SDK `dhan_http.py` | Difference **recorded**; the smoke test will show whether Dhan requires it |
| Segment instrument list `GET /v2/instrument/{segment}` | — | docs search excerpt only; not in the SDK | **UNVERIFIED**: not implemented |

**Risk flagged for the TS plugin.** The TS transport throws "Dhan response had
no data block" for any body without `data`. If `/charts/intraday` returns bare
arrays, the plugin's candle fetch would always fail. That would match SKILL.md's
"candle endpoint being down" notes. **UNVERIFIED**; the first recorded live
scan settles it (`envelopes.intraday` in the log).

## 4. Instrument resolution (futures contract): BLOCKED

- `sensex/instruments.py` parses the instrument master **only** with an
  explicit `InstrumentMapping` JSON that you create from the real header. It
  names column headers, cell values (exchange, instrument type, underlying),
  the expiry date format and `verified_from`. No mapping ships with the code.
- Selection rule = existing `registry.ts futuresForExpiry`: the nearest contract
  with expiry ≥ option expiry. Expired contracts are excluded. Several ids on
  the same nearest expiry are refused as ambiguous. Malformed ids, dates and
  lot sizes are counted, never guessed.
- **Until the header is verified**, the scanner needs `--futures-security-id`
  and `--futures-expiry`, and refuses a contract that has expired or expires
  before the option.

Mapping file shape, filled in **only** from the verified header:

```json
{
  "verified_from": "header pasted on <date> from <URL>",
  "columns": {"exchange": "?", "security_id": "?", "instrument": "?", "underlying": "?",
              "expiry": "?", "lot_size": "?"},
  "values": {"exchange": "?", "instrument": "?", "underlying": "?"},
  "expiry_format": "?"
}
```

**To unblock:** run `curl -s https://images.dhan.co/api-data/api-scrip-master.csv | head -2`
on your computer, plus one line for a SENSEX future:
`curl -s https://images.dhan.co/api-data/api-scrip-master.csv | grep -i sensex | grep -i fut | head -3`.
Paste the output (public data, no credentials).

## 5. Validation and safety controls

| Control | Where | Behaviour |
|---|---|---|
| Read-only allow-list | `dhan_client.READ_ONLY_ENDPOINTS` + `FORBIDDEN_FRAGMENTS` | Only 8 `(method, path)` pairs. Order, position, kill-switch, P&L-exit, IP, token, eDIS and margin paths refused before any socket |
| Timeouts | 15 s per request | — |
| Bounded retries | max 4 attempts; exponential backoff with jitter (cap 20 s) | Retried: network errors, timeouts, 5xx, 429, `805`, `DH-904`, `800`, `DH-908/909`. `Retry-After` honoured |
| Not retried | `AuthError` (401/403, `DH-901`, `807–810`), `PlanError` (`806`, `DH-902`), `RequestRejectedError` (`811`, `DH-905`, other 4xx) | Auth/plan failures stop the scanner |
| Rate limits | token buckets: option chain 1 per 3 s per key, quote 1/s, data 5/s, non-trading 20/s | — |
| Chain schema | `validate_chain` | Object with non-empty `oc`, ≥ 2 strikes with numeric CE+PE prices. Non-numeric keys and missing index LTP are warnings |
| Quote / candles / expiries | `validate_quote`, `validate_candles`, `validate_expiry_list` | Missing segment/instrument/LTP; ragged or unordered arrays; malformed dates |
| Freshness | existing engine | 3 s chain↔futures skew (gate); futures last-trade age > 15 s (warning); candle↔snapshot > 350 s → STALE rows; no candles → blank table |
| Duplicate snapshot | `snapshot_fingerprint` | Identical chain body on consecutive scans → `DUPLICATE_SNAPSHOT` warning (no rule says to block, so it doesn't) |
| Missing futures quote | scanner | Passed as missing, so the gate blocks with `NO_FUTURES_QUOTE`. Never the index LTP |
| Candle endpoint down | scanner | `CANDLES_UNAVAILABLE` warning; table blank per RULES.md §5. Nothing is estimated |
| Redaction | `Redactor` on every log line and error message | Registered token and client id plus anything JWT-shaped; `Credentials.__repr__` hides values |

## 6. Scanner behaviour

- **Order per scan:** expiry list → next weekly after today (RULES.md §6) →
  option chain → futures quote → today's 5-minute index candles 09:15–15:30
  (RULES.md §4) → engine → table.
- **Statuses:** `OK`, `BLOCKED` (gate or skew), `SKIPPED` (weekend, holiday,
  before open, after stop), `INVALID_DATA`, `CONFIG_BLOCKED` (no or expired
  futures contract), `ERROR` (API after retries), `AUTH_FAILED`, `PLAN_MISSING`.
  The last two stop the loop.
- **Loop:** configurable interval (≥ 10 s). It stops at `--stop-at` (default
  15:30 IST), on `--max-scans`, on Ctrl-C/SIGTERM (it sleeps in ≤ 1 s slices),
  or on an auth/plan failure. It does not start outside the session. It never
  starts on import or in tests.
- **Holidays:** a file you maintain (`--holidays`); no list is invented.
  Without it, a holiday shows up as `NO_CANDLES` and `DUPLICATE_SNAPSHOT` warnings.
- **Strikes:** passed explicitly, since the ATM±2 rule is undefined
  (PHASE4_PORT_PLAN P1). Each record logs `atmStrike` to help you choose.
- **Legs with no IV:** excluded and listed in `excludedLegs` (PHASE4 P2, unchanged).
- **Output:** a header line plus the table on stdout; one JSON line per scan in
  `scan-logs/scan-<date>.jsonl` (redacted). `--record DIR` saves raw
  market-data bodies for offline replay; headers, credentials and `/profile`
  are never saved.

## 7. Local credentials: secure setup on your own computer

Never put the token in code, on the command line, or in this chat. Use a file
only you can read, and type the token without echoing it.

macOS / Linux (each morning, after generating the token in Dhan web):
```bash
mkdir -p ~/.dhan && chmod 700 ~/.dhan
read -rs -p "Dhan token: " T && printf '%s' "$T" > ~/.dhan/token && unset T && chmod 600 ~/.dhan/token
read -rs -p "Dhan client id: " C && export DHAN_CLIENT_ID="$C" && unset C
export DHAN_TOKEN_FILE=~/.dhan/token
```

Windows PowerShell:
```powershell
$s = Read-Host "Dhan token" -AsSecureString
[IO.File]::WriteAllText("$HOME\.dhan-token", [Net.NetworkCredential]::new('', $s).Password)
icacls "$HOME\.dhan-token" /inheritance:r /grant:r "${env:USERNAME}:R"
$env:DHAN_TOKEN_FILE = "$HOME\.dhan-token"
$env:DHAN_CLIENT_ID = Read-Host "Dhan client id"
```

The scanner checks the token's expiry claim locally and refuses an expired
token before making any call.

## 8. Dhan Cloud (out of scope): prerequisites for the later dry run

The Phase 3 checklist must first establish:
- the Python version and that `scan_local.py` imports (stdlib only, so packages
  should not be needed);
- whether `os.environ` and file reads are allowed, which decides how credentials
  are supplied;
- whether files persist, which matters for the JSON-lines log, the duplicate
  check and holidays;
- outbound HTTPS to `api.dhan.co`;
- scheduling and the 15:30 auto-stop.

The dry run will stay read-only and log-only.
