import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
"""Phase 6 Cloud package: generated bundles are current, contain no order or
credential-handling paths, run on a bare runtime, reproduce Stage A, and the
OBSERVE -> log -> decode -> replay -> report chain works end to end.
MOCK data only; no network; nothing waits."""

import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import symtable
import builtins
import tempfile
import unittest
import ast
from datetime import datetime, timedelta, timezone
from unittest import mock

from parity_support import compare
from sensex.mock_dhan import load_scenario

ROOT = pathlib.Path(__file__).resolve().parents[2]
DIST = ROOT / "cloud" / "dist"
SINGLE = DIST / "single" / "sensex_observer.py"
MULTI = DIST / "multi"
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "tools"))
import build_bundle  # noqa: E402
import decode_cloud_log  # noqa: E402
import summarize_observation  # noqa: E402

SCENARIOS = ("gm18Sep", "s21Sep", "gmFuturesDiverge")
FAKE_CLIENT = "1000000001"


def fake_jwt(hours=20):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    exp = int((datetime.now(timezone.utc) + timedelta(hours=hours)).timestamp())
    return "%s.%s.%s" % (enc({"alg": "HS512"}), enc({"exp": exp, "dhanClientId": FAKE_CLIENT}), "c2lnbmF0dXJlLW5vdC1yZWFs")


_loaded = {}


def load_single():
    if "single" not in _loaded:
        spec = importlib.util.spec_from_file_location("bx_single_under_test", SINGLE)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        _loaded["single"] = m
    return _loaded["single"]


def load_multi():
    if "multi" not in _loaded:
        sys.path.insert(0, str(MULTI))
        spec = importlib.util.spec_from_file_location("bx_main_under_test", MULTI / "main.py")
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        _loaded["multi"] = m
    return _loaded["multi"]


def ns(bundle):
    """Namespace exposing the engine classes for either build."""
    if bundle is load_single():
        return bundle
    import bx_dhan_client, bx_scanner  # noqa: E401  (multi build on sys.path)
    return type("N", (), {**vars(bx_dhan_client), **vars(bx_scanner)})


class Transport:
    def __init__(self, raw_cls, bodies, receipts, default_ms, script=None, poll_fn=None):
        self.raw, self.bodies, self.receipts, self.default_ms = raw_cls, bodies, receipts, default_ms
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.poll_fn, self.polls = poll_fn, 0

    def send(self, method, url, headers, body, timeout):
        path = url.split("/v2", 1)[1]
        self.calls.append((method, path))
        req = json.loads(body) if body else None
        if self.poll_fn and path == "/marketfeed/quote" and "IDX_I" in (req or {}):
            self.polls += 1
            return self.raw(200, json.dumps({"status": "success", "data": self.poll_fn(self.polls, req)}).encode(),
                            {}, self.default_ms + self.polls * 5000)
        if self.script.get(path):
            status, b = self.script[path].pop(0)
            return self.raw(status, b, {}, self.default_ms)
        return self.raw(200, json.dumps(self.bodies[path]).encode(), {}, self.receipts.get(path, self.default_ms))


def bundle_scanner(bundle, name, clock=None, cfg_over=None):
    n = ns(bundle)
    b, ref = load_scenario(name)
    t = Transport(n.RawResponse, b, {"/optionchain": ref["receipts"]["chainMs"],
                                      "/marketfeed/quote": ref["receipts"]["futuresMs"]}, ref["receipts"]["chainMs"])
    sim = [0.0]
    lim = n.RateLimiter(clock=lambda: sim[0], sleep=lambda s: sim.__setitem__(0, sim[0] + s))
    red = n.Redactor()
    client = n.DhanClient(n.Credentials("MOCKCLIENT", "mock-token-not-a-real-credential-000"), transport=t,
                          limiter=lim, redactor=red, sleep=lambda s: None)
    at = datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, timezone.utc)
    kw = dict(strikes=ref["strikes"], futures_security_id="844615", futures_expiry="2026-09-24", ignore_session=True)
    kw.update(cfg_over or {})
    return n.Scanner(client, n.ScannerConfig(**kw), n.JsonLogger(red, stream=io.StringIO()),
                     clock=clock or (lambda: at)), t, ref


