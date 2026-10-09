"""Read-only Dhan client: allow-list, request shapes, envelopes, retries, rate
limits, error classes and credential redaction. MOCK transport only."""

import io
import json
import os
import tempfile
import unittest

from sensex.dhan_client import (AuthError, Credentials, DhanClient, ForbiddenEndpointError, PlanError,
                                RateLimitedError, RateLimiter, RawResponse, Redactor, RequestRejectedError,
                                ResponseFormatError, TransientError)
from sensex.mock_dhan import MockTransport
from sensex.scanner import JsonLogger

TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJleHAiOjQxMDI0NDQ4MDAsInN1YiI6InRlc3QifQ.c2lnbmF0dXJlLXRlc3Qtb25seQ"  # fake JWT, exp 2100
CID = "1100999999"  # fake client id
CREDS = Credentials(CID, TOKEN)


def resp(status=200, body=None, headers=None, ms=1000):
    return RawResponse(status, json.dumps(body).encode() if body is not None else b"", headers or {}, ms)


def make(script=None, bodies=None, max_attempts=4):
    t = MockTransport(bodies or {}, script=script)
    clock = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    lim = RateLimiter(clock=lambda: clock[0], sleep=sleep)
    out = io.StringIO()
    red = Redactor()
    log = JsonLogger(red, stream=out)
    c = DhanClient(CREDS, transport=t, limiter=lim, redactor=red, logger=log, sleep=sleep,
                   max_attempts=max_attempts, jitter=lambda: 1.0)
    return c, t, sleeps, out


class AllowList(unittest.TestCase):
    def test_mutating_paths_refused_without_any_request(self):
        c, t, _, _ = make()
        for method, path in [("POST", "/orders"), ("PUT", "/orders/1"), ("DELETE", "/orders/1"),
                             ("POST", "/super/orders"), ("POST", "/forever/orders"), ("POST", "/killswitch"),
                             ("POST", "/pnlExit"), ("POST", "/positions/convert"), ("POST", "/ip/setIP"),
                             ("PUT", "/ip/modifyIP"), ("GET", "/RenewToken"), ("POST", "/edis/form"),
                             ("POST", "/margincalculator"), ("GET", "/fundlimit"), ("GET", "/positions"),
                             ("POST", "/charts/rollingoption"), ("DELETE", "/optionchain"), ("GET", "/optionchain")]:
            with self.assertRaises(ForbiddenEndpointError, msg=path):
                c.request(method, path, {})
        self.assertEqual(t.calls, [])

    def test_allow_list_contains_no_forbidden_path(self):
        from sensex.dhan_client import FORBIDDEN_FRAGMENTS, READ_ONLY_ENDPOINTS
        for method, path in READ_ONLY_ENDPOINTS:
            self.assertIn(method, ("GET", "POST"))
            self.assertFalse(any(f in path.lower() for f in FORBIDDEN_FRAGMENTS), path)

    def test_only_read_only_methods_exist(self):
        public = {n for n in dir(DhanClient) if not n.startswith("_")}
        self.assertEqual(public - {"assert_read_only", "request"},
                         {"expiry_list", "option_chain", "quote", "intraday_candles", "daily_candles", "profile"})


