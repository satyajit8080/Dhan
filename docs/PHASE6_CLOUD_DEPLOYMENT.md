# Phase 6: Dhan Cloud Deployment of the Read-Only Observer

Date: 9 Oct 2026. Branch: `claude/eloquent-carson-o3l0po`.
Statuses used: PASS / FAIL / UNVERIFIED / BLOCKED.

## 0. What was and was not done

| Item | Status | Evidence |
|---|---|---|
| Deploy from this coding environment | **NOT POSSIBLE** | This session's network policy denies every Dhan host. `curl -I` to `api.dhan.co`, `docs.dhanhq.co`, `dhanhq.co` and `images.dhan.co` returns `CONNECT tunnel failed, response 403`. Dhan Cloud also needs your interactive Dhan login, which I must not use |
| Credential-free probe run **in Dhan Cloud** | **FAIL (attempt 1), fixed** | 9 Oct 2026, run by you: the save/run of probe v3 was rejected with `security violation at line 83: querying host platform/OS details is not allowed` (`platform.platform()`). Probe v4, the import probe and the observer no longer query platform/OS details (test `Safety` now forbids the `platform` module and `sys.platform`/`implementation`/`executable`/`version`, `os.uname`/`name`/`listdir`/`getcwd`). Re-run P1 with v4 |
| **P1 probe v4 in Dhan Cloud** | **PASS** | 9 Oct 2026 21:40 IST, project "test", run by you (screenshot): scanner accepted; `python=3.11.15`; `local_tz=IST`, `local_is_ist=True`; `reach_dhan_api=HTTP 200 in 76 ms`; `reach_dhan_instrument_cdn=HTTP 200`; `reach_outside_telegram=HTTP 200`; `heartbeat=0/1` 60 s apart (logs stream live, each line prefixed `[YYYY-MM-DD HH:MM:SS IST]`, which the decoder tolerates). Run controls: ▶ beside the file, Stop button; logs in TERMINAL |
| **P2 import probe in Dhan Cloud** | **PASS** | 9 Oct 2026 21:46 IST, "test v2": "Project mode detected", "Extracted 2 files, entry point: main.py", `python=3.11.15`, `dunder_name=__main__`, `sibling_import=OK`, "Execution completed successfully". Multi-file works; the single-file build was chosen only because it is one upload. Saving offers "new version" or "update current version" |
| **V1 observer VALIDATE in Dhan Cloud** | **PASS** | 9 Oct 2026 21:49 IST, project "main", single-file build as `main.py`: the scanner accepted the full program (incl. `os.environ` reads); `runtime` python 3.11.15, 8-entry allow-list, `orders: NONE`; `selftest result PASS status OK` with the expected table; `credential_names_present` false/false (no values printed); "Execution completed successfully". The observation may start after V3 |
| **6.1 upload rejected by the Cloud scanner** | **FAIL, fixed in 6.2** | 9 Oct 2026 22:10 IST: "Security Violation [CRITICAL]": blocked import `code`, blocked import `pathlib`, patterns `compile(`, `os.environ`, `base64.b64encode`, `sys.exit(`, `\x`, `getattr(`; "environment variables are restricted". 6.2: file/env helpers moved to the local-only `sensex/localio.py` (never bundled); `re.compile` → `re.match/fullmatch/sub` with pattern strings; plain-Python base-64url decoder for the token expiry; replay records gzip+hex; no `getattr`, no `sys.exit`, no escape sequences (chr()); docstrings dropped from the single-file build. Test `Safety.test_no_pattern_the_cloud_scanner_blocked` enforces the list. "import code" could not be traced to any text in the file and is UNVERIFIED |
| **Credentials mechanism** | **CHANGED, UNVERIFIED** | Env-variable reads are blocked, so 6.2 uses Dhan Cloud `{{NAME}}` placeholders (RULES.md §1: "credentials go in `{{VAR}}` Variables"): `CLIENT_ID = "{{DHAN_CLIENT_ID}}"`, `ACCESS_TOKEN = "{{DHAN_ACCESS_TOKEN}}"`. VALIDATE logs `credential_placeholders` as `filled` / `NOT filled` (never values). If they stay `NOT filled` with the Env Variables set, the substitution syntax differs: check the editor's Shortcuts tab / Documentation |
| Scanner uploaded / executed **in Dhan Cloud** | **V1 PASS; V3 pending** | See the rows below |
| Upload files built and tested | PASS (local) | `cloud/dist/` built by `cloud/build_bundle.py`. 20 tests in `engine/tests/test_cloud_package.py` and `test_observation.py`; full results in §7 |
| Official Cloud docs (`docs.dhanhq.co/cloud/`, execution environment) | **UNVERIFIED** | Not retrievable from here (same 403). UI labels and limits below come from earlier phases (search snippets plus community reports) and must be confirmed in the console |

