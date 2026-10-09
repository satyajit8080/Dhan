# Phase 3 — Dhan Cloud Verification Checklist (manual)

You run these steps; I could not. This session's network policy blocks every
Dhan domain (`dhanhq.co`, `docs.dhanhq.co`, `developer.dhanhq.co`,
`cloud.dhanhq.co`, `api.dhan.co`, `images.dhan.co`, `dhan.co`, `madefortrade.in`).
**Every result below is `UNVERIFIED` until you paste the output back.**

Ground rules for every step:
- **No orders.** None of the scripts contains order, account-change or login code.
  This is enforced by `tools/test_dhan_cloud_probe.py`.
- **No credentials** are needed for C1–C12. Do not put your token, PIN, TOTP
  secret or client id into any probe or variable during C1–C12.
- **What to copy back:** every line starting with `PROBE `, plus any scanner,
  install or scheduler message *verbatim*. Remove nothing, add nothing. The probes
  never print variable values, and they mask credential-like variable names.

> UI labels below ("strategy", "Env Variables", "Run", "Schedule", "Logs") come
> from Dhan's sample project and community guides. If a label differs in your
> console, note the real label in the "Actual" column.

---

## Part A — Baseline (no credentials)

### C1. Create a probe strategy and check the scanner accepts the BASIC probe
1. Open Dhan Cloud → create a new strategy/project named `probe-basic`.
2. Paste the full contents of `tools/dhan_cloud_probe.py` as the main file
   (`main.py` if the console requires that name).
3. Leave `requirements.txt` empty, or absent if allowed.
4. Save.
- **Copy back:** "saved", or the scanner's full message.

### C2. Python version, clock, timezone (on-demand run)
1. Run `probe-basic` once with **Run now / on-demand**.
2. Open its logs.
- **Copy back:** all `PROBE` lines (`python=`, `platform=`, `local_tz=`,
  `local_is_ist=`, `ist_clock=`, `reach_*`, `heartbeat=`, `done=`).

### C3. Do logs stream live?
While C2 runs, refresh the log view after ~2 minutes.
- **Copy back:** whether `heartbeat=1`, `heartbeat=2`… appear progressively, or
  only all at once at the end.

### C4. Outbound network, without credentials
From C2's output: `reach_dhan_api`, `reach_dhan_instrument_cdn`,
`reach_outside_telegram`. Any `HTTP nnn` means the host answered
(reachability, not authorisation). `UNREACHABLE …` means blocked or no DNS.

## Part B — Scanner rules, environment, filesystem, persistence

### C5. Does the scanner accept the EXTENDED probe?
Create a second strategy `probe-extended` with `tools/dhan_cloud_probe_extended.py`,
then save. It uses `os`, file writes and environment reads, which the community
reports the scanner may reject.
- **Copy back:** "saved", or the scanner's full message. **A rejection is a valid
  result:** it tells us which constructs are banned. If rejected, skip to C9.

### C6. Environment variables, without exposing anything
1. In `probe-extended` → **Env Variables**, add exactly one variable:
   name `PROBE_MARKER`, value `hello12345` (10 characters; not a secret).
2. Run once.
- **Copy back:** `env_count=`, `env_marker_present=`, `env_marker_length=`.
  Expected `True` and `10`. `env_names=` lists names only; credential-like names
  appear as `<masked-name>`. Also note whether the console stores the value
  encrypted or masked, and whether it shows the `{{VAR}}` syntax RULES.md mentions.

### C7. Filesystem write permission
From the same run: `write_cwd=`, `write_tmp=`, `write_home=`
(`WRITABLE` or `NOT_WRITABLE <Error>`).

### C8. Persistence across SEPARATE runs
1. Run `probe-extended` a **second** time, as a new run (not a replay of the
   first). Wait at least 1 minute.
- **Copy back:** `persist_cwd=`, `persist_tmp=`, `persist_home=`.
  `previous=<the first run's run_id>` means files persisted.
  `previous=None` means an ephemeral filesystem.
2. Optional: read the console's **Global Variables** help and record whether
   strategies can write to them.

## Part C — Packages

### C9. Package installation
Use `probe-extended` (or, if C5 was rejected, `probe-basic`, whose log still
shows the install step). Try these `requirements.txt` contents, one per save and
run. The versions are real PyPI releases:

| Try | requirements.txt |
|---|---|
| C9a | *(empty)* |
| C9b | `requests==2.32.3` |
| C9c | `pyotp==2.9.0` |
| C9d | `dhanhq==2.3.0` |

- **Copy back:** for each, the scanner/save result, the install log lines
  (including any platform-injected pins such as `numpy==…`), and the
  `PROBE pkg_*` lines (extended probe only).

## Part D — Scheduling and automatic stop

### C10. Scheduled start
Schedule `probe-basic` for the next weekday at a time you choose (e.g. 10:00
IST), with no stop time or a stop time ≥ 20 minutes later.
- **Copy back:** the schedule settings exactly as shown, and the first `PROBE`
  line's `ist_clock`. It should be within a minute or two of the scheduled time.