class Bundles(unittest.TestCase):
    def test_dist_is_generated_from_current_sources(self):
        built = build_bundle.build()
        for k, v in built.items():
            self.assertEqual((DIST / k).read_text(), v, "cloud/dist/%s is stale: run python3 cloud/build_bundle.py" % k)
        on_disk = {str(p.relative_to(DIST)) for p in DIST.rglob("*") if p.is_file() and "__pycache__" not in str(p)}
        self.assertEqual(on_disk, set(built))

    def test_single_file_has_no_unresolved_names(self):
        top = symtable.symtable(SINGLE.read_text(), str(SINGLE), "exec")
        defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
        missing = []

        def walk(t):
            for s in t.get_symbols():
                if t.get_type() != "module" and s.is_global() and s.is_referenced() \
                        and s.get_name() not in defined and not hasattr(builtins, s.get_name()):
                    missing.append((t.get_name(), s.get_name()))
            for c in t.get_children():
                walk(c)
        walk(top)
        self.assertEqual(missing, [])

    def test_stage_a_through_both_bundles_matches_typescript(self):
        for loader in (load_single, load_multi):
            for name in SCENARIOS:
                sc, t, ref = bundle_scanner(loader(), name)
                rec = sc.run_once()
                self.assertEqual([c[1] for c in t.calls], ref["calls"], name)
                ts = json.loads(json.dumps(ref["expected"]),
                                object_hook=lambda d: float(d["$num"]) if set(d) == {"$num"} else d)
                py = rec["scanInputs"] if rec["status"] == "OK" else {"status": rec["status"],
                                                                      "reasons": rec["reason"].split(" | ")}
                self.assertEqual(compare(py, ts, name), [], "%s %s" % (loader.__name__, name))

    def test_validate_mode_runs_on_a_bare_runtime(self):
        """Copied alone into an empty directory (as Cloud runs /tmp/script.py),
        isolated mode, no credentials, network disabled by an audit hook."""
        guard = ("import sys\n"
                 "def _h(ev, a):\n"
                 "    if ev in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):\n"
                 "        raise RuntimeError('blocked ' + ev)\n"
                 "sys.addaudithook(_h)\n")
        for label, files, entry in (("single", [SINGLE], "script.py"),
                                    ("multi", sorted(MULTI.glob("*.py")), "main.py")):
            with tempfile.TemporaryDirectory() as d:
                for f in files:
                    shutil.copy(f, pathlib.Path(d) / (entry if label == "single" else f.name))
                env = {k: v for k, v in os.environ.items() if not k.startswith("DHAN_")}
                # `python main.py` puts the script directory first on sys.path; -I drops it, so add it back
                runner = guard + "sys.path.insert(0, %r); import runpy; runpy.run_path(%r, run_name='__main__')" % (
                    d, str(pathlib.Path(d) / entry))
                p = subprocess.run([sys.executable, "-I", "-B", "-c", runner], cwd=d, env=env, capture_output=True,
                                   text=True, timeout=120)
                self.assertEqual(p.returncode, 0, label + p.stderr[-2000:])
                lines = p.stdout.strip().splitlines()
                self.assertTrue(lines and all(l.startswith("BX|") for l in lines), label)
                st = [json.loads(l[3:]) for l in lines if '"selftest"' in l][0]
                self.assertEqual(st["result"], "PASS", label)
                self.assertEqual(sorted(os.listdir(d)), sorted(pathlib.Path(f).name if label == "multi" else entry
                                                               for f in files), "VALIDATE must not write files")


