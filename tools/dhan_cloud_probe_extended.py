"""
Dhan Cloud runtime probe (EXTENDED) — read-only, stdlib only, no credentials.

Run AFTER dhan_cloud_probe.py. It touches `os`, the filesystem and the
environment, which Dhan Cloud's scanner is REPORTED to flag. If the save is
rejected, copy the scanner's exact message: that is the result for those
checks (see docs/PHASE3_CLOUD_CHECKLIST.md, tests C5-C8).

What it reports:
  * environment-variable NAMES only (never values); for the one variable you
    create for the test (PROBE_MARKER) it reports presence and LENGTH only
  * whether each candidate directory is writable (writes, reads back, deletes
    a tiny file it owns)
  * persistence: leaves one marker file per writable directory and, on the
    NEXT separate run, reports whether a previous run's marker survived
  * whether optional packages are importable, and their versions

It cannot install packages (that is the platform's requirements step) and it
cannot prove a schedule ran (the platform log timestamps do that).

Safety: no network calls at all, no Dhan credentials, never prints a variable's
value, only deletes files it created itself (names start with PROBE_FILE_PREFIX).
"""

import importlib.metadata
import importlib.util
import os
import re
import sys
import tempfile
import time

PROBE_VERSION = "3"
MARKER_VAR = "PROBE_MARKER"
PROBE_FILE_PREFIX = ".bull50_probe_"
PERSIST_FILE = PROBE_FILE_PREFIX + "persist"
OPTIONAL_PACKAGES = ["requests", "pyotp", "dhanhq", "pandas", "numpy", "websockets"]

# Variable names that look credential-like are reported as present but with
# the name itself masked, so even a name like CLIENT_1100xxxx is not printed.
_SENSITIVE_NAME = re.compile(r"(TOKEN|SECRET|PASS|PIN|TOTP|KEY|CLIENT|AUTH|CRED|\d{5,})", re.I)


def line(key, value):
    print("PROBE %s=%s" % (key, value), flush=True)


def env_report(environ=None):
    """Names only. Never values. Credential-like names are masked."""
    environ = os.environ if environ is None else environ
    names = sorted(environ.keys())
    shown = [n if not _SENSITIVE_NAME.search(n) else "<masked-name>" for n in names]
    marker = environ.get(MARKER_VAR)
    return {
        "env_count": len(names),
        "env_names": ",".join(shown),
        "env_marker_present": marker is not None,
        "env_marker_length": len(marker) if marker is not None else 0,
    }


def candidate_dirs():
    dirs = []
    for d in (os.getcwd(), tempfile.gettempdir(), os.path.expanduser("~")):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def write_check(directory, run_id):
    """Write, read back and delete one tiny file. Returns a status string."""
    path = os.path.join(directory, PROBE_FILE_PREFIX + run_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(run_id)
        with open(path, "r", encoding="utf-8") as f:
            ok = f.read() == run_id
        os.remove(path)
        return "WRITABLE" if ok else "READBACK_MISMATCH"
    except Exception as e:
        return "NOT_WRITABLE %s" % type(e).__name__


def persistence_check(directory, run_id):
    """Report the previous run's marker (if any), then leave ours."""
    path = os.path.join(directory, PERSIST_FILE)
    previous = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            previous = f.read().strip()[:64]
    except FileNotFoundError:
        previous = None
    except Exception as e:
        return "READ_ERROR %s" % type(e).__name__
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("%s %d" % (run_id, int(time.time())))
    except Exception as e:
        return "previous=%s; cannot leave marker (%s)" % (previous, type(e).__name__)
    return "previous=%s; marker_left=%s" % (previous, run_id)


def package_report(packages=OPTIONAL_PACKAGES):
    out = {}
    for name in packages:
        if importlib.util.find_spec(name) is None:
            out[name] = "NOT_INSTALLED"
            continue
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "importable (version unknown)"
    return out


def main():
    run_id = "%x" % int(time.time() * 1000)
    line("probe_version", PROBE_VERSION)
    line("run_id", run_id)
    line("python", sys.version.split()[0])
    line("cpu_count", os.cpu_count())
    for k, v in env_report().items():
        line(k, v)
    for d in candidate_dirs():
        label = "cwd" if d == os.getcwd() else ("tmp" if d == tempfile.gettempdir() else "home")
        line("write_" + label, write_check(d, run_id))
        line("persist_" + label, persistence_check(d, run_id))
    for name, status in package_report().items():
        line("pkg_" + name, status)
    line("done", run_id)


if __name__ == "__main__":
    main()
