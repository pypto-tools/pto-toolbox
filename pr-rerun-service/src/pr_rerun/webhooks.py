"""Signed comment commands with a durable inbox and the existing rerun checks."""

import hashlib
import hmac
import json
import logging
import os
import re
import stat
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

from pr_rerun.models import Principal, ServiceError, positive_id
from pr_rerun.policy import repository_name
from pr_rerun.registry import Registry
from pr_rerun.store import digest

BODY_LIMIT = 256 * 1024
MAX_AGE = 600
LOG = logging.getLogger("pr_rerun.webhook")


def read_secret(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_nlink != 1
        ):
            raise ValueError("Webhook secret must be an owner-only regular file")
        value = os.read(descriptor, 1025).strip()
        if not 32 <= len(value) <= 1024 or not re.fullmatch(rb"[A-Za-z0-9_-]+", value):
            raise ValueError("Webhook secret must contain 32-1024 URL-safe characters")
        return value
    finally:
        os.close(descriptor)


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timestamp timezone")
    return parsed.timestamp()


class CommentCommands:
    def __init__(self, registry: Registry, secret: bytes):
        self.registry, self.store, self.secret = registry, registry.store, secret
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        with self.store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS comment_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery TEXT UNIQUE NOT NULL, repository TEXT NOT NULL,
                repository_id INTEGER NOT NULL, installation_id INTEGER NOT NULL,
                pr INTEGER NOT NULL, comment_id INTEGER NOT NULL, actor INTEGER NOT NULL,
                login TEXT NOT NULL, created REAL NOT NULL, received REAL NOT NULL,
                state TEXT NOT NULL, request_id TEXT, message TEXT,
                UNIQUE(repository_id, comment_id)
            )""")

    def receive(self, body: bytes, signature: str, event: str, delivery: str) -> dict:
        if len(body) > BODY_LIMIT:
            raise ServiceError(413, "Webhook body is too large.")
        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature) or not hmac.compare_digest(
            signature, expected
        ):
            raise ServiceError(401, "Invalid webhook signature.")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise ServiceError(400, "Invalid webhook delivery identifier.")
        if event not in {"issue_comment", "ping"}:
            return {"state": "ignored"}
        try:
            value = json.loads(body)
            if event == "ping":
                return {"state": "pong"}
            if value["action"] != "created":
                return {"state": "ignored"}
            comment, issue = value["comment"], value["issue"]
            if not issue.get("pull_request") or comment["body"].strip() != "/ci rerun":
                return {"state": "ignored"}
            actor = positive_id(comment["user"]["id"])
            if (
                comment["user"].get("type") != "User"
                or value["sender"]["id"] != actor
                or value["sender"].get("type") != "User"
            ):
                return {"state": "ignored"}
            name = repository_name(value["repository"]["full_name"])
            repo_id, install_id = (
                positive_id(value["repository"]["id"]),
                positive_id(value["installation"]["id"]),
            )
            number, comment_id = positive_id(issue["number"]), positive_id(comment["id"])
            login = comment["user"]["login"]
            if not isinstance(login, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login
            ):
                raise ValueError("Invalid login")
            created = timestamp(comment["created_at"])
            if comment["updated_at"] != comment["created_at"]:
                return {"state": "ignored"}
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ServiceError(400, "Invalid webhook fields.") from error
        if not -30 <= self.store.clock() - created <= MAX_AGE:
            return {"state": "expired"}
        try:
            record = self.registry.policies.get(name)
        except ServiceError as error:
            if error.status == 404:
                return {"state": "ignored"}
            raise
        if (
            not record["policy"].enabled
            or not record["policy"].comment_reruns_enabled
            or record["repository_id"] != repo_id
            or record["installation_id"] != install_id
        ):
            return {"state": "ignored"}
        # No GitHub request in the delivery path. Persist before acknowledging.
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM comment_commands WHERE delivery=? "
                "OR (repository_id=? AND comment_id=?)",
                (delivery, repo_id, comment_id),
            ).fetchone():
                return {"state": "duplicate"}
            if (
                db.execute(
                    "SELECT count(*) FROM comment_commands WHERE state IN ('queued','processing')"
                ).fetchone()[0]
                >= 1000
            ):
                raise ServiceError(503, "Webhook queue is full.")
            db.execute(
                """INSERT INTO comment_commands
                (delivery,repository,repository_id,installation_id,pr,comment_id,actor,login,created,received,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,'queued')""",
                (
                    delivery,
                    name,
                    repo_id,
                    install_id,
                    number,
                    comment_id,
                    actor,
                    login,
                    created,
                    self.store.clock(),
                ),
            )
        self.wake.set()
        return {"state": "queued"}

    def recover(self) -> None:
        with self.store.connection() as db:
            # Never replay work whose GitHub POST outcome might be unknown.
            db.execute(
                "UPDATE comment_commands SET state='interrupted', "
                "message='Inspect GitHub Actions before posting a new command.' "
                "WHERE state='processing'"
            )

    def start(self) -> None:
        self.recover()
        self.thread = threading.Thread(target=self.run, name="pr-rerun-comments", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join()  # Keep App HTTP clients open until the worker stops.

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                if self.process_next():
                    continue
            except Exception as error:
                LOG.error("Comment worker failed (%s)", type(error).__name__)
            self.wake.wait(1)
            self.wake.clear()

    def check(self, command: dict, github) -> dict:
        record = self.registry.policies.get(command["repository"])
        if (
            not record["policy"].enabled
            or not record["policy"].comment_reruns_enabled
            or record["repository_id"] != command["repository_id"]
            or record["installation_id"] != command["installation_id"]
        ):
            raise ServiceError(403, "Comment commands are disabled or enrollment changed.")
        if self.store.clock() - command["created"] > MAX_AGE:
            raise ServiceError(409, "Comment command expired; post a new command.")
        current = github.comment(command["comment_id"])
        if (
            current.get("id") != command["comment_id"]
            or current.get("issue_url", "").lower()
            != f"https://api.github.com/repos/{command['repository']}/issues/{command['pr']}"
            or current.get("body", "").strip() != "/ci rerun"
            or current.get("user", {}).get("id") != command["actor"]
            or current.get("user", {}).get("type") != "User"
            or current.get("created_at") != current.get("updated_at")
            or timestamp(current["created_at"]) != command["created"]
        ):
            raise ServiceError(409, "The original PR comment changed or cannot be verified.")
        return record

    def process_next(self) -> bool:
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM comment_commands WHERE state='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            command = dict(row)
            db.execute(
                "UPDATE comment_commands SET state='processing' WHERE id=?", (command["id"],)
            )
        state, message, request_id = "rejected", "Command could not be verified.", None
        token_hash = None
        try:
            with closing(
                self.registry.auth.github(
                    command["repository"], command["repository_id"], command["installation_id"]
                )
            ) as github:
                self.check(command, github)
                pr = github.pull(command["pr"])
                if pr["user"]["id"] != command["actor"]:
                    raise ServiceError(403, "Only the PR author may request a rerun.")
                head = pr["head"]["sha"]
            provisional = Principal(command["actor"], command["login"], "")
            self.registry.policies.authorize(
                self.registry.policies.get(command["repository"]), provisional
            )
            # A verified GitHub comment authenticates its author. This short-lived
            # internal session is never returned to a client and never rotates logins.
            token = self.store.login_session(command["actor"], command["login"])
            token_hash = digest(token)
            with self.store.connection() as db:
                db.execute(
                    "UPDATE sessions SET expires=? WHERE token_hash=?",
                    (self.store.clock() + MAX_AGE, token_hash),
                )
            actor = self.store.authenticate_hash(token_hash)
            with self.registry.service(command["repository"], actor) as service:
                before_write = service.planner.github.before_write

                def guard():
                    if before_write:
                        before_write()
                    self.check(command, service.planner.github)
                    if service.planner.github.pull(command["pr"])["head"]["sha"] != head:
                        raise ServiceError(409, "PR head changed while processing the command.")

                service.planner.github.before_write = guard
                plan = service.preview(actor, command["pr"])
                if plan["pr"]["head_sha"] != head:
                    raise ServiceError(409, "PR head changed while processing the command.")
                if not plan["eligible"]:
                    state, message = (
                        "ineligible",
                        "No verified failed, timed-out or cancelled jobs are eligible.",
                    )
                else:
                    request_id = plan["request_id"]
                    with self.store.connection() as db:
                        db.execute(
                            "UPDATE comment_commands SET request_id=? WHERE id=?",
                            (request_id, command["id"]),
                        )
                    result = service.confirm(actor, plan["ticket"], plan["confirmation"])
                    state = result["state"]
                    message = (
                        "GitHub accepted the rerun request. This does not mean CI passed."
                        if state == "accepted"
                        else "The rerun was not fully accepted. Inspect the workflow "
                        "and request status before retrying."
                    )
        except ServiceError as error:
            message = str(error)
        except Exception as error:
            if request_id is not None:
                state, message = "unknown", "Inspect GitHub Actions before posting a new command."
            LOG.error("Comment command %d failed (%s)", command["id"], type(error).__name__)
        finally:
            if token_hash:
                self.store.logout_session(token_hash)
        with self.store.connection() as db:
            db.execute(
                "UPDATE comment_commands SET state=?, message=? WHERE id=?",
                (state, message, command["id"]),
            )
        LOG.info(
            "Comment command id=%d repository=%s pr=%d state=%s request=%s",
            command["id"],
            command["repository"],
            command["pr"],
            state,
            request_id,
        )
        return True
