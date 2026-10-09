import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
"""Scanner: outcomes, loop control, shutdown, CLI, and Stage A parity with the
TypeScript plugin client. MOCK data only; no network; nothing waits."""

import contextlib
import copy
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import scan_local
from parity_support import compare
from sensex.dhan_client import DhanClient, RateLimiter, RawResponse, Redactor
from sensex.mock_dhan import MOCK_CREDENTIALS, MockTransport, load_scenario, replay_client
from sensex.scan import BLANK_TABLE, run_refresh
from sensex.scanner import JsonLogger, Scanner, ScannerConfig
from sensex.session import IST

SCENARIOS = ("gm18Sep", "s21Sep", "gmFuturesDiverge")


class FakeTime:
    """Aware UTC clock that only moves when the scanner sleeps."""

    def __init__(self, start):
        self.now = start
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += timedelta(seconds=s)


def scanner_for(name, *, bodies=None, script=None, at=None, cfg_over=None, clock=None, sleep=None):
    b, ref = load_scenario(name)
    if bodies is not None:
        b = bodies
    receipts = {"/optionchain": ref["receipts"]["chainMs"], "/marketfeed/quote": ref["receipts"]["futuresMs"]}
    t = MockTransport(b, receipts, default_ms=ref["receipts"]["chainMs"], script=script)
    lim_clock = [0.0]
    lim = RateLimiter(clock=lambda: lim_clock[0], sleep=lambda s: lim_clock.__setitem__(0, lim_clock[0] + s))
    out = io.StringIO()
    red = Redactor()
    log = JsonLogger(red, stream=out)
    client = DhanClient(MOCK_CREDENTIALS, transport=t, limiter=lim, redactor=red, logger=log, sleep=lambda s: None)
    when = at or datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, timezone.utc)
    kw = {"strikes": ref["strikes"], "futures_security_id": "844615", "futures_expiry": "2026-09-24",
          "ignore_session": True}
    kw.update(cfg_over or {})
    cfg = ScannerConfig(**kw)
    sc = Scanner(client, cfg, log, clock=clock or (lambda: when), sleep=sleep or (lambda s: None))
    return sc, t, ref, out


class StageAParity(unittest.TestCase):
    """Same HTTP bodies, same receipt times: Python scanner vs TS plugin client."""

    def test_scan_inputs_and_tables_match_the_typescript_plugin(self):
        for name in SCENARIOS:
            sc, t, ref, _ = scanner_for(name)
            rec = sc.run_once()
            self.assertEqual(rec["expiry"], ref["expiry"], name)
            self.assertEqual([c["path"] for c in t.calls], ref["calls"], name)
            ts = json.loads(json.dumps(ref["expected"]), object_hook=lambda d: float(d["$num"]) if set(d) == {"$num"} else d)
            py = rec["scanInputs"] if rec["status"] == "OK" else {"status": rec["status"], "reasons": rec["reason"].split(" | ")}
            mism = compare(py, ts, name)
            self.assertEqual(mism, [], "\n".join(mism[:20]))
            for hh, mm in [(10, 33), (9, 20), (12, 0), (14, 55), (15, 45)]:
                d = datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, IST)
                now = d.replace(hour=hh, minute=mm)
                self.assertEqual(run_refresh(py, now)[1], run_refresh(ts, now)[1], "%s %02d:%02d" % (name, hh, mm))

    def test_blocked_scenario_publishes_blank_table(self):
        rec = scanner_for("gmFuturesDiverge")[0].run_once()
        self.assertEqual((rec["status"], rec["table"]), ("BLOCKED", BLANK_TABLE))

    def test_bare_envelopes_give_identical_results(self):
        bodies, _ = load_scenario("s21Sep")
        bare = {p: v["data"] for p, v in bodies.items()}
        a = scanner_for("s21Sep")[0].run_once()
        b = scanner_for("s21Sep", bodies=bare)[0].run_once()
        self.assertEqual(set(b["envelopes"].values()), {"bare"})
        self.assertEqual(compare(b["scanInputs"], a["scanInputs"]), [])
        self.assertEqual(a["table"], b["table"])