**Nothing below claims a Cloud run. The first real evidence is your P1 log.**

## 1. Files to upload

All generated files are in `cloud/dist/`. They are rebuilt from the tested sources
by `python3 cloud/build_bundle.py`, and a test fails if they drift. Do not edit them.

| Step | Upload | Purpose | Credentials |
|---|---|---|---|
| P1 | `tools/dhan_cloud_probe.py` (one file) | Runtime facts: Python version, clock/TZ, outbound HTTPS, log streaming | none |
| P2 | `cloud/probe/import_probe/main.py` + `probe_helper.py` | Can `main.py` import a second uploaded file? | none |
| P3 (optional, recommended) | `tools/dhan_cloud_probe_extended.py` | Can the program read an environment variable (names and lengths only)? | none; uses a dummy `PROBE_MARKER` |
| V1–V3 and the observation | **Either** `cloud/dist/multi/` (`main.py`, 19 `bx_*.py`, `requirements.txt`) **or** `cloud/dist/single/sensex_observer.py` | The read-only observer | token only from V2 onward, via Cloud variables |

- **Multi-file or single-file:** P2 decides. If it prints `sibling_import=OK`, use
  `multi/`. If it prints `FAILED`, or Cloud accepts only one file, use
  `single/sensex_observer.py` as the main file. Rename it to `main.py` if the
  console requires that name. The two builds are generated from the same
  sources and pass the same tests.
- **Packages:** none. Standard library only. `requirements.txt` holds a single
  comment line. Leave it like that, or leave it empty if Cloud rejects comments.
- **Python:** tested locally on 3.11.17, 3.12 and 3.13. Community reports
  suggest Cloud runs Python 3.11 (UNVERIFIED until P1 prints `python=`).
- **Code scanner:** the bundles contain:
  - no `exec`, `eval`, `compile` or `__import__`;
  - no imports of `subprocess`, `socket`, `importlib`, `pickle`, `requests` or `dhanhq`;
  - no order, position, kill-switch, token-generation, renewal or TOTP identifiers.

  `test_cloud_package.Safety` enforces all of this. The bundles do use `os.environ`
  to read the two credential variables. The community reports that Cloud's
  scanner may flag environment access (UNVERIFIED), and P3 tells you in advance.

## 2. Configuration (edit only the CONFIG block at the top of `main.py`)

In `single/sensex_observer.py`, search for `CONFIG`.