### C11. Automatic stop at a session boundary
Schedule `probe-basic` to start **15:20 IST** with **auto-stop 15:30 IST** (or the
nearest the console allows). The probe would otherwise run ~15 minutes.
- **Copy back:** the last `heartbeat=` line. Expected about 15:29–15:30 and
  **no** `PROBE done=` line, which means the platform stopped it. Note what the
  log says about the stop (exit code or message).

### C12. Holiday behaviour
Read the schedule options. Is there an exchange-holiday calendar, or only
weekdays?
- **Copy back:** the options shown.

## Part E — Read-only API connectivity WITH a token (do last, optional)

Do this **only** after C6 has shown how variables are stored, and only if you
are comfortable putting today's access token in a Dhan Cloud variable. It
performs one read-only `GET /v2/profile` and prints **only** the HTTP status and
two yes/no facts. Never paste the token into the code itself.

### C13. Profile check (read-only)
Use the mechanism C6 confirmed. Example if `os.environ` works:

```python
import json, os, urllib.request
req = urllib.request.Request("https://api.dhan.co/v2/profile",
    headers={"access-token": os.environ["DHAN_TOKEN"], "dhanClientId": os.environ["DHAN_CLIENT"]})  # headers as in DhanHQ-py DhanLogin.user_profile
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read() or b"{}")
        print("PROFILE status=%s dataPlan_present=%s tokenValidity_present=%s"
              % (r.status, "dataPlan" in body, "tokenValidity" in body))
except Exception as e:
    print("PROFILE error=%s" % type(e).__name__)  # never print e: it may echo headers
```
- **Copy back:** the single `PROFILE` line. Delete the token variable afterwards.

## Part F — Console facts

### C14. Cost
Developer Portal → cost estimator: 1 vCPU, Mon–Fri 09:15–15:30 IST.
- **Copy back:** the estimate and its units.

### C15. Code updates
Look for any "Git", "Import from repository", "Deploy API/CLI" or version
history on the strategy page.
- **Copy back:** what exists.

### C16. Instrument list header (local machine, not Dhan Cloud)
On your own computer: `curl -s https://images.dhan.co/api-data/api-scrip-master.csv | head -2`
- **Copy back:** the two lines. This is public data with no credentials. It
  fixes the real column names and the expiry-date format.

---

## Results table (fill the "Actual" column; statuses: PASS / FAIL / UNVERIFIED / BLOCKED)

| ID | Test | Expected | Actual | Status |
|---|---|---|---|---|
| C1 | Basic probe accepted by scanner | Saves | — | UNVERIFIED |
| C2 | Python version | 3.11.x (community report) | — | UNVERIFIED |
| C2 | Container timezone | Unknown; probe prints `local_is_ist` | — | UNVERIFIED |
| C3 | Live log streaming | Heartbeats appear progressively | — | UNVERIFIED |
| C4 | Reach `api.dhan.co` | `HTTP nnn` | — | UNVERIFIED |
| C4 | Reach `images.dhan.co` | `HTTP 200` | — | UNVERIFIED |
| C4 | Reach outside host (Telegram) | Unknown | — | UNVERIFIED |
| C5 | Extended probe accepted by scanner | Unknown (reported risk) | — | UNVERIFIED |
| C6 | Env variable readable (presence/length only) | `True`, `10` | — | UNVERIFIED |
| C7 | Filesystem writable (cwd/tmp/home) | Unknown | — | UNVERIFIED |
| C8 | Files persist between runs | Likely not (community inference) | — | UNVERIFIED |
| C9a–d | Packages installable | `requests` likely; others unknown | — | UNVERIFIED |
| C10 | Scheduled start on time | Within ~2 min | — | UNVERIFIED |
| C11 | Auto-stop at 15:30 IST | Last heartbeat ≈ 15:29–15:30, no `done` | — | UNVERIFIED |
| C12 | Holiday calendar | Unknown | — | UNVERIFIED |
| C13 | Read-only `/profile` | `status=200 dataPlan_present=True` | — | UNVERIFIED (optional) |
| C14 | Cost estimate | Unknown | — | UNVERIFIED |
| C15 | Git / deploy API | None found in research | — | UNVERIFIED |
| C16 | Instrument CSV header | `SEM_*` columns (official tooling) | — | UNVERIFIED |

## What each probe can and cannot establish

| Capability | Basic probe | Extended probe | Needs a manual step |
|---|---|---|---|
| Python version | Yes | Yes | — |
| Clock / timezone | Yes | — | — |
| Outbound reachability (no credentials) | Yes (HEAD) | No | — |
| Live log streaming | Yes (heartbeats) | — | Watch the log (C3) |
| Scanner rules | Partly (if it saves) | Partly (if it saves) | Copy the scanner message (C1, C5) |
| Env variables | No | Presence + length | Create `PROBE_MARKER` (C6) |
| Filesystem write | No | Yes | — |
| Persistence | No | Yes, across **two separate runs** | Run twice (C8) |
| Package install | No | Reports what is importable | Edit requirements.txt; read install log (C9) |
| Scheduling / auto-stop | Timestamps only | — | Configure schedule (C10, C11) |
| Authenticated API | No | No | Optional C13 |
