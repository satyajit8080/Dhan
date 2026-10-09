"""
Dhan Cloud runtime probe (BASIC) — read-only, stdlib only, no credentials.

Run this first. It uses only platform, sys, time, datetime and urllib, and
touches no files and no environment settings, so that a strict code scanner
(REPORTED by the community, unverified) has as little as possible to object
to. If it saves and runs, the runtime baseline is known; then run
`dhan_cloud_probe_extended.py` for environment, filesystem, persistence and
package checks.

What it establishes on its own:
  * Python version / implementation / platform           -> yes
  * container clock, timezone, offset from IST           -> yes
  * HTTPS reachability of Dhan hosts and one outside host -> yes (HEAD only)
  * whether log lines stream while the job runs          -> yes (heartbeat)
What it can NOT establish on its own (needs the manual steps in
docs/PHASE3_CLOUD_CHECKLIST.md): package installation, persistence, scheduling
and auto-stop (it only timestamps them), env-variable access.

Safety: sends HEAD requests with no headers, no body, no token, to public URLs.
It never calls an order, account or authentication endpoint.
"""

import platform
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PROBE_VERSION = "3"
IST = timezone(timedelta(hours=5, minutes=30))
TIMEOUT_SECONDS = 10
HEARTBEAT_SECONDS = 60
MAX_HEARTBEATS = 15  # ~15 min, then the probe stops on its own

# Public URLs only. HEAD = no response body is downloaded.
REACH_TARGETS = [
    ("dhan_api", "https://api.dhan.co/v2/"),
    ("dhan_instrument_cdn", "https://images.dhan.co/api-data/api-scrip-master.csv"),
    ("outside_telegram", "https://api.telegram.org/"),
]


def line(key, value):
    print("PROBE %s=%s" % (key, value), flush=True)


def reach(url, timeout=TIMEOUT_SECONDS):
    """HTTP status if the host answered (any status proves reachability),
    else the error class. Never raises."""
    req = urllib.request.Request(url, method="HEAD")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        status = "HTTP %s" % e.code
    except Exception as e:  # DNS, TLS, egress policy, timeout
        status = "UNREACHABLE %s" % type(e).__name__
    return "%s in %.0f ms" % (status, (time.monotonic() - started) * 1000)


def clock_report():
    now = datetime.now()
    aware = now.astimezone()
    utc = datetime.now(timezone.utc)
    offset = aware.utcoffset() or timedelta(0)
    return {
        "local_clock": now.isoformat(timespec="seconds"),
        "local_tz": "%s (UTC%+.2fh)" % (aware.tzname(), offset.total_seconds() / 3600),
        "utc_clock": utc.isoformat(timespec="seconds"),
        "ist_clock": utc.astimezone(IST).isoformat(timespec="seconds"),
        "local_is_ist": offset == timedelta(hours=5, minutes=30),
    }


def runtime_report():
    return {
        "probe_version": PROBE_VERSION,
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable_name": sys.executable.rsplit("/", 1)[-1] if sys.executable else "",
        "script_name": (sys.argv[0].rsplit("/", 1)[-1] if sys.argv and sys.argv[0] else ""),
    }


def main(heartbeats=MAX_HEARTBEATS, heartbeat_seconds=HEARTBEAT_SECONDS):
    run_id = "%x" % int(time.time() * 1000)
    line("run_id", run_id)
    for k, v in runtime_report().items():
        line(k, v)
    for k, v in clock_report().items():
        line(k, v)
    for name, url in REACH_TARGETS:
        line("reach_" + name, reach(url))
    for i in range(heartbeats):
        line("heartbeat", "%d ist=%s" % (i, datetime.now(timezone.utc).astimezone(IST).isoformat(timespec="seconds")))
        if i < heartbeats - 1:
            time.sleep(heartbeat_seconds)
    line("done", run_id)


if __name__ == "__main__":
    main()
