"""Versioned repository policies; no credentials or executable configuration."""

import json
import re
import sqlite3
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from pr_rerun.models import Principal, ServiceError, StrictModel, WorkflowRule
from pr_rerun.store import Store

RepositoryName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")]
UserID = Annotated[int, Field(gt=0, lt=2**63)]
AliasName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")]


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Use an owner/repository name")
    return value.lower()


class RepositoryPolicy(StrictModel):
    enabled: bool = True
    maintainers: list[UserID] = Field(min_length=1, max_length=100)
    users: list[UserID] = Field(default_factory=list, max_length=1000)
    access: Literal["explicit_users", "authenticated_users"] = "explicit_users"
    aliases: list[AliasName] = Field(default_factory=list, max_length=20)
    analytics_enabled: bool = False
    analytics_access: Literal["maintainers", "repository_users"] = "maintainers"
    error_logs_enabled: bool = False
    error_logs_access: Literal["maintainers", "repository_users"] = "maintainers"
    allow_drafts: bool = False
    base_branches: list[str] = Field(default_factory=lambda: ["main"], min_length=1, max_length=100)
    workflows: list[WorkflowRule] = Field(min_length=1, max_length=100)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    max_requests_per_user_per_day: int | None = Field(default=20, ge=1, le=1000)
    max_active_requests_per_pr: int | None = Field(default=1, ge=1, le=10)

    @field_validator("base_branches")
    @classmethod
    def branches(cls, values: list[str]) -> list[str]:
        if any(
            not value or len(value) > 255 or any(ord(c) < 32 for c in value) for value in values
        ):
            raise ValueError("Invalid base branch pattern")
        return values

    @model_validator(mode="after")
    def unique_rules(self):
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Repository aliases must be unique")
        if len({rule.path for rule in self.workflows}) != len(self.workflows):
            raise ValueError("Workflow paths must be unique")
        if len(set(self.maintainers)) != len(self.maintainers) or len(set(self.users)) != len(
            self.users
        ):
            raise ValueError("User IDs must be unique")
        return self


class PolicyUpdate(StrictModel):
    expected_revision: int = Field(gt=0)
    policy: RepositoryPolicy


