"""Authentication, hash-chained approval records and the persistent kill switch
(Phase 7.0C).

- Bearer API keys: a random 256-bit token is shown once, and only its SHA-256 is stored.
- Roles: viewer < approver < admin.
- Every human decision goes into `approval_records`, which is append-only (a database
  trigger blocks UPDATE/DELETE) and hash-chained (any edit breaks `verify()`).
- The kill switch is one row in `system_controls`. Every change to it is also an approval record.

CLI:
    python -m doi.core.auth create-user --email a@b.c
    python -m doi.core.auth create-key --email a@b.c --role approver --label laptop
    python -m doi.core.auth revoke --key-id KEY-…
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from doi.core.canonical import canonical
from doi.core.errors import DoiError

ROLES = {"viewer": 1, "approver": 2, "admin": 3}
GENESIS = "0" * 64


class AuthError(DoiError):
    pass


class ApprovalChainBroken(DoiError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int | None
    actor: str
    role: str
    key_id: str | None = None
    dev: bool = False  # in-memory development mode (no database): never used in production

    def has(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]


DEV_PRINCIPAL = Principal(None, "dev-mode", "admin", None, dev=True)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "doi_" + secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    seq: int
    created_at: datetime
    user_id: int | None
    actor: str
    role: str
    action: str
    subject_kind: str
    subject_id: str
    note: str | None
    payload: dict
    prev_hash: str
    hash: str


def _record_hash(
    prev: str, seq: int, created_at: datetime, actor: str, role: str, action: str, kind: str, sid: str, note, payload
) -> str:
    body = canonical(
        {
            "seq": seq,
            "at": created_at,
            "actor": actor,
            "role": role,
            "action": action,
            "kind": kind,
            "id": sid,
            "note": note,
            "payload": payload,
        }
    )
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuthStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def create_user(self, email: str, display_name: str | None = None) -> int:
        with psycopg.connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO users (email, display_name) VALUES (%s, %s) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id",
                (email.lower(), display_name),
            ).fetchone()
            return row[0]

    def create_key(self, user_id: int, role: str, label: str | None = None) -> tuple[str, str]:
        if role not in ROLES:
            raise AuthError(f"unknown role {role}")
        token = new_token()
        key_id = "KEY-" + secrets.token_hex(6)
        with psycopg.connect(self.dsn) as c:
            c.execute(
                "INSERT INTO api_keys (id, user_id, key_hash, role, label) VALUES (%s, %s, %s, %s, %s)",
                (key_id, user_id, hash_token(token), role, label),
            )
        return key_id, token

    def revoke(self, key_id: str) -> None:
        with psycopg.connect(self.dsn) as c:
            c.execute("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,))

    def authenticate(self, token: str | None) -> Principal | None:
        if not token or not token.startswith("doi_") or len(token) > 200:
            return None
        with psycopg.connect(self.dsn) as c:
            row = c.execute(
                "UPDATE api_keys k SET last_used_at = now() FROM users u WHERE k.key_hash = %s AND k.revoked_at IS NULL"
                " AND u.id = k.user_id AND u.status = 'active' RETURNING k.user_id, u.email, k.role, k.id",
                (hash_token(token),),
            ).fetchone()
        return Principal(row[0], row[1], row[2], row[3]) if row else None


class ApprovalLog:
    """Append-only, hash-chained log of human decisions (PostgreSQL)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def append_tx(
        self, conn: psycopg.Connection, who: Principal, action: str, kind: str, sid: str, note: str | None, payload: dict
    ) -> ApprovalRecord:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (0x0D01_A001,))
        last = conn.execute("SELECT seq, hash FROM approval_records ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
        now = datetime.now(UTC)
        payload = json.loads(canonical(payload))
        h = _record_hash(prev, seq, now, who.actor, who.role, action, kind, sid, note, payload)
        conn.execute(
            "INSERT INTO approval_records (seq, created_at, user_id, actor, role, action, subject_kind, subject_id, note, payload, prev_hash, hash)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (seq, now, who.user_id, who.actor, who.role, action, kind, sid, note, Jsonb(payload), prev, h),
        )
        return ApprovalRecord(seq, now, who.user_id, who.actor, who.role, action, kind, sid, note, payload, prev, h)

    def append(
        self, who: Principal, action: str, kind: str, sid: str, note: str | None = None, payload: dict | None = None
    ) -> ApprovalRecord:
        with psycopg.connect(self.dsn) as c, c.transaction():
            return self.append_tx(c, who, action, kind, sid, note, payload or {})

    def records(self, kind: str | None = None, sid: str | None = None) -> list[ApprovalRecord]:
        q, args = (
            "SELECT seq, created_at, user_id, actor, role, action, subject_kind, subject_id, note, payload, prev_hash, hash FROM approval_records",
            [],
        )
        if kind:
            q += " WHERE subject_kind = %s" + (" AND subject_id = %s" if sid else "")
            args = [kind] + ([sid] if sid else [])
        with psycopg.connect(self.dsn) as c:
            return [
                ApprovalRecord(r[0], r[1].astimezone(UTC), *r[2:10], r[10].strip(), r[11].strip())
                for r in c.execute(q + " ORDER BY seq", args)
            ]

    def verify(self) -> int:
        prev = GENESIS
        recs = self.records()
        for i, r in enumerate(recs, start=1):
            if (
                r.seq != i
                or r.prev_hash != prev
                or r.hash
                != _record_hash(
                    prev, r.seq, r.created_at, r.actor, r.role, r.action, r.subject_kind, r.subject_id, r.note, r.payload
                )
            ):
                raise ApprovalChainBroken(f"approval chain broken at seq {r.seq}")
            prev = r.hash
        return len(recs)


class MemoryApprovalLog:
    """Development-mode stand-in (no database). Same interface, same hashing."""

    def __init__(self) -> None:
        self._recs: list[ApprovalRecord] = []
        self._lock = threading.Lock()

    def append(
        self, who: Principal, action: str, kind: str, sid: str, note: str | None = None, payload: dict | None = None
    ) -> ApprovalRecord:
        with self._lock:
            seq = len(self._recs) + 1
            prev = self._recs[-1].hash if self._recs else GENESIS
            now = datetime.now(UTC)
            p = json.loads(canonical(payload or {}))
            r = ApprovalRecord(seq, now, who.user_id, who.actor, who.role, action, kind, sid, note, p, prev,
                               _record_hash(prev, seq, now, who.actor, who.role, action, kind, sid, note, p))  # fmt: skip
            self._recs.append(r)
            return r

    def records(self, kind: str | None = None, sid: str | None = None) -> list[ApprovalRecord]:
        return [r for r in self._recs if (kind is None or r.subject_kind == kind) and (sid is None or r.subject_id == sid)]

    def verify(self) -> int:
        return len(self._recs)


class ControlStore:
    def __init__(self, dsn: str, approvals: ApprovalLog) -> None:
        self.dsn, self.approvals = dsn, approvals

    def kill_switch(self) -> bool:
        with psycopg.connect(self.dsn) as c:
            row = c.execute("SELECT value FROM system_controls WHERE key = 'kill_switch'").fetchone()
        return bool(row and row[0].get("on"))

    def set_kill_switch(self, on: bool, who: Principal, note: str | None = None) -> ApprovalRecord:
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute(
                "UPDATE system_controls SET value = %s, updated_by = %s, updated_at = now() WHERE key = 'kill_switch'",
                (Jsonb({"on": on}), who.user_id),
            )
            return self.approvals.append_tx(
                c, who, "KILL_SWITCH_ON" if on else "KILL_SWITCH_OFF", "system", "kill_switch", note, {"on": on}
            )

    def get(self, key: str, default: Any = None) -> Any:
        with psycopg.connect(self.dsn) as c:
            row = c.execute("SELECT value FROM system_controls WHERE key = %s", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: Any, who: Principal, action: str, note: str | None = None) -> None:
        with psycopg.connect(self.dsn) as c, c.transaction():
            c.execute(
                "INSERT INTO system_controls (key, value, updated_by) VALUES (%s, %s, %s)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
                (key, Jsonb(value), who.user_id),
            )
            self.approvals.append_tx(c, who, action, "system", key, note, {"value": value})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doi.core.auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("create-user")
    u.add_argument("--email", required=True)
    k = sub.add_parser("create-key")
    k.add_argument("--email", required=True)
    k.add_argument("--role", required=True, choices=sorted(ROLES))
    k.add_argument("--label")
    r = sub.add_parser("revoke")
    r.add_argument("--key-id", required=True)
    args = ap.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    store = AuthStore(dsn)
    if args.cmd == "create-user":
        print(store.create_user(args.email))
    elif args.cmd == "create-key":
        key_id, token = store.create_key(store.create_user(args.email), args.role, args.label)
        print(f"key id: {key_id}\ntoken (shown once, store it securely): {token}")
    else:
        store.revoke(args.key_id)
        print("revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
