"""
Dhan Cloud runtime probe — READ-ONLY, stdlib only, no credentials, no orders.

Paste into a Dhan Cloud strategy and run once (on demand, then once on a
schedule). It prints facts the public docs do not state, so they can be
recorded in docs/PHASE2_VERIFICATION.md:

  1. Python version and platform
  2. Wall clock and timezone the container runs in
  3. Whether api.dhan.co and an outside host are reachable (HTTPS)
  4. A heartbeat every 60 s, so the logs show when a scheduled run is
     started and auto-stopped, and whether output streams live

It deliberately does NOT touch os, files, subprocess or env vars: the scanner
reportedly rejects some of those, and a rejected save is itself a finding.
Probe them separately (see the checklist in docs/PHASE2_VERIFICATION.md).

The api.dhan.co call sends NO token; a 4xx answer proves reachability only.
"""

import platform
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_SECONDS = 60
MAX_HEARTBEATS = 15  # stop on our own after ~15 minutes


def reach(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10) as r:
            return "HTTP %s" % r.status
    except urllib.error.HTTPError as e:
        return "HTTP %s (reachable)" % e.code
    except Exception as e:  # DNS, TLS, egress policy
        return "UNREACHABLE: %s: %s" % (type(e).__name__, e)


def main():
    print("PROBE python=%s" % sys.version.replace("\n", " "))
    print("PROBE platform=%s" % platform.platform())
    now_local = datetime.now()
    now_utc = datetime.now(timezone.utc)
    print("PROBE local_clock=%s utc=%s ist=%s" % (
        now_local.isoformat(timespec="seconds"),
        now_utc.isoformat(timespec="seconds"),
        now_utc.astimezone(IST).isoformat(timespec="seconds")))
    print("PROBE api.dhan.co -> %s" % reach("https://api.dhan.co/v2/profile"))
    print("PROBE outside (api.telegram.org) -> %s" % reach("https://api.telegram.org"))
    for i in range(MAX_HEARTBEATS):
        print("PROBE heartbeat=%d ist=%s" % (
            i, datetime.now(timezone.utc).astimezone(IST).isoformat(timespec="seconds")))
        time.sleep(HEARTBEAT_SECONDS)
    print("PROBE done")


if __name__ == "__main__":
    main()