FORBIDDEN_IDENTIFIERS = ("place_order", "placeorder", "modify_order", "cancel_order", "exit_position", "killswitch",
                         "kill_switch", "generate_token", "generatetoken", "renew_token", "renewtoken", "totp", "pyotp",
                         "super_order", "forever", "slice_order", "convert_position", "margin_calculator")
FORBIDDEN_CALLS = ("exec", "eval", "compile", "__import__", "input")
FORBIDDEN_MODULES = ("subprocess", "socket", "importlib", "pickle", "marshal", "ctypes", "shutil", "dhanhq", "requests",
                     "platform", "os", "pathlib", "base64", "code")
# Patterns the Dhan Cloud scanner reported as blocked on 9 Oct 2026 (Security Violation [CRITICAL]).
BLOCKED_TEXT = [r"\bpathlib\b", r"compile\(", r"os\.environ", r"\bbase64\b", r"sys\.exit\(", r"\\x",
                r"getattr\(", r"os\.getenv", r"os\.path", r"(?<![A-Za-z_.])open\(", r"\\u[0-9a-fA-F]{4}",
                # second Cloud scan (9 Oct 2026 22:17): chr( and bytearray( blocked; "import code" suspected = `types`
                r"(?<![A-Za-z_.])chr\(", r"bytearray\(", r"^\s*import types\b", r"^\s*from types\b",
                # fifth scan (22:25): still "import code" -> the bare word anywhere, strings included
                r"(?i)\bcode\b"]   # Dhan Cloud scanner: "querying host platform/OS details is not allowed" (9 Oct 2026)
HOST_QUERIES = {("sys", "platform"), ("sys", "implementation"), ("sys", "executable"), ("sys", "version"),
                ("os", "uname"), ("os", "name"), ("os", "listdir"), ("os", "getcwd"), ("os", "cpu_count")}


def _bundle_trees():
    yield "single", ast.parse(SINGLE.read_text())
    for f in sorted(MULTI.glob("*.py")):
        yield "multi/" + f.name, ast.parse(f.read_text())


