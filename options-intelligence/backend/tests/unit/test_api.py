from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from doi.app.main import create_app
from doi.app.runtime import Runtime
from doi.core.config import DEFAULT_STRATEGY_DIR
from doi.core.errors import TokenRejected
from doi.data.dhan.normalize import normalize_option_chain
from doi.data.instruments.master import InstrumentMaster
from doi.intelligence.strategy_configs.loader import load_index_configs
from tests.fixtures import E1, E2, chain_payload, master_csv


class FakeProvider:
    def __init__(self, age_s=0.0, fail=None):
        self.age_s, self.fail = age_s, fail

    async def get_expiries(self, u):
        if self.fail:
            raise self.fail
        return (E1, E2)

    async def get_option_chain(self, u, expiry, *, max_age_s=None):
        if self.fail:
            raise self.fail
        t = datetime.now(UTC) - timedelta(seconds=self.age_s)
        return normalize_option_chain(
            chain_payload(),
            underlying_key=u.key,
            expiry=expiry,
            request_key="k",
            requested_at=t,
            received_at=t,
            master_version=u.master_version,
        )


def client(provider=None, enable=("NIFTY",)):
    cfgs = load_index_configs(DEFAULT_STRATEGY_DIR)
    cfgs = {k: (c.model_copy(update={"live_enabled": True}) if k in enable else c) for k, c in cfgs.items()}
    rt = Runtime(configs=cfgs, provider=provider or FakeProvider())
    rt.resolve(InstrumentMaster.parse(master_csv()))
    return TestClient(create_app(runtime=rt))


def test_health_paper_only():
    r = client().get("/healthz").json()
    assert r["paper_only"] is True and r["master_version"]


def test_underlyings_lists_five_with_status():
    rows = client().get("/api/v1/market/underlyings").json()
    assert {r["key"] for r in rows} == {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX"}
    assert all(r["resolved"] and r["expiries"] for r in rows)


def test_disabled_index_refused():
    r = client().get("/api/v1/market/expiries", params={"underlying": "SENSEX"})
    assert r.status_code == 409


def test_option_chain_with_quality():
    r = client().get("/api/v1/market/option-chain", params={"underlying": "nifty", "expiry": E1.isoformat()})
    assert r.status_code == 200
    b = r.json()
    assert b["quality"]["is_stale"] is False and b["contract"]["lot_size"] == 75 and len(b["rows"]) == 2


def test_stale_chain_flagged():
    b = (
        client(FakeProvider(age_s=60))
        .get("/api/v1/market/option-chain", params={"underlying": "NIFTY", "expiry": E1.isoformat()})
        .json()
    )
    assert b["quality"]["is_stale"] is True


def test_unknown_expiry_404_and_token_error_503():
    c = client()
    assert c.get("/api/v1/market/option-chain", params={"underlying": "NIFTY", "expiry": "2030-01-01"}).status_code == 404
    r = client(FakeProvider(fail=TokenRejected("x"))).get("/api/v1/market/expiries", params={"underlying": "NIFTY"})
    assert r.status_code == 503 and "re-authenticate" in r.json()["detail"]


def test_no_order_routes():
    paths = {r.path for r in create_app(runtime=Runtime(configs={})).routes}
    assert not any(("order" in p and "paper" not in p) for p in paths)
