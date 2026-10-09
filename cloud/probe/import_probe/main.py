"""
Dhan Cloud import probe (Phase 6, step P2). Credential-free, no network,
no file writes, stdlib only.

Answers: can a strategy's main file import a SECOND uploaded file?
  PROBE2 sibling_import=OK      -> upload cloud/dist/multi/ (main.py + bx_*.py)
  PROBE2 sibling_import=FAILED  -> upload cloud/dist/single/sensex_observer.py
Also prints how the runtime starts the script (name, file, working dir).
"""

import os
import sys


def line(key, value):
    print("PROBE2 %s=%s" % (key, value), flush=True)


line("python", sys.version.split()[0])
line("dunder_name", __name__)
line("script_file", os.path.basename(globals().get("__file__", "") or "<none>"))
line("script_dir_listing", sorted(n for n in os.listdir(os.path.dirname(os.path.abspath(globals().get("__file__", ".")))) if n.endswith(".py"))[:20])
try:
    import probe_helper
    line("sibling_import", "OK" if probe_helper.MARKER == "bx-probe-helper-1" else "WRONG_MODULE")
except ImportError as e:
    line("sibling_import", "FAILED %s" % type(e).__name__)
line("done", True)
