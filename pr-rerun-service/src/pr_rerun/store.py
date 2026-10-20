"""SQLite transactions for identity, confirmation, rate limits, and durable deduplication."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pr_rerun.models import Plan, Principal, ServiceError, positive_id


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        if path.is_symlink():
            raise ValueError("The database must not be a symbolic link")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Set permissions at creation, before any token hashes or plans are written.
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    github_id INTEGER PRIMARY KEY, login TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, actor INTEGER NOT NULL, token_hash TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL,
                    ticket_hash TEXT UNIQUE NOT NULL, plan TEXT NOT NULL,
                    state TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    key TEXT PRIMARY KEY, at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    request_id TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    state TEXT NOT NULL, message TEXT, updated REAL NOT NULL,
                    PRIMARY KEY (request_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS claims (
                    repository_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL, request_id TEXT NOT NULL,
                    PRIMARY KEY (repository_id, run_id, attempt)
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def issue_token(self, github_id: int, login: str) -> str:
        positive_id(github_id)
        token = f"prr_{secrets.token_urlsafe(32)}"
        with self.connection() as db:
            db.execute(
                "INSERT INTO users VALUES (?, ?, ?, 1) ON CONFLICT(github_id) DO UPDATE SET "
                "login=excluded.login, token_hash=excluded.token_hash, enabled=1",
                (github_id, login, digest(token)),
            )
        return token

    def revoke(self, github_id: int) -> bool:
        with self.connection() as db:
            return bool(
                db.execute("UPDATE users SET enabled=0 WHERE github_id=?", (github_id,)).rowcount
            )

    def users(self) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute("SELECT github_id, login, enabled FROM users ORDER BY login")
            ]

    def authenticate(self, authorization: str) -> Principal:
        match = re.fullmatch(r"Bearer (prr_[A-Za-z0-9_-]{43})", authorization)
        if match is None:
            raise ServiceError(401, "A valid service access token is required.")
        return self.authenticate_hash(digest(match[1]))

    def authenticate_hash(self, token_hash: str) -> Principal:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM users WHERE token_hash=? AND enabled=1", (token_hash,)
            ).fetchone()
        if row is None:
            raise ServiceError(401, "Service access token is invalid or revoked.")
        return Principal(row["github_id"], row["login"], row["token_hash"])

    def rate_limit(self, key: str, interval: int) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT at FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row is not None and self.clock() - row["at"] < interval:
                raise ServiceError(429, f"Wait {interval} seconds before another request.")
            db.execute("INSERT OR REPLACE INTO rate_limits VALUES (?, ?)", (key, self.clock()))

    def preview(self, principal: Principal, plan: Plan, ttl: int) -> dict:
        request_id, ticket = f"rr_{uuid.uuid4().hex}", secrets.token_urlsafe(32)
        expires = self.clock() + ttl
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE requests SET state='superseded' WHERE actor=? AND state='preview'",
                (principal.github_id,),
            )
            db.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, 'preview', NULL)",
                (
                    request_id,
                    principal.github_id,
                    principal.token_hash,
                    self.clock(),
                    expires,
                    digest(ticket),
                    plan.model_dump_json(),
                ),
            )
        return {
            "request_id": request_id,
            "ticket": ticket,
            "expires_at": expires,
            "confirmation": plan.confirmation,
            **plan.model_dump(mode="json"),
        }

    def claim(self, principal: Principal, ticket: str, confirmation: str) -> tuple[str, Plan, bool]:
        """Atomically consume a preview; a retry returns its original request, without writes."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM requests WHERE ticket_hash=? AND actor=?",
                (digest(ticket), principal.github_id),
            ).fetchone()
            if row is None or row["token_hash"] != principal.token_hash:
                raise ServiceError(404, "Confirmation does not belong to this credential.")
            plan = Plan.model_validate_json(row["plan"])
            if not hmac.compare_digest(confirmation.encode(), plan.confirmation.encode()):
                raise ServiceError(400, "Confirmation text does not match the preview.")
            if row["state"] == "superseded":
                raise ServiceError(409, "A newer preview replaced this confirmation.")
            if row["state"] != "preview":
                return row["id"], plan, False
            if row["expires"] <= self.clock():
                raise ServiceError(409, "Confirmation expired; request another preview.")
            db.execute("UPDATE requests SET state='validating' WHERE id=?", (row["id"],))
            return row["id"], plan, True

    def reserve(self, request_id: str, plan: Plan) -> None:
        """Reserve every run before the first POST; roll back the entire set on conflict."""
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for run in plan.runs:
                    db.execute(
                        "INSERT INTO claims VALUES (?, ?, ?, ?)",
                        (plan.pr.repository_id, run.run_id, run.attempt, request_id),
                    )
                    db.execute(
                        "INSERT INTO items VALUES (?, ?, ?, 'reserved', NULL, ?)",
                        (request_id, run.run_id, run.attempt, self.clock()),
                    )
                db.execute("UPDATE requests SET state='submitting' WHERE id=?", (request_id,))
        except sqlite3.IntegrityError as error:
            raise ServiceError(
                409, "A run attempt was already submitted or is awaiting inspection."
            ) from error

    def item(
        self,
        request_id: str,
        run_id: int,
        state: str,
        message: str | None = None,
        *,
        release: bool = False,
    ) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE items SET state=?, message=?, updated=? WHERE request_id=? AND run_id=?",
                (state, message, self.clock(), request_id, run_id),
            )
            if release:
                # Only release when we know the POST was never sent or rejected.
                db.execute(
                    "DELETE FROM claims WHERE request_id=? AND run_id=?", (request_id, run_id)
                )

    def finish(self, request_id: str, state: str, message: str | None = None) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE requests SET state=?, error=? WHERE id=?", (state, message, request_id)
            )

    def result(self, principal: Principal, request_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE id=? AND actor=?", (request_id, principal.github_id)
            ).fetchone()
            if row is None:
                raise ServiceError(404, "Request not found.")
            items = {
                item["run_id"]: dict(item)
                for item in db.execute(
                    "SELECT run_id, state, message FROM items WHERE request_id=?", (request_id,)
                )
            }
        plan = Plan.model_validate_json(row["plan"])
        return {
            "request_id": row["id"],
            "state": row["state"],
            "error": row["error"],
            "created_at": row["created"],
            "pr": plan.pr.model_dump(mode="json"),
            "runs": [
                {
                    **run.model_dump(mode="json"),
                    **items.get(run.run_id, {"state": "not_submitted", "message": None}),
                }
                for run in plan.runs
            ],
        }

    def pending_items(self, request_id: str) -> list[dict]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM items WHERE request_id=? AND state IN ('unknown', 'submitting')",
                    (request_id,),
                )
            ]

    def recover(self) -> int:
        """Called once before serving: interrupted requests are never automatically retried."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = list(
                db.execute("SELECT id FROM requests WHERE state IN ('validating', 'submitting')")
            )
            for row in rows:
                request_id = row["id"]
                reserved = list(
                    db.execute(
                        "SELECT run_id FROM items WHERE request_id=? AND state='reserved'",
                        (request_id,),
                    )
                )
                for item in reserved:
                    db.execute(
                        "DELETE FROM claims WHERE request_id=? AND run_id=?",
                        (request_id, item["run_id"]),
                    )
                db.execute(
                    "UPDATE items SET state='not_attempted', "
                    "message='Service restarted before POST' "
                    "WHERE request_id=? AND state='reserved'",
                    (request_id,),
                )
                db.execute(
                    "UPDATE items SET state='unknown', message='Service restarted during POST' "
                    "WHERE request_id=? AND state='submitting'",
                    (request_id,),
                )
                db.execute(
                    "UPDATE requests SET state='interrupted', error='Inspect individual results; "
                    "no automatic retry was performed.' WHERE id=?",
                    (request_id,),
                )
            return len(rows)
