from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol

from .errors import StateConflictError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class Protector(Protocol):
    def protect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes: ...
    def unprotect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes: ...


class PlaintextTestProtector:
    """Test-only reversible protector. Production uses authenticated encryption."""

    def protect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes:
        return data

    def unprotect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes:
        return data


@dataclass(frozen=True, slots=True)
class AuthSnapshot:
    generation: int
    credential_revision: int
    account_id: str | None
    logged_in: bool
    refresh_inflight: bool


@dataclass(frozen=True, slots=True)
class Draft:
    id: str
    account_id: str
    auth_generation: int
    vacancy_id: str
    resume_id: str
    message: str
    message_hash: str
    summary: dict[str, object]
    status: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Operation:
    id: str
    draft_id: str
    account_id: str
    vacancy_id: str
    status: str


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS auth_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    generation INTEGER NOT NULL,
    credential_revision INTEGER NOT NULL DEFAULT 0,
    account_id TEXT,
    logged_in INTEGER NOT NULL,
    refresh_inflight INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO auth_state(singleton, generation, credential_revision, account_id, logged_in, refresh_inflight, updated_at)
VALUES(1, 0, 0, NULL, 0, 0, '1970-01-01T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    auth_generation INTEGER NOT NULL,
    vacancy_id TEXT NOT NULL,
    resume_id TEXT NOT NULL,
    message_cipher BLOB NOT NULL,
    message_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('prepared', 'expired', 'sending', 'sent', 'failed', 'unknown')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_owner_target ON drafts(account_id, vacancy_id);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(id),
    account_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('sending', 'sent', 'failed', 'unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    response_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_operation_per_target
ON operations(account_id, vacancy_id)
WHERE status IN ('sending', 'sent', 'unknown');
"""


class StateStore:
    def __init__(self, path: Path, protector: Protector) -> None:
        self.path = path
        self.protector = protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def auth_snapshot(self) -> AuthSnapshot:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM auth_state WHERE singleton = 1").fetchone()
        return AuthSnapshot(
            generation=row["generation"],
            credential_revision=row["credential_revision"],
            account_id=row["account_id"],
            logged_in=bool(row["logged_in"]),
            refresh_inflight=bool(row["refresh_inflight"]),
        )

    def replace_auth_state(
        self, *, account_id: str | None, logged_in: bool, refresh_inflight: bool = False
    ) -> int:
        now = iso(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM auth_state WHERE singleton = 1"
            ).fetchone()
            generation = int(row[0]) + 1
            connection.execute(
                "UPDATE auth_state SET generation=?, credential_revision=0, account_id=?, logged_in=?, refresh_inflight=?, updated_at=? WHERE singleton=1",
                (generation, account_id, int(logged_in), int(refresh_inflight), now),
            )
            connection.commit()
        return generation

    def set_refresh_inflight(self, expected_generation: int, expected_revision: int) -> None:
        now = iso(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation, credential_revision, logged_in, refresh_inflight FROM auth_state WHERE singleton=1"
            ).fetchone()
            if (
                row["generation"] != expected_generation
                or row["credential_revision"] != expected_revision
                or not row["logged_in"]
                or row["refresh_inflight"]
            ):
                connection.rollback()
                raise StateConflictError("Authentication changed while refresh was starting")
            connection.execute(
                "UPDATE auth_state SET refresh_inflight=1, updated_at=? WHERE singleton=1",
                (now,),
            )
            connection.commit()

    def finish_refresh(self, expected_generation: int, expected_revision: int) -> int:
        now = iso(utc_now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation, credential_revision, logged_in, refresh_inflight FROM auth_state WHERE singleton=1"
            ).fetchone()
            if (
                row["generation"] != expected_generation
                or row["credential_revision"] != expected_revision
                or not row["logged_in"]
                or not row["refresh_inflight"]
            ):
                connection.rollback()
                raise StateConflictError("Authentication changed before refreshed credentials were saved")
            revision = expected_revision + 1
            connection.execute(
                "UPDATE auth_state SET credential_revision=?, refresh_inflight=0, updated_at=? WHERE singleton=1",
                (revision, now),
            )
            connection.commit()
        return revision

    def create_draft(
        self,
        *,
        account_id: str,
        auth_generation: int,
        vacancy_id: str,
        resume_id: str,
        message: str,
        summary: dict[str, object],
        ttl: timedelta = timedelta(hours=24),
        now: datetime | None = None,
    ) -> Draft:
        created = now or utc_now()
        draft_id = uuid.uuid4().hex
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        cipher = self.protector.protect(
            message.encode("utf-8"), entropy=f"draft:{draft_id}".encode("ascii")
        )
        expires = created + ttl
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO drafts
                (id, account_id, auth_generation, vacancy_id, resume_id, message_cipher,
                 message_hash, summary_json, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    draft_id,
                    account_id,
                    auth_generation,
                    vacancy_id,
                    resume_id,
                    cipher,
                    digest,
                    json.dumps(summary, ensure_ascii=False),
                    iso(created),
                    iso(expires),
                ),
            )
        return self.get_draft(draft_id, now=created)

    def get_draft(self, draft_id: str, *, now: datetime | None = None) -> Draft:
        current = now or utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StateConflictError("Draft not found")
            expires = datetime.fromisoformat(row["expires_at"])
            status = row["status"]
            if status == "prepared" and current >= expires:
                status = "expired"
                connection.execute(
                    "UPDATE drafts SET status='expired', message_cipher=? WHERE id=?", (b"", draft_id)
                )
            connection.commit()
        message = ""
        if row["message_cipher"]:
            message = self.protector.unprotect(
                row["message_cipher"], entropy=f"draft:{draft_id}".encode("ascii")
            ).decode("utf-8")
        return Draft(
            id=row["id"], account_id=row["account_id"], auth_generation=row["auth_generation"],
            vacancy_id=row["vacancy_id"], resume_id=row["resume_id"], message=message,
            message_hash=row["message_hash"], summary=json.loads(row["summary_json"]),
            status=status, expires_at=expires,
        )

    def reserve_target(
        self, *, draft_id: str, account_id: str, current_generation: int, now: datetime | None = None
    ) -> Operation:
        current = now or utc_now()
        operation_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise StateConflictError("Draft not found")
            if row["account_id"] != account_id:
                connection.rollback()
                raise StateConflictError("Draft belongs to another HH account")
            if row["auth_generation"] != current_generation:
                connection.rollback()
                raise StateConflictError(
                    "Authentication lifecycle changed after this draft was prepared; prepare it again"
                )
            if row["status"] != "prepared":
                connection.rollback()
                raise StateConflictError(f"Draft cannot be sent from state {row['status']}")
            if current >= datetime.fromisoformat(row["expires_at"]):
                connection.execute("UPDATE drafts SET status='expired' WHERE id=?", (draft_id,))
                connection.commit()
                raise StateConflictError("Draft has expired")
            try:
                connection.execute(
                    """INSERT INTO operations
                    (id, draft_id, account_id, vacancy_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'sending', ?, ?)""",
                    (operation_id, draft_id, account_id, row["vacancy_id"], iso(current), iso(current)),
                )
            except sqlite3.IntegrityError as exc:
                blocker = connection.execute(
                    """SELECT id, draft_id, status FROM operations
                    WHERE account_id=? AND vacancy_id=? AND status IN ('sending','sent','unknown')""",
                    (account_id, row["vacancy_id"]),
                ).fetchone()
                connection.rollback()
                details = dict(blocker) if blocker else None
                raise StateConflictError(
                    "Another unresolved or completed operation already reserves this account and vacancy",
                    details=details,
                ) from exc
            connection.execute("UPDATE drafts SET status='sending' WHERE id=?", (draft_id,))
            connection.commit()
        return Operation(operation_id, draft_id, account_id, row["vacancy_id"], "sending")

    def finish_operation(self, operation_id: str, status: str, response: dict[str, object] | None = None) -> None:
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid terminal operation status")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT draft_id, status FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["status"] != "sending":
                connection.rollback()
                raise StateConflictError("Operation is not in sending state")
            connection.execute(
                "UPDATE operations SET status=?, updated_at=?, response_json=? WHERE id=?",
                (status, iso(utc_now()), json.dumps(response) if response is not None else None, operation_id),
            )
            if status in {"sent", "failed"}:
                connection.execute(
                    "UPDATE drafts SET status=?, message_cipher=? WHERE id=?",
                    (status, b"", row["draft_id"]),
                )
            else:
                connection.execute("UPDATE drafts SET status=? WHERE id=?", (status, row["draft_id"]))
            connection.commit()

    def operation_for_target(self, account_id: str, vacancy_id: str) -> Operation | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, draft_id, account_id, vacancy_id, status FROM operations
                WHERE account_id=? AND vacancy_id=? AND status IN ('sending','sent','unknown')""",
                (account_id, vacancy_id),
            ).fetchone()
        return Operation(**dict(row)) if row else None

    def operation_for_draft(self, draft_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, draft_id, account_id, vacancy_id, status, created_at, updated_at, response_json
                FROM operations WHERE draft_id=? ORDER BY created_at DESC LIMIT 1""",
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("response_json")
        result["response"] = json.loads(raw) if raw else None
        return result

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            drafts = connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
            operations = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        return {"drafts": drafts, "operations": operations}
