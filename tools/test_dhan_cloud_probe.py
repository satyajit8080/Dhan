"""Tests for the Dhan Cloud probes. Stdlib only:
    cd tools && python3 -m unittest -v test_dhan_cloud_probe
"""

import ast
import contextlib
import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import dhan_cloud_probe as basic
import dhan_cloud_probe_extended as ext

HERE = Path(__file__).resolve().parent
PROBES = [HERE / "dhan_cloud_probe.py", HERE / "dhan_cloud_probe_extended.py"]


def code_without_docstrings(path):
    """Source with module/function docstrings and comments removed."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def imported_modules(path):
    tree = ast.parse(path.read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


class StaticSafety(unittest.TestCase):
    def test_stdlib_only(self):
        for p in PROBES:
            third_party = {m for m in imported_modules(p) if m not in sys.stdlib_module_names}
            self.assertEqual(third_party, set(), p.name)

    def test_no_order_or_credential_code(self):
        for p in PROBES:
            code_only = code_without_docstrings(p)
            for bad in ("place_order", "/orders", "access-token", "access_token", "client-id",
                        "generateAccessToken", "RenewToken", "killswitch", "subprocess"):
                self.assertNotIn(bad, code_only, "%s contains %s" % (p.name, bad))

    def test_basic_probe_avoids_os_files_env(self):
        self.assertNotIn("os", imported_modules(PROBES[0]))
        code = code_without_docstrings(PROBES[0])
        builtin_open = [n for n in ast.walk(ast.parse(code)) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == "open"]
        self.assertEqual(builtin_open, [])
        self.assertNotIn("environ", code)
        # A text-matching scanner may read docstrings too: keep risky words out.
        self.assertNotIn("subprocess", PROBES[0].read_text())

    def test_extended_probe_makes_no_network_calls(self):
        mods = imported_modules(PROBES[1])
        for net in ("urllib", "http", "socket", "ssl"):
            self.assertNotIn(net, mods)

    def test_requests_are_head_without_headers_or_body(self):
        tree = ast.parse(PROBES[0].read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "Request"]
        self.assertTrue(calls)
        for c in calls:
            kw = {k.arg: k.value for k in c.keywords}
            self.assertNotIn("headers", kw)
            self.assertNotIn("data", kw)
            self.assertEqual(getattr(kw.get("method"), "value", None), "HEAD")
            self.assertEqual(len(c.args), 1)  # url only

    def test_targets_are_public_https(self):
        for _, url in basic.REACH_TARGETS:
            self.assertTrue(url.startswith("https://"))


class Reach(unittest.TestCase):
    def test_unreachable_never_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")):
            self.assertIn("UNREACHABLE URLError", basic.reach("https://example.invalid/"))

    def test_http_error_counts_as_reachable(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertIn("HTTP 404", basic.reach("https://x/"))

    def test_timeout_is_passed(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()) as m:
            self.assertIn("UNREACHABLE TimeoutError", basic.reach("https://x/", timeout=3))
            self.assertEqual(m.call_args.kwargs["timeout"], 3)

    def test_basic_main_runs_and_finishes(self):
        out = io.StringIO()
        with mock.patch.object(basic, "reach", return_value="HTTP 200 in 1 ms"), \
                contextlib.redirect_stdout(out):
            basic.main(heartbeats=1, heartbeat_seconds=0)
        text = out.getvalue()
        for key in ("PROBE python=", "PROBE local_tz=", "PROBE ist_clock=",
                    "PROBE reach_dhan_api=", "PROBE heartbeat=0", "PROBE done="):
            self.assertIn(key, text)


class EnvReport(unittest.TestCase):
    FAKE = {
        "DHAN_ACCESS_TOKEN": "eyJfake.secret.value",
        "CLIENT_1100999999": "x",
        "PATH": "/usr/bin",
        "PROBE_MARKER": "hello",
    }

    def test_values_never_appear(self):
        rep = ext.env_report(self.FAKE)
        text = repr(rep)
        for value in ("eyJfake", "/usr/bin", "hello", "1100999999"):
            self.assertNotIn(value, text)
        self.assertIn("PATH", rep["env_names"])
        self.assertIn("<masked-name>", rep["env_names"])

    def test_marker_presence_and_length_only(self):
        rep = ext.env_report(self.FAKE)
        self.assertTrue(rep["env_marker_present"])
        self.assertEqual(rep["env_marker_length"], 5)
        self.assertFalse(ext.env_report({})["env_marker_present"])


class FilesystemAndPersistence(unittest.TestCase):
    def test_write_check_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ext.write_check(d, "r1"), "WRITABLE")
            self.assertEqual(os.listdir(d), [])

    def test_write_check_reports_unwritable(self):
        self.assertTrue(ext.write_check("/nonexistent-dir-for-probe", "r1").startswith("NOT_WRITABLE"))

    def test_persistence_across_two_runs(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("previous=None", ext.persistence_check(d, "run1"))
            self.assertIn("previous=run1", ext.persistence_check(d, "run2"))

    def test_extended_main_end_to_end_without_leaking_env(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"DHAN_PIN": "123456", PROBE_SECRET: "topsecret"}), \
                mock.patch("os.getcwd", return_value=d), \
                mock.patch("tempfile.gettempdir", return_value=d), \
                mock.patch("os.path.expanduser", return_value=d):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ext.main()
        text = out.getvalue()
        self.assertIn("PROBE done=", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("topsecret", text)

    def test_package_report_handles_missing(self):
        rep = ext.package_report(["json", "definitely_not_a_package_xyz"])
        self.assertEqual(rep["definitely_not_a_package_xyz"], "NOT_INSTALLED")
        self.assertNotEqual(rep["json"], "NOT_INSTALLED")


PROBE_SECRET = "PROBE_TEST_SECRET"

if __name__ == "__main__":
    unittest.main()
