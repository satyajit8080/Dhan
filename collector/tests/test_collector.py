"""Offline tests. A fake Dhan transport serves SYNTHETIC bars; nothing here is real market data."""
from __future__ import annotations

import csv
import gzip
import json
import math
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from collector import collect, config
from collector.client import AuthError, DhanClient
from collector.credentials import Credentials, load_credentials, redact
from collector.expiry import ExpiryMapper, rule_expiry

HOLIDAY = date(2026, 3, 31)        # a Tuesday that is a month's rule expiry
GAP_DAY = date(2026, 3, 10)        # 09:30 bar missing on this day
NULL_IV_DAY = date(2026, 3, 12)    # PE IV missing at ATM on this day
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0dXNlciJ9.c2lnbmF0dXJlLXRlc3Q"


def synthetic_expiry(d: date) -> date:
    m = ExpiryMapper(holidays={HOLIDAY})
    return m.expiry_for(d)


class FakeDhan:
    """Synthetic rolling-option server with injected faults."""

    def __init__(self, fail_first=(), auth_fail=False):
        self.calls = 0
        self.fail_first = list(fail_first)    # HTTP codes returned for the first calls
        self.auth_fail = auth_fail

    def __call__(self, url, body, headers, timeout):
        self.calls += 1
        assert url.endswith("/charts/rollingoption")
        assert headers["access-token"] == TOKEN
        if self.auth_fail:
            return 401, b'{"errorCode":"DH-901","errorMessage":"invalid token"}'
        if self.fail_first:
            return self.fail_first.pop(0), b'{"errorCode":"DH-904"}'
        req = json.loads(body)
        off = 0 if req["strike"] == "ATM" else int(req["strike"][3:])
        call = req["drvOptionType"] == "CALL"
        d, end = date.fromisoformat(req["fromDate"]), date.fromisoformat(req["toDate"])
        ts, close, oi, iv, vol, strike, spot = [], [], [], [], [], [], []
        while d < end:
            if d.weekday() < 5 and d != HOLIDAY:
                exp = synthetic_expiry(d)
                for minute in range(9 * 60 + 15, 15 * 60 + 30):
                    if d == GAP_DAY and minute == 9 * 60 + 30:
                        continue
                    t = datetime(d.year, d.month, d.day, minute // 60, minute % 60,
                                 tzinfo=config.IST)
                    s = 1000 + 5 * math.sin(minute / 30) + d.day
                    k = round(s / 20) * 20 + 20 * off
                    days_left = (exp - d).days + (15 * 60 + 30 - minute) / 375
                    tv = 0.02 * s * math.sqrt(max(days_left, 0) / 30)
                    intrinsic = max(s - k, 0) if call else max(k - s, 0)
                    ts.append(int(t.timestamp()))
                    close.append(round(intrinsic + tv, 2))
                    oi.append(1000 + minute)
                    iv.append(None if (d == NULL_IV_DAY and not call and off == 0) else 22.5)
                    vol.append(10)
                    strike.append(k)
                    spot.append(round(s, 2))
            d += timedelta(days=1)
        if not ts:
            return 400, b'{"errorCode":"DH-907","errorMessage":"no data"}'
        side = {"timestamp": ts, "close": close, "oi": oi, "iv": iv, "volume": vol,
                "strike": strike, "spot": spot, "open": close, "high": close, "low": close}
        data = {"ce": side, "pe": None} if call else {"ce": None, "pe": side}
        return 200, json.dumps({"data": data}).encode()