class ScanOutcomes(unittest.TestCase):
    def test_session_closed_skips_without_calls(self):
        sc, t, _, _ = scanner_for("s21Sep", at=datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc),  # 08:30 IST
                                  cfg_over={"ignore_session": False})
        sc.cfg.ignore_session = False
        rec = sc.run_once()
        self.assertEqual((rec["status"], rec["reason"]), ("SKIPPED", "BEFORE_OPEN"))
        self.assertEqual(t.calls, [])

    def test_auth_failure_stops_and_never_logs_secrets(self):
        echo = {"errorCode": "DH-901", "errorMessage": "bad token %s" % MOCK_CREDENTIALS.access_token}
        sc, _, _, out = scanner_for("s21Sep", script={"/optionchain": [RawResponse(401, json.dumps(echo).encode(), {}, 1)]})
        rec = sc.run_once()
        self.assertEqual(rec["status"], "AUTH_FAILED")
        self.assertTrue(sc.stop_event.is_set())
        self.assertNotIn(MOCK_CREDENTIALS.access_token, out.getvalue() + json.dumps(rec))
        self.assertNotIn(MOCK_CREDENTIALS.client_id, out.getvalue() + json.dumps(rec))

    def test_plan_missing(self):
        sc, *_ = scanner_for("s21Sep", script={"/optionchain/expirylist": [RawResponse(403, b'{"errorCode":"806"}', {}, 1)]})
        self.assertEqual(sc.run_once()["status"], "PLAN_MISSING")

    def test_futures_contract_missing_or_expired(self):
        sc, t, _, _ = scanner_for("s21Sep", cfg_over={"futures_security_id": None, "futures_expiry": None})
        rec = sc.run_once()
        self.assertEqual(rec["status"], "CONFIG_BLOCKED")
        self.assertIn("BLOCKED", rec["reason"])
        sc, *_ = scanner_for("s21Sep", cfg_over={"futures_expiry": "2026-09-20"})
        self.assertEqual(sc.run_once()["status"], "CONFIG_BLOCKED")

    def test_invalid_chain(self):
        bodies, _ = load_scenario("s21Sep")
        bad = copy.deepcopy(bodies)
        bad["/optionchain"]["data"]["oc"] = {}
        rec = scanner_for("s21Sep", bodies=bad)[0].run_once()
        self.assertEqual((rec["status"], rec["table"]), ("INVALID_DATA", BLANK_TABLE))

    def test_bad_expiry_list(self):
        bodies, _ = load_scenario("s21Sep")
        bad = copy.deepcopy(bodies)
        bad["/optionchain/expirylist"]["data"] = ["2026-09-01"]   # nothing after today
        self.assertEqual(scanner_for("s21Sep", bodies=bad)[0].run_once()["status"], "INVALID_DATA")

    def test_futures_quote_missing_blocks_the_gate(self):
        bodies, _ = load_scenario("s21Sep")
        bad = copy.deepcopy(bodies)
        bad["/marketfeed/quote"]["data"] = {"BSE_FNO": {}}
        rec = scanner_for("s21Sep", bodies=bad)[0].run_once()
        self.assertEqual(rec["status"], "BLOCKED")
        self.assertIn("No futures quote", rec["reason"])
        self.assertTrue(any("QUOTE_MISSING_INSTRUMENT" in w for w in rec["warnings"]))

    def test_candle_endpoint_down_degrades_to_no_level(self):
        sc, *_ = scanner_for("s21Sep", script={"/charts/intraday": [RawResponse(502, b"<html>", {}, 1)] * 5})
        rec = sc.run_once()
        self.assertEqual(rec["status"], "OK")
        self.assertTrue(any(w.startswith("CANDLES_UNAVAILABLE") for w in rec["warnings"]))
        self.assertTrue(any(w.startswith("NO_CANDLES") for w in rec["warnings"]))
        # No candles = no candle timestamp: RULES.md §5 blank table (candles/snapshot skew), not invented levels.
        self.assertEqual((rec["table"], rec["rowStatuses"]), (BLANK_TABLE, []))

    def test_stale_candles(self):
        bodies, _ = load_scenario("s21Sep")
        gm, _ = load_scenario("gm18Sep")
        stale = dict(bodies)
        stale["/charts/intraday"] = gm["/charts/intraday"]   # 18-Sep bars against a 21-Sep snapshot
        rec = scanner_for("s21Sep", bodies=stale)[0].run_once()
        self.assertEqual(rec["rowStatuses"], ["STALE"])

    def test_duplicate_snapshot_is_flagged(self):
        sc, *_ = scanner_for("s21Sep")
        first, second = sc.run_once(), sc.run_once()
        self.assertFalse(any("DUPLICATE_SNAPSHOT" in w for w in first["warnings"]))
        self.assertTrue(any("DUPLICATE_SNAPSHOT" in w for w in second["warnings"]))

    def test_expiry_day_uses_next_weekly_and_requests_todays_candles(self):
        bodies, ref = load_scenario("s21Sep")
        b = copy.deepcopy(bodies)
        b["/optionchain/expirylist"]["data"] = ["2026-09-21", "2026-09-24", "2026-10-01"]   # today is an expiry
        sc, t, _, _ = scanner_for("s21Sep", bodies=b)
        rec = sc.run_once()
        self.assertEqual(rec["expiry"], "2026-09-24")                          # RULES.md §6: next weekly on expiry day
        chain_call = next(c for c in t.calls if c["path"] == "/optionchain")
        self.assertEqual(chain_call["body"]["Expiry"], "2026-09-24")
        candle_call = next(c for c in t.calls if c["path"] == "/charts/intraday")
        self.assertEqual((candle_call["body"]["fromDate"], candle_call["body"]["toDate"]),
                         ("2026-09-21 09:15:00", "2026-09-21 15:30:00"))      # today's session (RULES.md §4)
        self.assertEqual((candle_call["body"]["securityId"], candle_call["body"]["exchangeSegment"],
                          candle_call["body"]["instrument"], candle_call["body"]["interval"]), ("51", "IDX_I", "INDEX", 5))
        quote_call = next(c for c in t.calls if c["path"] == "/marketfeed/quote")
        self.assertEqual(quote_call["body"], {"BSE_FNO": [844615]})

    def test_config_guards(self):
        sc, *_ = scanner_for("s21Sep")
        with self.assertRaises(ValueError):
            Scanner(sc.client, ScannerConfig(strikes=[]), sc.log)
        with self.assertRaises(ValueError):
            Scanner(sc.client, ScannerConfig(strikes=[1.0], interval_s=2), sc.log)


