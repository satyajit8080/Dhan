"""
Dhan Cloud import probe (Phase 6, step P2). Credential-free, no network,
no file writes, stdlib only.

Answers: can a strategy's main file import a SECOND uploaded file?
  PROBE2 sibling_import=OK      -> upload cloud/dist/multi/ (main.py + bx_*.py)
  PROBE2 sibling_import=FAILED  -> upload cloud/dist/single/sensex_observer.py
Also prints whether the script runs as __main__. No OS or filesystem queries
(Dhan Cloud's scanner rejects host platform/OS details).
"""

import sys


def line(key, value):
    print("PROBE2 %s=%s" % (key, value), flush=True)


line("python", "%d.%d.%d" % tuple(sys.version_info[:3]))
line("dunder_name", __name__)
try:
    import probe_helper
    line("sibling_import", "OK" if probe_helper.MARKER == "bx-probe-helper-1" else "WRONG_MODULE")
except ImportError as e:
    line("sibling_import", "FAILED %s" % type(e).__name__)
line("done", True)