class RequestShapes(unittest.TestCase):
    """Payload keys as in DhanHQ-py v2.3.0 (_option_chain.py, _market_feed.py, _historical_data.py)."""

    def setUp(self):
        ok = {"status": "success", "data": {"oc": {}}}
        self.c, self.t, _, _ = make(bodies={p: ok for p in ["/optionchain", "/optionchain/expirylist", "/charts/intraday",
                                                            "/charts/historical", "/marketfeed/quote", "/profile"]})

    def test_shapes_and_headers(self):
        self.c.option_chain("2026-09-24")
        self.c.expiry_list()
        self.c.quote({"BSE_FNO": [844615]})
        self.c.intraday_candles("51", "IDX_I", "INDEX", 5, "2026-09-18 09:15:00", "2026-09-18 15:30:00")
        self.c.daily_candles("51", "IDX_I", "INDEX", "2026-09-01", "2026-09-19")
        bodies = [x["body"] for x in self.t.calls]
        self.assertEqual(bodies[0], {"UnderlyingScrip": 51, "UnderlyingSeg": "IDX_I", "Expiry": "2026-09-24"})
        self.assertEqual(bodies[1], {"UnderlyingScrip": 51, "UnderlyingSeg": "IDX_I"})
        self.assertEqual(bodies[2], {"BSE_FNO": [844615]})
        self.assertEqual(bodies[3], {"securityId": "51", "exchangeSegment": "IDX_I", "instrument": "INDEX",
                                     "interval": 5, "oi": False, "fromDate": "2026-09-18 09:15:00",
                                     "toDate": "2026-09-18 15:30:00"})
        self.assertEqual(set(bodies[4]), {"securityId", "exchangeSegment", "instrument", "expiryCode", "oi",
                                          "fromDate", "toDate"})
        for call in self.t.calls:
            self.assertNotIn(TOKEN, call["path"])
            self.assertEqual(call["headers"]["access-token"], TOKEN)
            self.assertEqual(call["headers"]["client-id"], CID)
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["timeout"], 15.0)

    def test_profile_is_get_with_sdk_header(self):
        self.c.profile()
        self.assertEqual(self.t.calls[-1]["method"], "GET")
        self.assertEqual(self.t.calls[-1]["headers"]["dhanClientId"], CID)

    def test_input_guards(self):
        with self.assertRaises(RequestRejectedError):
            self.c.intraday_candles("51", "IDX_I", "INDEX", 3, "a", "b")
        with self.assertRaises(RequestRejectedError):
            self.c.quote({})
        self.assertEqual(self.t.calls, [])


class Envelopes(unittest.TestCase):
    def test_wrapped_and_bare(self):
        arrays = {"timestamp": [1], "open": [1], "high": [1], "low": [1], "close": [1]}
        c, _, _, _ = make(script={"/charts/intraday": [resp(body={"status": "success", "data": arrays}),
                                                       resp(body=arrays)]})
        a = c.intraday_candles("51", "IDX_I", "INDEX", 5, "x", "y")
        b = c.intraday_candles("51", "IDX_I", "INDEX", 5, "x", "y")
        self.assertEqual((a.envelope, b.envelope), ("wrapped", "bare"))
        self.assertEqual(a.payload, b.payload)

    def test_unrecognised_and_non_json(self):
        c, _, _, _ = make(script={"/optionchain": [resp(body={"something": 1})],
                                  "/marketfeed/quote": [RawResponse(200, b"<html>", {}, 1)]})
        with self.assertRaises(ResponseFormatError):
            c.option_chain("2026-09-24")
        with self.assertRaises(ResponseFormatError):
            c.quote({"BSE_FNO": [1]})

    def test_status_failure_in_200(self):
        c, _, _, _ = make(script={"/optionchain": [resp(body={"status": "failure", "errorMessage": "x"})]})
        with self.assertRaises(RequestRejectedError):
            c.option_chain("2026-09-24")