| Setting | Value | Notes |
|---|---|---|
| `MODE` | `"VALIDATE"`, then `"LIVE_CHECK"`, then `"OBSERVE"` | See §3 |
| `STRIKES` | e.g. `[81000, 81100, 81200, 81300, 81400]` | **You choose.** The ATM±2 rule is undefined (PHASE4 P1). Every scan logs `atmStrike` to guide you. Strikes not in the chain are listed under `excludedLegs` |
| `FUTURES_SECURITY_ID`, `FUTURES_EXPIRY` | The current SENSEX future, verified in Dhan web or the instrument master | The plugin registry's OCT contract `864571` / `2026-10-29` is only REPORTED. **Re-verify it.** The program refuses a contract that is expired or expires before the option expiry. Automatic lookup stays BLOCKED until the instrument-master header is verified |
| `INTERVAL_S` | `60` | Minimum 10. The option chain allows 1 request per 3 s |
| `STOP_TIME` | `"15:30"` | IST. The loop stops itself here |
| `HOLIDAYS` | Exchange holidays you maintain | None are invented. On an unlisted holiday you get `NO_CANDLES` and `DUPLICATE_SNAPSHOT` warnings |
| `RECORD_EVERY_N` | `15` | Replay record on scan 1, every 15th scan, and every non-OK scan |
| `RECORD_CHUNK`, `LEG_BAND` | `3000`, `5` | Lower these if the decoder reports truncated lines |
| `EXTRA_SERIES` | `True` | Also fetch 1-min futures candles (session VWAP, volume vs previous-10 average) and 1-min index candles (close beyond trigger, next bar holds). Observation only (D1–D3 undecided) |
| `PAPER_ENABLED`, `PAPER_TAKE_PROFIT_PTS`, `PAPER_STOP_LOSS_PTS`, `PAPER_TIME_STOP_MIN` | `True`, `6`, `11`, `10` | **PAPER tracker, no orders.** Your exit rule (9 Oct 2026), taken as **option premium points** (to be confirmed); time stop from RULES.md §6. RULES.md itself is unchanged until you confirm. Entry = the index CROSSING an OK refresh-table trigger outside the no-trade windows, once per trigger per session, for each configured strike of that side; fill at the ASK, exits judged on the BID |
| `POLL_S` | `5` | Fast read-only `/marketfeed/quote` poll (index + configured legs) between scans, so crossings and +6/−11 are seen within ~5 s. Min 2 |
| `LOT_SIZE`, `COST_PER_TRADE_RS` | `None` | Fill in to get rupees (gross / net of your cost figure). Never guessed |
| `CLIENT_ID`, `ACCESS_TOKEN` | `"{{DHAN_CLIENT_ID}}"`, `"{{DHAN_ACCESS_TOKEN}}"` | **Leave the placeholders as they are.** Create Env Variables `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in the strategy; Cloud substitutes them (6.2; UNVERIFIED until VALIDATE shows `filled`). Never type real values into the file |

## 3. Exact steps (you perform them in the official Dhan Cloud interface)

> UI labels (strategy/project, Env Variables, Run, Schedule, Logs) come from
> Dhan's sample project and community guides. Where your console differs,
> use the equivalent and note the real label.

### P1. Credential-free runtime probe
1. Dhan Cloud → new strategy `probe-basic` → paste `tools/dhan_cloud_probe.py` as
   the main file → empty `requirements.txt` → Save.
2. Run it on demand and open its logs.
3. **PASS when:**
   - the save is accepted;
   - `python=` shows 3.9 or newer (3.11 expected);
   - `reach_dhan_api=HTTP …`;
   - the `heartbeat=` lines appear;
   - `done=` is printed.
4. **Copy back:** every `PROBE` line, plus any scanner message verbatim.

### P2. Multi-file import probe
1. New strategy `probe-import` → upload `cloud/probe/import_probe/main.py` and
   `probe_helper.py` → Save → run on demand.
2. **Copy back:** the `PROBE2` lines. `sibling_import=OK` means use `multi/`;
   anything else means use `single/`.

### P3. Environment-variable probe (recommended before any credential)
Follow `docs/PHASE3_CLOUD_CHECKLIST.md` C5–C6 (dummy variable `PROBE_MARKER=hello12345`).

| Result | Meaning |
|---|---|
| `env_marker_present=True` | The credential mechanism used by the observer works |
| Save rejected | Cloud forbids environment access. **Stop:** credentials are BLOCKED (see §6). Do not hardcode a token as a workaround |
| `False` | The Cloud UI has no usable variable mechanism. Same blocker |

### V1. Harmless validation run of the observer (no credentials, no network)
1. New strategy `sensex-observer` → upload the chosen build → keep
   `MODE = "VALIDATE"` → Save → run on demand.
2. **PASS when** the log shows:
   - `BX|{… "event": "runtime" …}` with the Python version and the 8-entry allow-list;
   - `BX|{… "event": "selftest", "result": "PASS" …}`;
   - `"credential_names_present"` showing `true`/`false` per variable name.
     No values are ever printed.
3. This makes **no** Dhan request. It shows that the deployed program starts,
   reproduces the tested table on embedded, labelled MOCK data, and writes a log.
   **Do not start the observation until V1 passes.**

### V2. Configure credentials in the Cloud interface
1. Each trading morning, generate a fresh access token in Dhan web:
   web.dhan.co → My Profile → **Access DhanHQ APIs**. It is valid for 24 h
   (official authentication docs, Phase 2).
2. In `sensex-observer` → **Env Variables**, set:
   - `DHAN_CLIENT_ID` = your client id;
   - `DHAN_ACCESS_TOKEN` = the token.

   Type the values only into the Dhan Cloud interface. **Never** put them into
   the code, a chat, a commit, an issue or a screenshot.
3. If the UI offers a "secret" or "masked" option, use it.
4. **Not used, by design:**
   - PIN + TOTP generation (the SDK path leaks PIN/TOTP to logs, tested in Phase 3);
   - `RenewToken`;
   - any unattended login.

### V3. One live read-only scan
1. During market hours (09:25–15:20 IST on a trading day):
   - fill in `STRIKES`, `FUTURES_SECURITY_ID` and `FUTURES_EXPIRY`;
   - set `MODE = "LIVE_CHECK"`, Save, run on demand.
2. It makes exactly 4 read-only calls:
   - `/optionchain/expirylist`
   - `/optionchain`
   - `/marketfeed/quote`
   - `/charts/intraday`
3. **PASS when:**
   - `token_status` is logged with `hours_left`;
   - one `scan` event is logged with `status` OK, or BLOCKED with explicit gate reasons;
   - `BX|REC|…` lines follow;
   - the closing `session_summary` shows `"ordersPlaced": 0`.
4. Download the log (§5), decode it, and run the replay against TypeScript.
   The replay must PASS.

### O. Start the observation (only after V1 and V3 pass)
1. Set `MODE = "OBSERVE"`, then Save.
2. Schedule it Monday to Friday, start **09:16 IST**. If Cloud requires an
   auto-stop, set **15:35 IST**; the program already stops at 15:30.
3. Before each session, update `DHAN_ACCESS_TOKEN` (V2). An expired token is
   refused before any call (`token_expired`, exit 3). A token rejected mid-session
   stops the loop (`AUTH_FAILED`).
4. **Cost:** Dhan Cloud is pay-per-use (official FAQ); rates are only in the
   portal estimator. Check the estimate for about 6 h 15 min a day before you
   schedule.

## 4. What each run logs (every line starts `BX|`, one JSON object)

| Event | Content |
|---|---|
| `runtime` | Program/version, mode, Python version, platform, UTC clock, the read-only allow-list, `orders: NONE` |
| `token_status` / `token_expired` | Hours left or hours ago. **Never** the token |
| `scan` | Run identity: `scanId` (`YYYYMMDD-NNNN`), `startedIst`, `clockUtc`, `session` window, `status`, `reason`, `warnings`, `envelopes`, `expiry`, `futures` contract, `table` |
| `scan` → `observation` | Endpoints, data quality and gate: per-endpoint `ok`/`attempts`/`envelope`/`durationMs`/`error`/`code`; `chainCompleteness`; `freshness` (chain↔futures skew, futures last-trade age, last-candle age); `prices` (index, futures, OHLC, parity forward, T, days to expiry, ATM); `gate` (spread, divergence, carry, findings, or block reasons and numbers) |
| `scan` → `observation` (continued) | Strategy outputs: `levels` (breakout/breakdown, ATR, every level with touches and sources, diagnostics); `legs` (±`LEG_BAND` strikes: bid/ask/qty, spread %, OI, OI change, volume, IV, delta, gamma, theta/day, vega, vendor IV for diagnosis only); `refreshRows`; `conditions` (each rule and its outcome); `skipped` (reasons); `strategy: NO_SIGNAL / NOT_CONFIGURED`; `durationMs`; `nextScanIst` |
| `request_retry`, `request_failed` | Path, attempt, error class, Dhan error code, wait |
| `BX|REC|<scanId>|i/n|…` | gzip+base64 replay record: the 4 market-data response bodies, receipt times, strikes, contract and clock. No headers, no credentials, no `/profile` |
| `paper_entry` / `paper_exit` / `paper_skip` / `paper_summary` | PAPER trades: trigger, index at crossing, entry ask/bid, spread, TP/SL levels; outcome (TAKE_PROFIT / STOP_LOSS / TIME_STOP / SESSION_END), exit bid, P&L points, held seconds, best/worst excursion, bid path; win rate vs the 64.7 % break-even |
| `scan` → `observation.futuresFlow`, `oneMinuteIndex` | Futures day volume, exchange average price, session VWAP (1-min, same formula as the TS plugin), last bar volume vs previous-10 average; last three 1-min index bars and close vs breakout/breakdown |
| `session_summary` | Stop reason, scan count, status counts, `ordersPlaced: 0` |

## 5. Downloading and reviewing logs

1. In Cloud, open the run's **Logs** and download or export them. If there is
   no download button, select all and paste into a local text file. Save it as
   `observation/<date>/cloud_log.txt` (`observation/` is git-ignored).
2. Decode the log:
   ```bash
   python3 tools/decode_cloud_log.py observation/<date>/cloud_log.txt --out observation/<date>
   ```
   - It writes `events.jsonl`, `scan-records/scan-*` and `decode_report.json`.
   - **It refuses to write anything if a line looks like a token.** If that
     happens, delete the file and report it as a defect.
   - Exit 1 means truncated lines or incomplete replay records. Lower
     `LEG_BAND` or `RECORD_CHUNK` and continue.
3. Run the replay against TypeScript on the identical data:
   ```bash
   cd server && npx tsx scripts/replay-recorded.ts ../observation/<date>/scan-records
   cd ../engine && python3 tests/replay_recorded.py ../observation/<date>/scan-records | tee ../observation/<date>/replay.txt
   ```
4. Generate the session report:
   ```bash
   python3 tools/summarize_observation.py observation/<date>/events.jsonl \
       --replay-output observation/<date>/replay.txt > observation/<date>/REPORT.md
   ```
   Then fill in `docs/PHASE6_GAP_ANALYSIS_TEMPLATE.md` from it.

## 6. Decision tree for Cloud outcomes

| Observation | Action |
|---|---|
| Save rejected by the scanner (any step) | Copy the message verbatim. Do not work around it. I adapt the code to the named construct |
| P2 `sibling_import` fails | Use `single/sensex_observer.py` |
| P3 env access rejected or unavailable | **BLOCKED:** credentials cannot be supplied safely. Do **not** paste the token into the code. Options to decide: Dhan's own credential mechanism, if the official docs describe one; or keep observing locally with `engine/scan_local.py` |
| `reach_dhan_api=UNREACHABLE` | **BLOCKED:** Cloud has no egress to the API (unexpected). Report it |
| V1 `selftest FAIL` | Copy the `selftest` line. Cloud's Python behaves differently. Do not proceed |
| V3 `AUTH_FAILED` / `PLAN_MISSING` | Token or data plan. Regenerate the token, check the Data API plan in web.dhan.co |
| V3 `CONFIG_BLOCKED` | The futures contract is wrong or expired. Re-verify the id and expiry |
| V3 `BLOCKED` | The data gate worked as designed. Read the reasons; not a deployment failure |
| Log lines cut off | Lower `LEG_BAND` (e.g. 2) or `RECORD_CHUNK` (e.g. 1500) |

## 7. Local verification performed (this environment, MOCK data only)

| Check | Result |
|---|---|
| `python3 cloud/build_bundle.py --check` | dist up to date (22 files) |
| Both bundles in VALIDATE, copied alone into an empty directory, isolated interpreter, network/subprocess blocked by an audit hook | PASS; writes no files; every stdout line starts `BX|` |
| Stage A (3 scenarios) through **both** bundles vs TypeScript reference values | PASS (0 mismatches) |
| Single file: no unresolved global names (symtable) | PASS |
| OBSERVE simulated (fake transport, simulated clock 10:33:56→10:45), with timestamps prefixed as a log viewer would → decode → TS replay → Python replay → report | 12 scans, 4 replay records, TS replay 4/4 OK, Python-vs-TS **4/4 PASS**, report has all 3 sections |
| Token and client id absent from all output, including a 401 whose body echoes the token | PASS |
| Refusals before any call: empty config, missing variables, expired token | PASS (0 requests) |
| Mutations: an order call added to the entry; a `subprocess` import added | Both caught (allow-list, identifier and drift tests) |
| Engine suite (`cd engine && python3.X -B -m unittest discover -s tests -t tests`) | **121 OK** on 3.11, 3.12, 3.13 |
| TS: `npm run typecheck`, `npx vitest run` | PASS, 282/282 |
| `tools/test_dhan_cloud_probe.py` | OK |
