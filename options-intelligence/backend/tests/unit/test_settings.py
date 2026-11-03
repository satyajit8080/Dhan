"""Settings page: write-only secrets, audit without values, API key lifecycle, dev-mode refusal."""

import stat

from fastapi.testclient import TestClient

from doi.app.main import create_app
from doi.app.runtime import Runtime
from doi.core.config import Settings
from doi.core.secret_store import SecretStore
from doi.data.dhan.token import TokenSource
from tests.unit.test_system_e2e import system

TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.abcdefghijklmnopqrstuvwx"


def _rt(pg_dsn, tmp_path):
    rt, c, k = system(pg_dsn)
    rt.settings = Settings(secrets_dir=tmp_path / "secrets")
    return rt, c, k


def test_secrets_write_only_and_audited(pg_dsn, tmp_path):
    rt, c, k = _rt(pg_dsn, tmp_path)
    assert c.get("/api/v1/admin/settings", headers=k["approver"]).status_code == 403
    assert c.put("/api/v1/admin/settings/xai_api_key", json={"value": "not-a-key"}, headers=k["admin"]).status_code == 422
    assert c.put("/api/v1/admin/settings/nope", json={"value": "x"}, headers=k["admin"]).status_code == 404
    r = c.put("/api/v1/admin/settings/dhan_access_token", json={"value": TOKEN}, headers=k["admin"])
    assert r.status_code == 200 and r.json()["fingerprint"].startswith("sha256:")
    p = tmp_path / "secrets" / "dhan.token"
    assert p.read_text().strip() == TOKEN and stat.S_IMODE(p.stat().st_mode) == 0o600
    st = c.get("/api/v1/admin/settings", headers=k["admin"])
    assert TOKEN not in st.text
    item = next(i for i in st.json()["items"] if i["name"] == "dhan_access_token")
    assert item["configured"] and item["source"] in ("file", "env")
    recs = [x for x in rt.approvals.records() if x.subject_kind == "settings"]
    assert recs and recs[-1].action == "SECRET_SET" and TOKEN not in str(recs[-1].payload)
    assert c.delete("/api/v1/admin/settings/dhan_access_token", headers=k["admin"]).json()["removed"] is True
    assert not p.exists()


def test_token_rotation_goes_to_watched_file_live(pg_dsn, tmp_path):
    rt, c, k = _rt(pg_dsn, tmp_path)
    watched = tmp_path / "other" / "dhan.token"
    rt.token = TokenSource(path=watched)
    r = c.put("/api/v1/admin/settings/dhan_access_token", json={"value": TOKEN}, headers=k["admin"]).json()
    assert r["applied"] == "live" and rt.token.get() == TOKEN
    assert not (tmp_path / "secrets" / "dhan.token").exists()


def test_api_key_lifecycle_and_last_admin_guard(pg_dsn, tmp_path):
    _, c, k = _rt(pg_dsn, tmp_path)
    r = c.post("/api/v1/admin/api-keys", json={"email": "phone@x.in", "role": "viewer", "label": "phone"}, headers=k["admin"])
    assert r.status_code == 201 and r.json()["token"].startswith("doi_")
    new = {"Authorization": "Bearer " + r.json()["token"]}
    assert c.get("/api/v1/auth/whoami", headers=new).json()["role"] == "viewer"
    keys = c.get("/api/v1/admin/api-keys", headers=k["admin"]).json()
    assert all("token" not in x and "key_hash" not in x for x in keys)
    assert c.post(f"/api/v1/admin/api-keys/{r.json()['id']}/revoke", headers=k["admin"]).status_code == 200
    assert c.get("/api/v1/auth/whoami", headers=new).status_code == 401
    admin_id = next(x["id"] for x in keys if x["role"] == "admin" and x["revoked_at"] is None)
    assert c.post(f"/api/v1/admin/api-keys/{admin_id}/revoke", headers=k["admin"]).status_code == 409


def test_settings_refused_in_dev_mode():
    c = TestClient(create_app(runtime=Runtime(configs={})))
    assert c.get("/api/v1/admin/settings").status_code == 403
    assert c.put("/api/v1/admin/settings/xai_api_key", json={"value": "xai-" + "a" * 30}).status_code == 403


def test_from_env_reads_saved_files_env_wins(tmp_path):
    st = SecretStore(tmp_path)
    st.write("dhan_client_id", "1100123456")
    st.write("xai_api_key", "xai-" + "b" * 30)
    s = Settings.from_env({"SECRETS_DIR": str(tmp_path)})
    assert s.dhan_client_id == "1100123456" and s.xai_api_key.get_secret_value() == "xai-" + "b" * 30
    assert s.dhan_token_file == tmp_path / "dhan.token"
    s2 = Settings.from_env({"SECRETS_DIR": str(tmp_path), "DHAN_CLIENT_ID": "999999"})
    assert s2.dhan_client_id == "999999"
