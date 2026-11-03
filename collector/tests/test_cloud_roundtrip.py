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
from pathlib import Path

from collector import decode_cloud_log
from collector.tests.test_collector import GAP_DAY, HOLIDAY, TOKEN, FakeDhan

CLOUD = Path(__file__).resolve().parents[2] / "cloud" / "stock_options_history_cloud.py"
MASTER = (b"SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
          b"SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
          b"SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME\n"
          b"NSE,E,2885,EQUITY,,RELIANCE,1,Reliance,,,,0.1,,ES,EQ,RELIANCE\n"
          b"NSE,D,90001,OPTSTK,0,RELIANCE-Nov2099-1500-CE,500,R,2099-11-24 14:30:00,1500,CE,0.05,M,OP,,R\n"
          b"NSE,E,7777,EQUITY,,NOTFNO,1,N,,,,0.1,,ES,EQ,NOTFNO\n")


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
    mod.SYMBOLS = ["RELIANCE", "NOTFNO"]
    mod.REQUESTS_PER_SECOND = 1000
    return mod


class CloudRoundTrip(unittest.TestCase):
    def test_roundtrip(self):
        import urllib.request as ur
        orig = ur.urlopen
        try:
            mod = load_cloud(FakeDhan(fail_first=[429]))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
        finally:
            ur.urlopen = orig
        logtext = buf.getvalue()
        self.assertEqual(rc, 0, logtext[-2000:])
        self.assertNotIn(TOKEN, logtext)
        self.assertNotIn("1100000000", logtext)

        with tempfile.TemporaryDirectory() as d:
            logf = Path(d) / "run.log"
            # imitate the Dhan Cloud log viewer prefix
            logf.write_text("".join(f"›[2026-11-03 14:45:21 IST] {l}\n" for l in logtext.splitlines()))
            data = Path(d) / "data"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(decode_cloud_log.main([str(logf), "--data-dir", str(data)]), 0)
            out = data / "exports" / "2026-03-01_2026-04-15"
            rows = list(csv.DictReader((out / "all_symbols.csv").open(encoding="utf-8")))
            self.assertEqual(len(rows), 31 * 2 * 7)
            self.assertTrue(all(r["lot_size"] == "500" for r in rows))
            self.assertIn("09:29", {r["time"] for r in rows if r["date"] == GAP_DAY.isoformat()})
            self.assertFalse([r for r in rows if r["date"] == HOLIDAY.isoformat()])
            report = (out / "validation_report.md").read_text(encoding="utf-8")
            self.assertIn("NOT_IN_FNO", report)
            self.assertTrue((data / "exports" / "bull50_stock_options_2026-03-01_2026-04-15.zip").exists())

    def test_cloud_source_avoids_blocked_patterns(self):
        src = CLOUD.read_text(encoding="utf-8")
        for pat in (r"^\s*import os\b", r"\bpathlib\b", r"os\.environ", r"sys\.exit\(", r"\bopen\(",
                    r"subprocess", r"boto", r"place_order", r"getenv"):
            self.assertIsNone(re.search(pat, src, re.M), pat)
        self.assertIn('CLIENT_ID = "{{CLIENT_ID}}"', src)
        self.assertIn('ACCESS_TOKEN = "{{ACCESS_TOKEN}}"', src)


if __name__ == "__main__":
    unittest.main()
