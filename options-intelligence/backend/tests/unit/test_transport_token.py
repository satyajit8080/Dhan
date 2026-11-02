import asyncio
import base64
import inspect
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from doi.core.errors import DhanRequestError, ForbiddenEndpoint, RateLimited, TokenRejected, UpstreamError, UpstreamTimeout
from doi.core.logging import RedactingFilter
from doi.data.dhan import adapter as adapter_mod
from doi.data.dhan import transport as transport_mod
from doi.data.dhan.token import TokenSource, inspect_token
from doi.data.dhan.transport import READ_ONLY_ENDPOINTS, DhanTransport

TOKEN = "tok-secret-abcd1234"


def make(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DhanTransport(base_url="https://api.test/v2", client_id="cid", token=lambda: TOKEN, client=client)


def run(coro):
    return asyncio.run(coro)


def test_sends_auth_headers_and_body():
    seen = {}

    def h(req):
        seen["headers"], seen["body"], seen["url"] = req.headers, json.loads(req.content), str(req.url)
        return httpx.Response(200, json={"data": [], "status": "success"})

    run(make(h).post("/optionchain/expirylist", {"UnderlyingScrip": 13}))
    assert seen["headers"]["access-token"] == TOKEN and seen["headers"]["client-id"] == "cid"
    assert seen["url"] == "https://api.test/v2/optionchain/expirylist" and seen["body"] == {"UnderlyingScrip": 13}


@pytest.mark.parametrize("path", ["/orders", "/super/orders", "/positions", "/fundlimit", "/margincalculator", "/killswitch"])
def test_non_allowlisted_paths_refused_before_network(path):
    def h(req):
        raise AssertionError("network must not be touched")

    with pytest.raises(ForbiddenEndpoint):
        run(make(h).post(path, {}))


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (429, {}, RateLimited),
        (200, {"errorCode": "DH-904"}, RateLimited),
        (401, {}, TokenRejected),
        (400, {"errorType": "Invalid_Authentication", "errorCode": "DH-901"}, TokenRejected),
        (500, {}, UpstreamError),
        (400, {"errorCode": "DH-905"}, DhanRequestError),
        (200, {"status": "failure", "remarks": {"error_code": "DH-907"}}, DhanRequestError),
    ],
)
def test_error_classification(status, body, exc):
    with pytest.raises(exc):
        run(make(lambda r: httpx.Response(status, json=body, headers={"retry-after": "2"})).post("/optionchain", {}))


def test_timeout_classified():
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(UpstreamTimeout):
        run(make(h).post("/optionchain", {}))


def test_no_order_capability_reachable():
    assert not any(
        p.startswith(("/orders", "/super", "/forever", "/positions", "/holdings", "/fund", "/margin"))
        for p in READ_ONLY_ENDPOINTS
    )
    banned = ("order", "place", "modify", "cancel", "position", "margin", "fund")
    for mod in (transport_mod, adapter_mod):
        for name, obj in inspect.getmembers(mod, inspect.isclass):
            if obj.__module__ == mod.__name__:
                assert not [m for m in dir(obj) if any(b in m.lower() for b in banned)], name
    src = inspect.getsource(transport_mod) + inspect.getsource(adapter_mod)
    assert "import dhanhq" not in src and "from dhanhq" not in src


def _jwt(exp: datetime) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS512'})}.{b({'exp': int(exp.timestamp()), 'dhanClientId': 'x'})}.c2lnbmF0dXJlLXZhbHVl"


def test_token_states():
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert inspect_token(None, now).state == "missing"
    assert inspect_token("opaque-token", now).state == "unknown_expiry"
    assert inspect_token(_jwt(now + timedelta(hours=5)), now).state == "valid"
    assert inspect_token(_jwt(now + timedelta(minutes=20)), now).state == "expiring"
    st = inspect_token(_jwt(now - timedelta(seconds=1)), now)
    assert st.state == "expired" and st.fingerprint.startswith("…") and len(st.fingerprint) == 5


def test_token_source_file_rotation_and_expiry(tmp_path):
    p = tmp_path / "dhan.token"
    src = TokenSource(path=p)
    with pytest.raises(TokenRejected):
        src.get()
    good = _jwt(datetime.now(UTC) + timedelta(hours=10))
    p.write_text(good + "\n")
    assert src.get() == good
    import os

    p.write_text(_jwt(datetime.now(UTC) - timedelta(hours=1)))
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    with pytest.raises(TokenRejected, match="expired"):
        src.get()


def test_log_redaction():
    flt = RedactingFilter([TOKEN])
    jwt = _jwt(datetime.now(UTC))
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "t=%s jwt=%s access-token: zzzz9999 pin=1234", (TOKEN, jwt), None)
    flt.filter(rec)
    assert TOKEN not in rec.msg and jwt not in rec.msg and "zzzz9999" not in rec.msg and "1234" not in rec.msg