class Safety(unittest.TestCase):
    def test_no_order_token_or_dynamic_code_paths(self):
        for label, tree in _bundle_trees():
            for n in ast.walk(tree):
                ident = (n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) else
                         n.attr if isinstance(n, ast.Attribute) else n.id if isinstance(n, ast.Name) else None)
                if ident:
                    self.assertFalse(any(f in ident.lower() for f in FORBIDDEN_IDENTIFIERS), "%s: %s" % (label, ident))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                    self.assertNotIn(n.func.id, FORBIDDEN_CALLS, label)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                    self.assertNotIn((n.value.id, n.attr), HOST_QUERIES, "%s: host query %s.%s" % (label, n.value.id, n.attr))
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
                    for m in mods:
                        self.assertNotIn(m.split(".")[0], FORBIDDEN_MODULES, "%s imports %s" % (label, m))
                        # fourth Cloud scan (9 Oct 2026 22:22): "Blocked import detected: code" for `unicodedata`,
                        # i.e. blocked names are matched as substrings of the imported module name
                        for bad in ("code", "os", "subprocess", "socket", "pathlib", "base64", "platform", "pickle",
                                    "marshal", "ctypes", "shutil", "importlib", "types"):
                            if bad == "os":
                                continue   # too short for a substring rule to be meaningful; exact match above
                            self.assertNotIn(bad, m.lower(), "%s imports %s (contains %r)" % (label, m, bad))

    def test_no_pattern_the_cloud_scanner_blocked(self):
        import re
        for f in [SINGLE] + sorted(MULTI.glob("*.py")):
            text = f.read_text()
            for pat in BLOCKED_TEXT:
                m = re.search(pat, text, re.M)
                self.assertIsNone(m, "%s contains blocked pattern %s: %r" % (f.name, pat, m and text[max(0, m.start() - 40):m.end() + 20]))

    def test_no_identifier_named_like_a_blocked_module(self):
        """Third Cloud scan (9 Oct 2026 22:19): 'Blocked import detected: code' with no such import; the
        scanner evidently treats the NAME `code` as the module. No bundle may bind or read such names."""
        blocked = {"code", "os", "subprocess", "socket", "pathlib", "base64", "platform", "types", "importlib"}
        for label, tree in _bundle_trees():
            for n in ast.walk(tree):
                name = n.id if isinstance(n, ast.Name) else n.arg if isinstance(n, ast.arg) else \
                    n.arg if isinstance(n, ast.keyword) else None
                self.assertNotIn(name, blocked, "%s line %s uses the name %r" % (label, getattr(n, "lineno", "?"), name))

    def test_every_request_site_is_on_the_allow_list(self):
        single = load_single()
        allowed = set(single.READ_ONLY_ENDPOINTS)
        for label, tree in _bundle_trees():
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "request" \
                        and len(n.args) >= 2 and all(isinstance(a, ast.Constant) for a in n.args[:2]):
                    self.assertIn((n.args[0].value, n.args[1].value), allowed, label)
        for path in ("/orders", "/super/orders", "/positions", "/killswitch", "/pnlExit", "/RenewToken",
                     "/edis/tpin", "/margincalculator", "/forever/orders", "/ip/setIP"):
            for method in ("GET", "POST", "PUT", "DELETE"):
                with self.assertRaises(single.ForbiddenEndpointError):
                    single.DhanClient.assert_read_only(method, path)

    def test_allow_list_is_identical_in_all_builds(self):
        from sensex.dhan_client import FORBIDDEN_FRAGMENTS, READ_ONLY_ENDPOINTS
        load_multi()
        self.assertEqual(load_single().READ_ONLY_ENDPOINTS, READ_ONLY_ENDPOINTS)
        self.assertEqual(sys.modules["bx_dhan_client"].READ_ONLY_ENDPOINTS, READ_ONLY_ENDPOINTS)
        self.assertEqual(load_single().FORBIDDEN_FRAGMENTS, FORBIDDEN_FRAGMENTS)

    def test_no_credentials_in_config(self):
        for label, tree in _bundle_trees():
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    self.assertNotRegex(n.value, r"eyJ[A-Za-z0-9_-]{10,}\.", label)