class PolicyStore:
    def __init__(self, store: Store):
        self.store = store
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repositories (
                    name TEXT PRIMARY KEY, repository_id INTEGER UNIQUE NOT NULL,
                    installation_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    policy TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policy_history (
                    repository TEXT NOT NULL, revision INTEGER NOT NULL,
                    actor INTEGER NOT NULL, created REAL NOT NULL, policy TEXT NOT NULL,
                    PRIMARY KEY(repository, revision)
                );
                CREATE TABLE IF NOT EXISTS repository_aliases (
                    alias TEXT PRIMARY KEY, repository TEXT NOT NULL
                );
            """)

    def get(self, name: str) -> dict:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM repositories WHERE name=?", (repository_name(name),)
            ).fetchone()
        if row is None:
            raise ServiceError(404, "Repository is not enrolled.")
        return {**dict(row), "policy": RepositoryPolicy.model_validate_json(row["policy"])}

    @staticmethod
    def replace_aliases(db, name: str, aliases: list[str]) -> None:
        db.execute("DELETE FROM repository_aliases WHERE repository=?", (name,))
        try:
            db.executemany(
                "INSERT INTO repository_aliases VALUES (?, ?)", [(alias, name) for alias in aliases]
            )
        except sqlite3.IntegrityError:
            raise ServiceError(
                409, "An official alias is already assigned to another repository."
            ) from None

    def aliases(self, principal: Principal) -> dict[str, str]:
        result = {}
        service_admin = self.store.is_service_admin(principal.github_id)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT a.alias, a.repository, r.policy FROM repository_aliases a "
                "JOIN repositories r ON r.name=a.repository ORDER BY a.alias"
            ).fetchall()
        for row in rows:
            policy = RepositoryPolicy.model_validate_json(row["policy"])
            if service_admin and policy.enabled:
                result[row["alias"]] = row["repository"]
                continue
            try:
                self.authorize({"policy": policy}, principal)
            except ServiceError:
                continue
            result[row["alias"]] = row["repository"]
        return result

    def repositories(self, principal: Principal) -> dict:
        """List local enrollment only; never imply live GitHub authorization."""
        service_admin = self.store.is_service_admin(principal.github_id)
        result = []
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT name, revision, policy FROM repositories ORDER BY name"
            ).fetchall()
        for row in rows:
            policy = RepositoryPolicy.model_validate_json(row["policy"])
            maintainer = principal.github_id in policy.maintainers
            if not service_admin and not maintainer:
                try:
                    self.authorize({"policy": policy}, principal)
                except ServiceError:
                    continue
            result.append(
                {
                    "repository": row["name"],
                    "aliases": policy.aliases,
                    "enabled": policy.enabled,
                    "revision": row["revision"],
                    "role": "service_admin"
                    if service_admin
                    else "maintainer"
                    if maintainer
                    else "user",
                }
            )
        return {
            "scope": "all" if service_admin else "visible",
            "github_checked": False,
            "repositories": result,
        }

    def resolve_alias(self, alias: str, principal: Principal) -> str:
        # Both listing and resolution hide inaccessible or disabled repositories.
        value = self.aliases(principal).get(alias)
        if value is None:
            raise ServiceError(404, "Official alias is unavailable for your account.")
        return value

    def authorize(
        self, record: dict, principal: Principal, *, manage: bool = False, db=None
    ) -> None:
        if manage and self.store.is_service_admin(principal.github_id, db=db):
            return
        policy = record["policy"]
        members = policy.maintainers if manage else policy.maintainers + policy.users
        if principal.github_id not in members and not (
            not manage and policy.access == "authenticated_users"
        ):
            raise ServiceError(403, "You are not authorized for this repository.")
        if not manage and not policy.enabled:
            raise ServiceError(403, "Repository reruns are disabled.")

    def authorize_analytics(self, record: dict, principal: Principal) -> None:
        policy = record["policy"]
        if not policy.enabled or not policy.analytics_enabled:
            raise ServiceError(403, "CI statistics are disabled for this repository.")
        if self.store.is_service_admin(principal.github_id):
            return
        self.authorize(record, principal, manage=policy.analytics_access == "maintainers")

    def authorize_errors(self, record: dict, principal: Principal) -> None:
        policy = record["policy"]
        if not policy.enabled or not policy.analytics_enabled or not policy.error_logs_enabled:
            raise ServiceError(403, "Error excerpts are disabled for this repository.")
        if self.store.is_service_admin(principal.github_id):
            return
        self.authorize(record, principal, manage=policy.error_logs_access == "maintainers")

    def enroll(self, name: str, repository_id: int, installation_id: int, policy: RepositoryPolicy):
        name = repository_name(name)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM repositories WHERE name=?", (name,)).fetchone():
                raise ServiceError(409, "Repository is already enrolled; update its policy.")
            db.execute(
                "INSERT INTO repositories VALUES (?, ?, ?, 1, ?)",
                (name, repository_id, installation_id, policy.model_dump_json()),
            )
            self.replace_aliases(db, name, policy.aliases)
            db.execute(
                "INSERT INTO policy_history VALUES (?, 1, 0, ?, ?)",
                (name, self.store.clock(), policy.model_dump_json()),
            )

    def update(self, name: str, update: PolicyUpdate, principal: Principal) -> dict:
        name = repository_name(name)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM repositories WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ServiceError(404, "Repository is not enrolled.")
            active = self.store.authenticated_row(db, principal.token_hash)
            if active is None or active["github_id"] != principal.github_id:
                raise ServiceError(401, "Service access token is invalid or revoked.")
            current = RepositoryPolicy.model_validate_json(row["policy"])
            self.authorize({"policy": current}, principal, manage=True, db=db)
            if row["revision"] != update.expected_revision:
                raise ServiceError(409, "Policy changed; fetch it again before updating.")
            revision = row["revision"] + 1
            self.replace_aliases(db, name, update.policy.aliases)
            db.execute(
                "UPDATE repositories SET policy=?, revision=? WHERE name=?",
                (update.policy.model_dump_json(), revision, name),
            )
            db.execute(
                "INSERT INTO policy_history VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    revision,
                    principal.github_id,
                    self.store.clock(),
                    update.policy.model_dump_json(),
                ),
            )
        return {"revision": revision, "policy": update.policy.model_dump(mode="json")}

    def history(self, name: str, principal: Principal) -> list[dict]:
        record = self.get(name)
        self.authorize(record, principal, manage=True)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT revision, actor, created, policy FROM policy_history "
                "WHERE repository=? ORDER BY revision DESC LIMIT 100",
                (record["name"],),
            ).fetchall()
        return [{**dict(row), "policy": json.loads(row["policy"])} for row in rows]