class LoopControl(unittest.TestCase):
    def _loop(self, start_ist, **over):
        ft = FakeTime(start_ist.astimezone(timezone.utc))
        sc, t, _, _ = scanner_for("s21Sep", clock=ft.clock, sleep=ft.sleep,
                                  cfg_over=dict({"ignore_session": False, "interval_s": 60}, **over))
        sc.cfg.ignore_session = False
        return sc, t, ft

    def test_interval_and_max_scans(self):
        sc, t, ft = self._loop(datetime(2026, 9, 21, 10, 0, tzinfo=IST), max_scans=3)
        self.assertEqual(sc.run_loop(), "MAX_SCANS")
        self.assertEqual(sc.state.scans, 3)
        self.assertAlmostEqual(sum(ft.slept), 120.0, places=6)   # two 60 s waits between three scans
        self.assertTrue(all(s <= 1.0 for s in ft.slept))         # sleeps in slices: shutdown stays prompt

    def test_stops_at_session_end(self):
        sc, t, ft = self._loop(datetime(2026, 9, 21, 15, 27, tzinfo=IST))
        self.assertEqual(sc.run_loop(), "SESSION_AFTER_STOP")
        self.assertEqual(sc.state.scans, 3)   # 15:27, 15:28, 15:29 — none at/after 15:30

    def test_does_not_start_outside_session(self):
        for start, why in [(datetime(2026, 9, 20, 11, 0, tzinfo=IST), "SESSION_WEEKEND"),
                           (datetime(2026, 9, 21, 16, 0, tzinfo=IST), "SESSION_AFTER_STOP")]:
            sc, t, _ = self._loop(start)
            self.assertEqual(sc.run_loop(), why)
            self.assertEqual(t.calls, [])

    def test_graceful_shutdown_mid_wait(self):
        sc, t, ft = self._loop(datetime(2026, 9, 21, 10, 0, tzinfo=IST))
        orig = ft.sleep

        def sleep_then_signal(s):
            orig(s)
            if len(ft.slept) == 5:
                sc.request_stop("SIGINT")
        sc.sleep = sleep_then_signal
        self.assertEqual(sc.run_loop(), "SIGINT")
        self.assertEqual(sc.state.scans, 1)
        self.assertEqual(len(ft.slept), 5)

    def test_auth_failure_ends_loop(self):
        sc, t, ft = self._loop(datetime(2026, 9, 21, 10, 0, tzinfo=IST))
        sc.client.transport.script["/optionchain"] = [RawResponse(401, b"{}", {}, 1)]
        self.assertEqual(sc.run_loop(), "AUTH_FAILED")
        self.assertEqual(sc.state.scans, 1)