class RetriesAndErrors(unittest.TestCase):
    ok = {"status": "success", "data": {"oc": {"1": {}}}}

    def test_429_retry_after_then_success(self):
        c, t, sleeps, _ = make(script={"/optionchain": [resp(429, {"errorCode": "DH-904"}, {"Retry-After": "2"}),
                                                        resp(200, self.ok)]})
        r = c.option_chain("2026-09-24")
        self.assertEqual(r.attempts, 2)
        self.assertIn(2.0, sleeps)

    def test_rate_limit_is_bounded(self):
        c, t, _, _ = make(script={"/optionchain": [resp(400, {"errorCode": "805"})] * 10}, max_attempts=4)
        with self.assertRaises(RateLimitedError):
            c.option_chain("2026-09-24")
        self.assertEqual(len(t.calls), 4)

    def test_transient_failures_retried(self):
        c, t, _, _ = make(script={"/optionchain": [ConnectionResetError("reset"), RawResponse(502, b"<html>", {}, 1),
                                                   resp(500, {"errorCode": "DH-908"}), resp(200, self.ok)]})
        self.assertEqual(c.option_chain("2026-09-24").attempts, 4)

    def test_transient_exhausted(self):
        c, t, _, _ = make(script={"/optionchain": [TimeoutError()] * 5}, max_attempts=3)
        with self.assertRaises(TransientError):
            c.option_chain("2026-09-24")
        self.assertEqual(len(t.calls), 3)

    def test_non_retryable_errors(self):
        cases = [(401, {}, AuthError), (403, {"errorCode": "DH-901"}, AuthError), (400, {"errorCode": "807"}, AuthError),
                 (400, {"errorCode": "806"}, PlanError), (403, {"errorCode": "DH-902"}, PlanError),
                 (400, {"errorCode": "811"}, RequestRejectedError), (400, {"errorCode": "DH-905"}, RequestRejectedError)]
        for status, body, exc in cases:
            c, t, _, _ = make(script={"/optionchain": [resp(status, body)] * 3})
            with self.assertRaises(exc, msg=(status, body)):
                c.option_chain("2026-09-24")
            self.assertEqual(len(t.calls), 1, (status, body))

    def test_option_chain_3s_rule_and_quote_1s(self):
        c, t, sleeps, _ = make(bodies={"/optionchain": self.ok, "/marketfeed/quote": {"status": "success", "data": {"BSE_FNO": {}}}})
        c.option_chain("2026-09-24")
        c.option_chain("2026-09-24")
        self.assertGreaterEqual(sum(sleeps), 3.0 - 1e-9)
        before = sum(sleeps)
        c.quote({"BSE_FNO": [1]})
        c.quote({"BSE_FNO": [1]})
        self.assertGreaterEqual(sum(sleeps) - before, 1.0 - 1e-9)


class CredentialsNeverLeak(unittest.TestCase):
    def test_server_echo_of_token_is_redacted_in_logs_and_errors(self):
        echo = {"errorCode": "DH-901", "errorMessage": "invalid token %s for client %s" % (TOKEN, CID)}
        c, _, _, out = make(script={"/optionchain": [resp(401, echo)]})
        with self.assertRaises(AuthError) as cm:
            c.option_chain("2026-09-24")
        for text in (out.getvalue(), str(cm.exception)):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(CID, text)
            self.assertNotIn("eyJhbGciOiJIUzUxMiJ9", text)

    def test_non_jwt_secret_echoed_in_a_rejection_is_redacted(self):
        plain = Credentials("CLIENT77777", "plain-opaque-token-77777777")
        t = MockTransport({}, script={"/optionchain": [resp(400, {"errorCode": "DH-905",
                                      "errorMessage": "bad field; token plain-opaque-token-77777777 client CLIENT77777"})]})
        out = io.StringIO()
        red = Redactor()
        c = DhanClient(plain, transport=t, redactor=red, logger=JsonLogger(red, stream=out), sleep=lambda s: None)
        with self.assertRaises(RequestRejectedError) as cm:
            c.option_chain("2026-09-24")
        for text in (out.getvalue(), str(cm.exception)):
            self.assertNotIn("plain-opaque-token-77777777", text)
            self.assertNotIn("CLIENT77777", text)
            self.assertIn("[REDACTED]", text)

    def test_network_error_text_is_not_logged(self):
        c, _, _, out = make(script={"/optionchain": [OSError("connect failed token=%s" % TOKEN)] * 4})
        with self.assertRaises(TransientError) as cm:
            c.option_chain("2026-09-24")
        self.assertNotIn(TOKEN, out.getvalue() + str(cm.exception))

    def test_repr_and_env_loading(self):
        self.assertNotIn(TOKEN, repr(CREDS))
        self.assertNotIn(CID, repr(CREDS))
        with self.assertRaises(AuthError) as cm:
            Credentials.from_environment({"DHAN_CLIENT_ID": CID})
        self.assertNotIn(CID, str(cm.exception))
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(TOKEN + "\n")
        try:
            c = Credentials.from_environment({"DHAN_CLIENT_ID": CID, "DHAN_TOKEN_FILE": f.name})
            self.assertEqual(c.access_token, TOKEN)
            self.assertEqual(c.token_expiry_ms(), 4102444800000)
        finally:
            os.unlink(f.name)
        self.assertIsNone(Credentials(CID, "not-a-jwt").token_expiry_ms())


if __name__ == "__main__":
    unittest.main()
