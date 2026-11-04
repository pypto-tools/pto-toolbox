"""Shared validated configuration and immutable preview data."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from pr_rerun.credentials import github_credential

Mode = Literal["failed", "all"]


class ServiceError(Exception):
    """An error message safe to return to a client."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class UpstreamError(ServiceError):
    """A GitHub failure, including whether a write may already have happened."""

    def __init__(self, message: str, uncertain: bool = False) -> None:
        super().__init__(502, message)
        self.uncertain = uncertain


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class WorkflowRule(StrictModel):
    path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9_-]+\.ya?ml$")
    mode: Mode = "failed"
    identity: Literal["pull_request", "codex_manifest"] = "pull_request"

    @model_validator(mode="after")
    def check_mode(self):
        # An all-jobs rerun is an explicit exception for the Codex workflow.
        if (self.mode == "all") != (self.identity == "codex_manifest"):
            raise ValueError("codex_manifest requires all; ordinary PR workflows require failed")
        return self


class Settings(StrictModel):
    repository: str = Field(
        default="hw-native-sys/pypto", pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$"
    )
    workflows: tuple[WorkflowRule, ...] = (
        WorkflowRule(path=".github/workflows/ci.yml"),
        WorkflowRule(path=".github/workflows/docs.yml"),
        WorkflowRule(
            path=".github/workflows/codex-review.yml", mode="all", identity="codex_manifest"
        ),
    )
    app_id: int | None = Field(default=None, gt=0)
    app_client_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    app_key_file: Path | None = Field(default=None, repr=False)
    login_proxy: SecretStr | None = Field(default=None, repr=False)
    policy_revision: int = Field(default=0, ge=0)
    repository_id: int | None = Field(default=None, gt=0)
    allow_drafts: bool = True
    base_branches: tuple[str, ...] = ()
    daily_limit: int | None = None
    active_limit: int | None = None
    preview_ttl: int = Field(default=300, ge=30, le=900)
    preview_interval: int = Field(default=5, ge=0)
    confirm_interval: int = Field(default=60, ge=0)
    database: Path
    stats_database: Path | None = None
    github_token: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def validate_settings(self):
        if not self.github_token.get_secret_value().strip():
            raise ValueError("A GitHub token is required")
        if not self.workflows or len({r.path for r in self.workflows}) != len(self.workflows):
            raise ValueError("At least one unique workflow rule is required")
        return self

    @classmethod
    def from_env(cls, *, github_auth: str | None = None) -> "Settings":
        """Read service configuration and the explicitly selected credential source."""
        data = {}
        if path := os.environ.get("PR_RERUN_CONFIG"):
            data = json.loads(Path(path).read_text())
            if not isinstance(data, dict) or {"github_token", "database"} & data.keys():
                raise ValueError("Config must be an object without credentials or a database path")
            if "workflows" in data:
                data["workflows"] = tuple(WorkflowRule(**item) for item in data["workflows"])
        database = os.environ.get("PR_RERUN_DATABASE")
        if not database:
            raise ValueError("Configure PR_RERUN_DATABASE")
        mode = github_auth or os.environ.get("PR_RERUN_GITHUB_AUTH", "token")
        if mode == "app":
            if data:
                raise ValueError("App mode uses repository policies; unset PR_RERUN_CONFIG")
            app_id = int(os.environ.get("PR_RERUN_APP_ID", "0"))
            key_file = os.environ.get("PR_RERUN_APP_PRIVATE_KEY_FILE")
            if app_id <= 0 or not key_file:
                raise ValueError("Set PR_RERUN_APP_ID and PR_RERUN_APP_PRIVATE_KEY_FILE")
            return cls(
                database=Path(database),
                stats_database=(
                    Path(os.environ["PR_RERUN_STATS_DATABASE"])
                    if os.environ.get("PR_RERUN_STATS_DATABASE")
                    else None
                ),
                github_token=SecretStr("installation-managed"),
                app_id=app_id,
                app_client_id=os.environ.get("PR_RERUN_APP_CLIENT_ID") or None,
                login_proxy=(
                    SecretStr(os.environ["PR_RERUN_LOGIN_PROXY"])
                    if os.environ.get("PR_RERUN_LOGIN_PROXY")
                    else None
                ),
                app_key_file=Path(key_file),
            )
        token = github_credential(github_auth)
        return cls(database=Path(database), github_token=SecretStr(token), **data)


@dataclass(frozen=True)
class Principal:
    github_id: int
    login: str
    token_hash: str


class PullRequest(StrictModel):
    repository_id: int
    number: int
    node_id: str
    title: str
    author_id: int
    author: str
    head_sha: str
    head_repository_id: int
    head_ref: str
    base_ref: str
    draft: bool
    url: str


class RunPlan(StrictModel):
    run_id: int
    attempt: int
    workflow_id: int
    workflow: str
    name: str
    mode: Mode
    failed_jobs: tuple[str, ...]
    url: str
    cancelled_jobs: tuple[str, ...] = ()


class Plan(StrictModel):
    policy_revision: int = 0
    repository: str
    pr: PullRequest
    runs: tuple[RunPlan, ...]
    skipped: tuple[str, ...] = ()

    @property
    def confirmation(self) -> str:
        return f"{self.repository}#{self.pr.number} @{self.pr.author}"

    @property
    def fingerprint(self) -> str:
        # Explanatory text about excluded runs is not part of the authorized write.
        data = self.model_dump(mode="json", exclude={"skipped"})
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def positive_id(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise UpstreamError("GitHub returned an invalid identifier.")
    return value