MASTER = """EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE
NSE,E,2885,INE002A01018,EQUITY,2885,RELIANCE,RELIANCE,Reliance,ES,EQ,1,,,
NSE,D,90001,,OPTSTK,2885,RELIANCE,RELIANCE-Nov2026-1500-CE,RELIANCE 1500 CE,OP,,500,2026-11-24 14:30:00,1500,CE
NSE,D,90002,,OPTSTK,2885,RELIANCE,RELIANCE-Dec2026-1500-CE,RELIANCE 1500 CE,OP,,500,2026-12-29 14:30:00,1500,CE
NSE,E,1594,INE009A01021,EQUITY,1594,INFY,INFY,Infosys,ES,EQ,1,,,
NSE,E,7777,INE000000000,EQUITY,7777,NOTFNO,NOTFNO,NoFno,ES,EQ,1,,,
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "master.csv").write_text(MASTER)
        self.args = ["--from", "2026-03-01", "--to", "2026-04-15", "--symbols", "RELIANCE,NOTFNO",
                     "--data-dir", str(self.dir / "data"), "--master", str(self.dir / "master.csv"),
                     "--rps", "0"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, fake, *extra):
        orig = collect.DhanClient

        def factory(creds, **kw):
            return orig(creds, transport=fake, sleep=lambda s: None, **kw)

        collect.DhanClient = factory
        collect.load_credentials = lambda: Credentials(TOKEN, "1100000000")
        try:
            return collect.main(self.args + list(extra))
        finally:
            collect.DhanClient = orig


class EndToEnd(Base):
    def test_full_run_resume_and_outputs(self):
        fake = FakeDhan(fail_first=[429, 500])
        self.assertEqual(self.run_with(fake), 0)
        n_windows = 2
        self.assertEqual(fake.calls, n_windows * 14 + 2)      # +2 retried faults

        out = self.dir / "data" / "exports" / "2026-03-01_2026-04-15"
        rows = list(csv.DictReader((out / "all_symbols.csv").open()))
        self.assertEqual(list(rows[0].keys()), config.CSV_COLUMNS)
        self.assertTrue(rows)
        self.assertTrue(all(r["symbol"] == "RELIANCE" and r["lot_size"] == "500" for r in rows))
        keys = [(r["date"], r["time"], r["strike"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "duplicates in export")
        times = {r["time"] for r in rows}
        self.assertEqual(times, {"09:30", "15:20"})
        # gap day: no 09:30 bar -> as-of uses the 09:29 trade; row still labelled 09:30
        gap = [r for r in rows if r["date"] == GAP_DAY.isoformat() and r["time"] == "09:30"]
        self.assertTrue(gap and all(r["ce_ltp"] != "" and r["pe_ltp"] != "" for r in gap))
        self.assertEqual(len([r for r in rows if r["date"] == "2026-03-02"]), 14)
        # missing PE IV stays blank (never fabricated)
        nulls = [r for r in rows if r["date"] == NULL_IV_DAY.isoformat() and r["pe_iv"] == ""]
        self.assertTrue(nulls)
        self.assertTrue(all(r["pe_ltp"] != "" for r in nulls))
        # holiday: no rows, March expiry moved back to Monday 30th
        self.assertFalse([r for r in rows if r["date"] == HOLIDAY.isoformat()])
        self.assertEqual({r["expiry"] for r in rows if r["date"] == "2026-03-27"}, {"2026-03-30"})
        self.assertEqual({r["expiry"] for r in rows if r["date"] == "2026-04-01"}, {"2026-04-28"})

        missing = (out / "missing_data.csv").read_text()
        self.assertIn("PE_IV_MISSING", missing)
        report = (out / "validation_report.md").read_text()
        self.assertIn("NOT_IN_FNO", report)
        self.assertIn("| Legs priced from a trade before the snapshot minute | 14 /", report)
        self.assertIn("oldest 1 min", report)
        self.assertIn("Moved for holidays: 2026-03-31→2026-03-30", report)
        summary = json.loads((out / "summary.json").read_text())
        self.assertLess(summary["median_expiry_straddle_pct"], config.EXPIRY_STRADDLE_MAX_PCT)
        self.assertEqual(summary["pending_requests"], 0)

        zpath = self.dir / "data" / "exports" / "bull50_stock_options_2026-03-01_2026-04-15.zip"
        names = zipfile.ZipFile(zpath).namelist()
        for n in ("all_symbols.csv", "csv/RELIANCE.csv", "validation_report.md",
                  "missing_data.csv", "summary.json", "instruments.csv"):
            self.assertIn(n, names)

        raw = list((self.dir / "data" / "raw" / "RELIANCE").rglob("*.json.gz"))
        self.assertEqual(len(raw), n_windows * 14)
        rec = json.load(gzip.open(raw[0], "rt"))
        self.assertIn("request", rec)
        self.assertIn("response", rec)

        # resume: second run makes no API calls
        fake2 = FakeDhan()
        self.assertEqual(self.run_with(fake2), 0)
        self.assertEqual(fake2.calls, 0)

        # token never written anywhere on disk
        for p in (self.dir / "data").rglob("*"):
            if p.is_file():
                blob = gzip.open(p).read() if p.suffix == ".gz" else p.read_bytes()
                self.assertNotIn(TOKEN.encode(), blob, p)

    def test_auth_error_stops_and_keeps_progress(self):
        self.assertEqual(self.run_with(FakeDhan(auth_fail=True)), 1)
        self.assertFalse(list((self.dir / "data" / "raw").rglob("*.json.gz")))

    def test_failed_requests_are_reported_and_retried(self):
        fake = FakeDhan(fail_first=[503] * config.MAX_RETRIES)   # first request exhausts retries
        self.assertEqual(self.run_with(fake), 0)
        out = self.dir / "data" / "exports" / "2026-03-01_2026-04-15"
        self.assertIn("REQUEST_NOT_FETCHED", (out / "missing_data.csv").read_text())
        fake2 = FakeDhan()
        self.run_with(fake2)
        self.assertEqual(fake2.calls, 1)


class AsOf(unittest.TestCase):
    """Stock options trade sparsely: the 09:30 value is each leg's last trade <= 09:30."""

    def _rec(self, side, strike_label, bars):
        ts = [int(datetime(2026, 3, 2, h, m, tzinfo=config.IST).timestamp()) for h, m, _ in bars]
        leg = {"timestamp": ts, "close": [c for _, _, c in bars], "oi": [1] * len(bars),
               "iv": [20] * len(bars), "volume": [1] * len(bars), "strike": [1000] * len(bars),
               "spot": [1001] * len(bars)}
        return {"status": "ok", "request": {"strike": strike_label},
                "response": {"data": {side: leg}}}

    def test_last_trade_at_or_before_snapshot(self):
        from collector.assemble import build_symbol
        recs = [self._rec("ce", "ATM", [(9, 10, 5.0), (9, 18, 6.0), (9, 31, 99.0)]),
                self._rec("pe", "ATM", [(9, 10, 7.0)])]                # only 20 min old -> too old
        rows, st = build_symbol("X", recs, ExpiryMapper(), 100, 15)
        r = [x for x in rows if x["time"] == "09:30"]
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["ce_ltp"], 6)            # 09:18 trade; 09:31 is after the snapshot
        self.assertIsNone(r[0]["pe_ltp"])              # nothing within 15 min -> blank, reported
        self.assertEqual(st.lag_max, 12)
        self.assertIn("PE_LEG_MISSING", {i[2] for i in st.issues})


class Units(unittest.TestCase):
    def test_rule_expiry(self):
        self.assertEqual(rule_expiry(2025, 8), date(2025, 8, 28))   # Thursday era
        self.assertEqual(rule_expiry(2025, 9), date(2025, 9, 30))   # Tuesday era
        self.assertEqual(rule_expiry(2026, 11), date(2026, 11, 24))

    def test_credentials_redacted(self):
        c = load_credentials(env={"DHAN_ACCESS_TOKEN": TOKEN, "DHAN_CLIENT_ID": "1"},
                             dotenv=Path("/nonexistent"))
        self.assertNotIn(TOKEN, repr(c))
        self.assertNotIn(TOKEN, redact(f"header access-token: {TOKEN}"))

    def test_client_auth(self):
        c = DhanClient(Credentials(TOKEN, "1"), rps=0, transport=FakeDhan(auth_fail=True),
                       sleep=lambda s: None)
        with self.assertRaises(AuthError):
            c.rolling_option({})


if __name__ == "__main__":
    unittest.main()
