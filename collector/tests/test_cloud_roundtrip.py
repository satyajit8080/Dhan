"""Cloud script -> log -> decoder round trip, offline, with SYNTHETIC data."""
from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import re
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path

from collector import decode_cloud_log
from collector.tests.test_collector import GAP_DAY, HOLIDAY, TOKEN, FakeDhan

CLOUD = Path(__file__).resolve().parents[2] / "cloud" / "stock_options_history_cloud.py"
MASTER = (b"SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
          b"SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
          b"SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME\n"
          b"NSE,E,2885,EQUITY,,RELIANCE,1,Reliance,,,,0.1,,ES,EQ,RELIANCE\n"
          b"NSE,D,90001,OPTSTK,0,RELIANCE-Nov2099-1500-CE,500,R,2099-11-24 14:30:00,1500,CE,0.05,M,OP,,R\n"
          b"NSE,E,7777,EQUITY,,NOTFNO,1,N,,,,0.1,,ES,EQ,NOTFNO\n"
          b"NSE,E,16669,EQUITY,,BAJAJ-AUTO,1,B,,,,0.1,,ES,EQ,BAJAJ-AUTO\n"
          b"NSE,D,90009,OPTSTK,0,BAJAJ-AUTO-Nov2099-9000-CE,75,B,2099-11-24 14:30:00,9000,CE,0.05,M,OP,,B\n")