class Cli(unittest.TestCase):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scan_local.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_mock_mode_offline(self):
        code, out, _ = self.run_cli(["--mock"], {})
        self.assertEqual(code, 0)
        self.assertIn("MOCK DATA", out)
        self.assertIn("| 74500 | CE |", out)

    def test_missing_credentials(self):
        code, out, err = self.run_cli(["--once", "--strikes", "74500"], {"DHAN_CLIENT_ID": "1100999999"})
        self.assertEqual(code, 2)
        self.assertNotIn("1100999999", out + err)

    def test_strikes_required_and_expired_token_refused(self):
        expired = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjEwMDAwMDAwMDB9.c2lnbmF0dXJlLXRlc3Q"   # exp 2001
        code, out, err = self.run_cli(["--once", "--log-file", os.devnull], {"DHAN_CLIENT_ID": "X1", "DHAN_ACCESS_TOKEN": expired})
        self.assertEqual(code, 2)
        self.assertNotIn(expired, out + err)


if __name__ == "__main__":
    unittest.main()


class Recording(unittest.TestCase):
    def test_recorder_saves_market_data_only(self):
        import tempfile
        from pathlib import Path
        from sensex.localio import RecordingTransport
        bodies, ref = load_scenario("s21Sep")
        bodies = dict(bodies, **{"/profile": {"status": "success", "data": {"dhanClientId": "1100999999"}}})
        with tempfile.TemporaryDirectory() as d:
            rt = RecordingTransport(MockTransport(bodies, default_ms=1), d)
            client = DhanClient(MOCK_CREDENTIALS, transport=rt, sleep=lambda s: None,
                                limiter=RateLimiter(clock=lambda: 0.0 + len(rt.bodies), sleep=lambda s: None))
            rt.begin_scan(1, {"strikes": [1.0]})
            client.profile()
            client.expiry_list()
            rt.flush()
            saved = "".join(p.read_text() for p in Path(d).rglob("*.json"))
            self.assertNotIn("/profile", saved)
            self.assertNotIn("1100999999", saved)
            self.assertNotIn(MOCK_CREDENTIALS.access_token, saved)
            self.assertNotIn("access-token", saved)
            self.assertIn("/optionchain/expirylist", saved)