def run_main(bundle, *, mode, env, clock_start=None, script=None, cfg=None, poll_fn=None):
    """Run the Cloud entry with a fake transport, simulated clock and limiter."""
    n = ns(bundle)
    b, ref = load_scenario("s21Sep")
    receipts = {"/optionchain": ref["receipts"]["chainMs"], "/marketfeed/quote": ref["receipts"]["futuresMs"]}
    t = Transport(n.RawResponse, b, receipts, ref["receipts"]["chainMs"], script=script, poll_fn=poll_fn)
    now = [clock_start or datetime.fromtimestamp(ref["receipts"]["chainMs"] / 1000, timezone.utc)]
    real_scanner, real_client = n.Scanner, n.DhanClient
    sim = [0.0]

    def scanner(client, config, log):
        return real_scanner(client, config, log, clock=lambda: now[0],
                            sleep=lambda s: now.__setitem__(0, now[0] + timedelta(seconds=s)))

    def client(creds, **kw):
        kw["limiter"] = n.RateLimiter(clock=lambda: sim[0], sleep=lambda s: sim.__setitem__(0, sim[0] + s))
        kw["sleep"] = lambda s: None
        return real_client(creds, **kw)
    over = dict(MODE=mode, STRIKES=ref["strikes"], FUTURES_SECURITY_ID="844615", FUTURES_EXPIRY="2026-09-24",
                STOP_TIME="10:45", RECORD_EVERY_N=4, RECORD_CHUNK=500)
    over["CLIENT_ID"] = env.get("DHAN_CLIENT_ID", "{{DHAN_CLIENT_ID}}")      # what Cloud substitutes
    over["ACCESS_TOKEN"] = env.get("DHAN_ACCESS_TOKEN", "{{DHAN_ACCESS_TOKEN}}")
    over.update(cfg or {})
    out = io.StringIO()
    with contextlib.ExitStack() as st:
        for k, v in over.items():
            st.enter_context(mock.patch.object(bundle, k, v))
        st.enter_context(mock.patch.object(bundle, "Scanner", scanner))
        st.enter_context(mock.patch.object(bundle, "DhanClient", client))
        st.enter_context(mock.patch.object(bundle, "UrllibTransport", lambda: t))
        st.enter_context(contextlib.redirect_stdout(out))
        code = bundle.main()
    return code, out.getvalue(), t


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.token = fake_jwt()
        self.env = {"DHAN_CLIENT_ID": FAKE_CLIENT, "DHAN_ACCESS_TOKEN": self.token}

    def test_observe_log_decode_replay_report(self):
        for loader in (load_single, load_multi):
            code, text, t = run_main(loader(), mode="OBSERVE", env=self.env)
            self.assertEqual(code, 0, text[-1500:])
            self.assertNotIn(self.token, text)
            self.assertNotIn(FAKE_CLIENT, text)
            self.assertTrue(all(l.startswith("BX|") for l in text.splitlines()))
            self.assertEqual({p for _, p in t.calls}, {"/optionchain/expirylist", "/optionchain", "/marketfeed/quote",
                                                       "/charts/intraday"})
            # simulate the Cloud log viewer prefixing timestamps
            exported = ["2026-09-21 10:%02d:00 | %s" % (i % 60, l) for i, l in enumerate(text.splitlines())]
            events, records, bad, incomplete, secrets = decode_cloud_log.decode_lines(exported)
            self.assertEqual((bad, incomplete, secrets), ([], [], []))
            scans = [e for e in events if e["event"] == "scan"]
            self.assertEqual(len(scans), 12)            # 10:33:56 .. 10:44:56 every 60 s, stop 10:45
            self.assertEqual(sorted(records), ["20260921-0001", "20260921-0004", "20260921-0008", "20260921-0012"])
            summary = [e for e in events if e["event"] == "session_summary"][0]
            self.assertEqual((summary["ordersPlaced"], summary["stopReason"]), (0, "SESSION_AFTER_STOP"))
            o = scans[0]["observation"]
            for key in ("endpoints", "chainCompleteness", "contract", "prices", "gate", "levels", "legs",
                        "refreshRows", "freshness", "conditions", "skipped", "strategy", "durationMs", "nextScanIst"):
                self.assertIn(key, o)
            self.assertEqual(o["strategy"]["verdict"], "NO_SIGNAL")
            self.assertEqual(scans[-1]["observation"]["nextScanIst"], "none (stop time 10:45:00)")
            with tempfile.TemporaryDirectory() as d:
                decode_cloud_log.write_out(pathlib.Path(d), events, records)
                import replay_recorded
                for sd in sorted((pathlib.Path(d) / "scan-records").glob("scan-*")):
                    rec, _ = replay_recorded.replay(sd)
                    self.assertEqual(rec["table"], scans[0]["table"], sd.name)
                report = summarize_observation.summarize(
                    [json.loads(l) for l in (pathlib.Path(d) / "events.jsonl").read_text().splitlines()])
            for h in ("## 1. Data retrieval correctness", "## 2. Calculation correctness",
                      "## 3. Strategy-rule effectiveness", "NOT_CONFIGURED", "orders placed: **0**"):
                self.assertIn(h, report)

    def test_paper_trades_flow_from_cloud_entry_to_report(self):
        """Index crosses the CE trigger (74725) on poll 2; every configured CE leg then bids +6.5 over its entry ask."""
        def poll(n, req):
            legs = {str(i): {"last_price": 1, "depth": {"buy": [{"price": 100.0 if n <= 2 else 107.5, "quantity": 1,
                                                                 "orders": 1}],
                                                         "sell": [{"price": 101.0 if n <= 2 else 108.0, "quantity": 1,
                                                                   "orders": 1}]}} for i in req.get("BSE_FNO", [])}
            return {"IDX_I": {"51": {"last_price": 74700.0 if n == 1 else 74730.0}}, "BSE_FNO": legs}
        for loader in (load_single, load_multi):
            code, text, t = run_main(loader(), mode="OBSERVE", env=self.env, poll_fn=poll)
            self.assertEqual(code, 0, text[-1500:])
            events, records, bad, incomplete, secrets = decode_cloud_log.decode_lines(text.splitlines())
            self.assertEqual((bad, secrets), ([], []))
            exits = [e for e in events if e["event"] == "paper_exit"]
            self.assertEqual(len(exits), 4, loader.__name__)              # 4 CE strikes, PE rows are WEAK_LEVEL
            self.assertEqual({(e["outcome"], e["pnlPts"]) for e in exits}, {("TAKE_PROFIT", 6.5)})
            summary = [e for e in events if e["event"] == "paper_summary"][0]
            self.assertEqual((summary["trades"], summary["winRateTpVsSl"]), (4, 1.0))
            report = summarize_observation.summarize(events)
            self.assertIn("### PAPER trades", report)
            self.assertIn("TAKE_PROFIT 4", report)
            self.assertNotIn(self.token, text)
            self.assertEqual(t.polls, 132)                                 # 11 polls in each of the 12 60-s waits

    def test_auth_failure_stops_and_leaks_nothing(self):
        echo = json.dumps({"errorCode": "DH-901", "errorMessage": "bad token " + self.token}).encode()
        code, text, _ = run_main(load_single(), mode="OBSERVE", env=self.env,
                                 script={"/optionchain/expirylist": [(401, echo)]})
        self.assertEqual(code, 3)
        self.assertNotIn(self.token, text)
        self.assertIn('"stopReason": "AUTH_FAILED"', text)

    def test_live_modes_refuse_without_config_or_credentials(self):
        code, text, t = run_main(load_single(), mode="LIVE_CHECK", env=self.env, cfg={"STRIKES": []})
        self.assertEqual((code, t.calls), (2, []))
        self.assertIn("config_blocked", text)
        code, text, t = run_main(load_single(), mode="LIVE_CHECK", env={})
        self.assertEqual((code, t.calls), (3, []))
        self.assertIn("credentials_missing", text)

    def test_expired_token_refused_before_any_call(self):
        code, text, t = run_main(load_multi(), mode="LIVE_CHECK",
                                 env={"DHAN_CLIENT_ID": FAKE_CLIENT, "DHAN_ACCESS_TOKEN": fake_jwt(hours=-1)})
        self.assertEqual((code, t.calls), (3, []))
        self.assertIn("token_expired", text)

    def test_live_check_is_one_scan(self):
        code, text, t = run_main(load_multi(), mode="LIVE_CHECK", env=self.env)
        self.assertEqual(code, 0)
        self.assertEqual(len([l for l in text.splitlines() if '"event": "scan"' in l]), 1)
        # 4 snapshot calls + 1-min futures and 1-min index candles (EXTRA_SERIES); no paper polls in LIVE_CHECK
        self.assertEqual([p for _, p in t.calls], ["/optionchain/expirylist", "/optionchain", "/marketfeed/quote",
                                                   "/charts/intraday", "/charts/intraday", "/charts/intraday"])

    def test_decoder_refuses_a_log_containing_a_token(self):
        events, records, bad, incomplete, secrets = decode_cloud_log.decode_lines(
            ['BX|{"event": "x", "m": "%s"}' % self.token])
        self.assertEqual(secrets, [1])
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "log.txt"
            p.write_text('BX|{"event": "x", "m": "%s"}\n' % self.token)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(decode_cloud_log.main([str(p), "--out", str(pathlib.Path(d) / "o")]), 3)
            self.assertFalse((pathlib.Path(d) / "o").exists())


if __name__ == "__main__":
    unittest.main()