class StrictDhan(FakeDhan):
    """Mimics the real cloud runs: HTTP 400 DH-905 for a string interval or a window longer
    than max_days (or for everything with reject_all)."""

    def __init__(self, reject_all=False, max_days=1, **kw):
        super().__init__(**kw)
        self.reject_all, self.max_days = reject_all, max_days
        self.bodies = []

    def __call__(self, url, body, headers, timeout):
        import json as _j
        from datetime import date as _d
        req = _j.loads(body)
        self.bodies.append(req)
        span = (_d.fromisoformat(req["toDate"]) - _d.fromisoformat(req["fromDate"])).days
        if self.reject_all or isinstance(req.get("interval"), str) or span > self.max_days:
            self.calls += 1
            return 400, b'{"errorType":"Input_Exception","errorCode":"DH-905","errorMessage":"Missing required fields, bad values for parameters etc."}'
        return super().__call__(url, body, headers, timeout)


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, body

    def read(self):
        return self._b

    def __iter__(self):
        return iter(self._b.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def load_cloud(fake):
    spec = importlib.util.spec_from_file_location("cloud_mod", CLOUD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def urlopen(req, timeout=None):
        if req.full_url.endswith(".csv"):
            return _Resp(200, MASTER)
        status, body = fake(req.full_url, req.data, {"access-token": req.headers["Access-token"]}, timeout)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
        return _Resp(status, body)

    mod.urllib.request.urlopen = urlopen
    mod.time.sleep = lambda s: None
    mod.CLIENT_ID, mod.ACCESS_TOKEN = "1100000000", TOKEN
    mod.FROM_DATE, mod.TO_DATE = "2026-03-01", "2026-04-15"
    mod.SYMBOLS = ["RELIANCE", "NOTFNO", "BAJAJ-AUTO"]
    mod.REQUESTS_PER_SECOND = 1000
    return mod


class CloudRoundTrip(unittest.TestCase):
    def test_roundtrip(self):
        import urllib.request as ur
        orig = ur.urlopen
        try:
            fake = StrictDhan(fail_first=[429])
            mod = load_cloud(fake)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            ur.urlopen = orig
        logtext = buf.getvalue()
        self.assertEqual(rc, 0, logtext[-2000:])
        self.assertNotIn(TOKEN, logtext)
        self.assertNotIn("1100000000", logtext)
        self.assertIn('"event": "probe_selected", "format": "2-day/step-1/toDate-inclusive/secId-str/'
                      'open,high,low,close,iv,volume,strike,oi,spot"', logtext)
        self.assertTrue(all(isinstance(b["interval"], int) for b in fake.bodies))
        run = [b for b in fake.bodies if b["exchangeSegment"] == "NSE_FNO"][-50:]
        self.assertTrue(all((date.fromisoformat(b["toDate"]) - date.fromisoformat(b["fromDate"])).days <= 1
                            for b in run))
        self.assertNotIn("BAJAJ-AUTO (NOT_IN_FNO)", logtext)

        with tempfile.TemporaryDirectory() as d:
            logf = Path(d) / "run.log"
            # imitate the Dhan Cloud log viewer prefix
            logf.write_text("".join(f"›[2026-11-03 14:45:21 IST] {l}\n" for l in logtext.splitlines()))
            data = Path(d) / "data"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(decode_cloud_log.main([str(logf), "--data-dir", str(data)]), 0)
            out = data / "exports" / "2026-03-01_2026-04-15"
            rows = list(csv.DictReader((out / "all_symbols.csv").open(encoding="utf-8")))
            self.assertEqual(len([r for r in rows if r["symbol"] == "RELIANCE"]), 31 * 2 * 7)
            self.assertEqual({r["lot_size"] for r in rows if r["symbol"] == "BAJAJ-AUTO"}, {"75"})
            self.assertTrue(all(r["lot_size"] == "500" for r in rows if r["symbol"] == "RELIANCE"))
            self.assertIn("09:29", {r["time"] for r in rows if r["date"] == GAP_DAY.isoformat()})
            self.assertFalse([r for r in rows if r["date"] == HOLIDAY.isoformat()])
            report = (out / "validation_report.md").read_text(encoding="utf-8")
            self.assertIn("NOT_IN_FNO", report)
            self.assertTrue((data / "exports" / "bull50_stock_options_2026-03-01_2026-04-15.zip").exists())

    def test_30_day_windows_when_accepted(self):
        import urllib.request as ur
        orig = ur.urlopen
        try:
            fake = StrictDhan(max_days=30)
            mod = load_cloud(fake)
            mod.SYMBOLS = ["RELIANCE"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            ur.urlopen = orig
        self.assertEqual(rc, 0)
        self.assertIn('"format": "30-day/step-30/toDate-exclusive/', buf.getvalue())
        with tempfile.TemporaryDirectory() as d:
            logf = Path(d) / "run.log"
            logf.write_text(buf.getvalue())
            data = Path(d) / "data"
            with contextlib.redirect_stdout(io.StringIO()):
                decode_cloud_log.main([str(logf), "--data-dir", str(data)])
            rows = list(csv.DictReader((data / "exports" / "2026-03-01_2026-04-15" / "all_symbols.csv")
                                       .open(encoding="utf-8")))
            self.assertEqual(len(rows), 31 * 2 * 7)

    def test_stops_when_every_format_is_refused(self):
        import urllib.request as ur
        orig = ur.urlopen
        try:
            fake = StrictDhan(reject_all=True)
            mod = load_cloud(fake)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            ur.urlopen = orig
        self.assertEqual(rc, 2)
        expected = 1 + len(mod.PROBE_DAYS) * 2 + 2 * 2         # control + stage B, nothing else
        self.assertEqual(fake.calls, expected)
        self.assertIn("Missing required fields", buf.getvalue())

    def test_cloud_source_avoids_blocked_patterns(self):
        src = CLOUD.read_text(encoding="utf-8")
        for pat in (r"^\s*import os\b", r"\bpathlib\b", r"os\.environ", r"sys\.exit\(", r"\bopen\(",
                    r"subprocess", r"boto", r"place_order", r"getenv", r"\bcode\b", r"\bhttp\b"):
            self.assertIsNone(re.search(pat, src, re.M), pat)
        # Dhan Cloud's scanner reports a bare name equal to a stdlib module (e.g. `code`)
        # as a "blocked import". Only real imports may use module names.
        import ast
        import sys
        tree = ast.parse(src)
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                    for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names = set()
        for n in ast.walk(tree):
            for attr in ("id", "arg", "attr", "name"):
                v = getattr(n, attr, None)
                if isinstance(v, str):
                    names.add(v)
        stdlib = getattr(sys, "stdlib_module_names", set())
        self.assertEqual(sorted((names & set(stdlib)) - imported), [])
        self.assertIn('CLIENT_ID = "{{CLIENT_ID}}"', src)
        self.assertIn('ACCESS_TOKEN = "{{ACCESS_TOKEN}}"', src)


if __name__ == "__main__":
    unittest.main()
