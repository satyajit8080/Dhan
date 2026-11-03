"""Settings page API: integration secrets and dashboard API keys (admin only).

Rules: values are write-only (never returned, never logged); every change is recorded in the
hash-chained approval log without the value; refused entirely in in-memory dev mode (no auth).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, SecretStr

from doi.app.security import requires
from doi.core import setup_store
from doi.core.auth import ROLES, Principal
from doi.core.secret_store import SPECS, SecretError, SecretStore, fingerprint

router = APIRouter(prefix="/api/v1/admin")


class SecretIn(BaseModel):
    value: SecretStr = Field(min_length=1, max_length=4096)
    note: str | None = Field(default=None, max_length=500)


class ApiKeyIn(BaseModel):
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    role: str
    label: str | None = Field(default=None, max_length=100)


def _guard(request: Request):
    rt = request.app.state.runtime
    if rt.auth is None:
        raise HTTPException(403, "settings require database-backed authentication (DATABASE_URL); refused in dev mode")
    return rt


def _store(rt) -> SecretStore:
    s = rt.settings
    return SecretStore(Path(s.secrets_dir) if s is not None else Path(os.environ.get("SECRETS_DIR", "./secrets")))


def _audit(rt, who: Principal, action: str, sid: str, note: str | None, payload: dict) -> None:
    rt.approvals.append(who, action, "settings", sid, note, payload)


def _status(rt) -> dict:
    st = _store(rt)
    items = [st.status(n) for n in SPECS]
    tok = rt.token.peek_status() if rt.token else None
    for it in items:
        if it["name"] == "dhan_access_token" and tok is not None:
            it["token_state"], it["expires_at"] = tok.state, tok.expires_at
        if it["name"] == "xai_api_key":
            it["ai_live"] = bool(getattr(rt, "ai", None) and rt.ai.live)
        if it["name"] == "dhan_client_id":
            it["market_data_active"] = rt.provider is not None
    return {"secrets_dir_mode": "0700/0600 files on the server", "items": items,
            "restart_hint": "Dhan client ID, first-time Grok key and AI_ENABLED changes apply after restarting the api and ai-worker services"}  # fmt: skip


@router.get("/settings")
async def settings_status(request: Request, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    return jsonable_encoder(_status(rt))


@router.put("/settings/{name}")
async def set_secret(request: Request, name: str, body: SecretIn, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    if name not in SPECS:
        raise HTTPException(404, "unknown setting")
    value = body.value.get_secret_value().strip()
    store = _store(rt)
    try:
        store.write(name, value)
        applied = "restart_required"
        if name == "dhan_access_token" and rt.token is not None and getattr(rt.token, "_path", None):
            watched = Path(rt.token._path)
            if watched.resolve() != store.path(name).resolve():  # keep one source: the file the running TokenSource watches
                store.delete(name)
                watched.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_to(watched, value)
            applied = "live"
        if name == "xai_api_key" and getattr(rt, "ai", None) is not None:
            client = rt.ai.gateway.client
            if rt.ai.live and hasattr(client, "set_key"):
                client.set_key(SecretStr(value))
                applied = "live"
    except SecretError as e:
        raise HTTPException(422, str(e)) from None
    env_override = bool(os.environ.get(SPECS[name].env))
    await asyncio.to_thread(
        _audit, rt, who, "SECRET_SET", name, body.note, {"fingerprint": fingerprint(value), "applied": applied}
    )
    return {"name": name, "fingerprint": fingerprint(value), "applied": applied, "env_overrides_file": env_override}


def _write_to(path: Path, value: str) -> None:
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@router.delete("/settings/{name}")
async def delete_secret(request: Request, name: str, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    if name not in SPECS:
        raise HTTPException(404, "unknown setting")
    removed = _store(rt).delete(name)
    await asyncio.to_thread(_audit, rt, who, "SECRET_DELETE", name, None, {"removed": removed})
    return {"name": name, "removed": removed}


@router.post("/settings/xai_api_key/test")
async def test_xai(request: Request, who: Principal = Depends(requires("admin"))):
    """Free check: GET /v1/models with the stored key. No chat call, no budget spend."""
    rt = _guard(request)
    from doi.llm.gateway import XaiClient

    key = os.environ.get("XAI_API_KEY") or _store(rt).read("xai_api_key")
    if not key:
        raise HTTPException(409, "no Grok key configured")
    base = rt.settings.xai_base_url if rt.settings is not None else "https://api.x.ai/v1"
    try:
        models = await XaiClient(SecretStr(key), base, timeout_s=15).list_models()
    except Exception as e:  # never echo provider bodies (may contain the key)
        return {"ok": False, "error": type(e).__name__ + (f" {e.response.status_code}" if hasattr(e, "response") else "")}
    return {"ok": True, "models": sorted(m.get("id") or m.get("name") or "?" for m in models)[:50]}


# ------------------------------------------------------------------ dashboard API keys


def _keys(dsn: str) -> list[dict]:
    with psycopg.connect(dsn, connect_timeout=3) as c:
        rows = c.execute(
            "SELECT k.id, u.email, k.role, k.label, k.created_at, k.last_used_at, k.revoked_at FROM api_keys k"
            " JOIN users u ON u.id = k.user_id ORDER BY k.created_at DESC LIMIT 500"
        ).fetchall()
    return [dict(zip(("id", "email", "role", "label", "created_at", "last_used_at", "revoked_at"), r, strict=True)) for r in rows]


@router.get("/api-keys")
async def list_keys(request: Request, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    return jsonable_encoder(await asyncio.to_thread(_keys, rt.auth.dsn))


@router.post("/api-keys", status_code=201)
async def create_key(request: Request, body: ApiKeyIn, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    if body.role not in ROLES:
        raise HTTPException(422, f"role must be one of {sorted(ROLES)}")
    uid = await asyncio.to_thread(rt.auth.create_user, body.email)
    key_id, token = await asyncio.to_thread(rt.auth.create_key, uid, body.role, body.label)
    await asyncio.to_thread(
        _audit, rt, who, "API_KEY_CREATE", key_id, body.label, {"email": body.email.lower(), "role": body.role}
    )
    return {"id": key_id, "token": token, "shown_once": True}


@router.post("/api-keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: str, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    keys = await asyncio.to_thread(_keys, rt.auth.dsn)
    target = next((k for k in keys if k["id"] == key_id), None)
    if target is None or target["revoked_at"] is not None:
        raise HTTPException(404, "active key not found")
    admins = [k for k in keys if k["role"] == "admin" and k["revoked_at"] is None]
    if target["role"] == "admin" and len(admins) <= 1:
        raise HTTPException(409, "refusing to revoke the last active admin key")
    await asyncio.to_thread(rt.auth.revoke, key_id)
    await asyncio.to_thread(_audit, rt, who, "API_KEY_REVOKE", key_id, None, {"self": key_id == who.key_id})
    return {"revoked": key_id}


# ------------------------------------------------------------------ setup wizard

STARTED_AT = time.time()


class SetupIn(BaseModel):
    ai_enabled: bool | None = None
    dhan_feed_enabled: bool | None = None
    live_indices: list[str] | None = Field(default=None, max_length=5)
    note: str | None = Field(default=None, max_length=500)


def _secrets_dir(rt) -> Path:
    return _store(rt).dir


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _checklist(rt) -> dict:
    s = rt.settings
    st = _status(rt)
    items = {i["name"]: i for i in st["items"]}
    saved = setup_store.load(_secrets_dir(rt))
    tok = items["dhan_access_token"]
    live_now = sorted(k for k, c in rt.configs.items() if c.live_enabled)
    ai = getattr(rt, "ai", None)
    watched = [setup_store.path(_secrets_dir(rt)), _store(rt).path("dhan_client_id"), _store(rt).path("xai_api_key")]
    restart_pending = any((m := _mtime(p)) is not None and m > STARTED_AT for p in watched)
    steps = [
        {"id": "admin", "title": "Admin login key", "done": True, "detail": "you are signed in as admin"},
        {"id": "dhan_client_id", "title": "Dhan client ID", "done": items["dhan_client_id"]["configured"], "detail": items["dhan_client_id"]["fingerprint"]},
        {"id": "dhan_access_token", "title": "Dhan access token", "done": tok["configured"] and tok.get("token_state") in (None, "valid", "expiring", "unknown_expiry"),
         "detail": tok.get("token_state") or ("saved" if tok["configured"] else "missing")},
        {"id": "indices", "title": "Indices to track", "done": bool(live_now), "detail": ", ".join(live_now) or "none enabled"},
        {"id": "market_data", "title": "Market data running", "done": rt.provider is not None, "detail": "connected" if rt.provider is not None else "needs client ID + token, then restart"},  # noqa: E501
        {"id": "xai_api_key", "title": "Grok API key", "done": items["xai_api_key"]["configured"], "detail": items["xai_api_key"]["fingerprint"]},
        {"id": "ai", "title": "AI committee enabled", "done": bool(ai and ai.live), "detail": "live" if ai and ai.live else "off (enable below, then restart)"},
        {"id": "feed", "title": "Live WebSocket feed (optional)", "done": rt.collector is not None, "detail": rt.collector.status.state if rt.collector is not None else "off", "optional": True},  # noqa: E501
    ]  # fmt: skip
    return {
        "steps": steps,
        "saved": saved,
        "effective": {
            "ai_enabled": bool(s and s.ai_enabled),
            "dhan_feed_enabled": bool(s and s.dhan_feed_enabled),
            "live_indices": live_now,
        },
        "env_locked": {
            k: bool(os.environ.get(e)) for k, e in (("ai_enabled", "AI_ENABLED"), ("dhan_feed_enabled", "DHAN_FEED_ENABLED"))
        },
        "universe": list(setup_store.UNIVERSE),
        "restart_pending": restart_pending,
        "started_at": STARTED_AT,
        "budget_cap_inr": float(s.llm_monthly_limit_inr) if s else 3000.0,
    }


@router.get("/setup")
async def setup_status(request: Request, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    return jsonable_encoder(_checklist(rt))


@router.put("/setup")
async def setup_save(request: Request, body: SetupIn, who: Principal = Depends(requires("admin"))):
    rt = _guard(request)
    if body.live_indices is not None:
        bad = [x for x in body.live_indices if x not in setup_store.UNIVERSE]
        if bad:
            raise HTTPException(422, f"unknown indices {bad}")
    changes = body.model_dump(exclude={"note"}, exclude_none=True)
    saved = await asyncio.to_thread(setup_store.save, _secrets_dir(rt), changes)
    await asyncio.to_thread(_audit, rt, who, "SETUP_CHANGE", "setup", body.note, changes)
    return {"saved": saved, "applied": "restart_required"}


@router.post("/restart")
async def restart(request: Request, who: Principal = Depends(requires("admin"))):
    """Graceful self-restart so saved keys and setup apply. Needs a supervisor (docker `restart: unless-stopped`)."""
    import signal

    rt = _guard(request)
    await asyncio.to_thread(_audit, rt, who, "RESTART", "api", None, {"pid": os.getpid()})
    asyncio.get_running_loop().call_later(1.0, os.kill, os.getpid(), signal.SIGTERM)
    return {"restarting": True, "eta_s": 15}
