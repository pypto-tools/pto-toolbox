"""Explicit enrollment and signed-event parsing for native CI environment gates."""

import re
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from pr_rerun.models import StrictModel, positive_id
from pr_rerun.policy import repository_name


class GatePolicy(StrictModel):
    repository: str
    repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    workflow: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9_-]+\.ya?ml$")
    base_branch: str = Field(min_length=1, max_length=255)
    environment: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    lint_jobs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("repository")
    @classmethod
    def normalize_repository(cls, value):
        return repository_name(value)

    @model_validator(mode="after")
    def unique_jobs(self):
        if any(not name.strip() for name in self.lint_jobs) or len(set(self.lint_jobs)) != len(
            self.lint_jobs
        ):
            raise ValueError("lint_jobs must contain unique, nonempty display names")
        return self


class GateConfig(StrictModel):
    repositories: list[GatePolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_repositories(self):
        if len({p.repository_id for p in self.repositories}) != len(self.repositories) or len(
            {p.repository for p in self.repositories}
        ) != len(self.repositories):
            raise ValueError("Configure exactly one CI gate policy per repository")
        return self


def timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamps must include a time zone")
    return parsed.timestamp()


def command(comment: dict, since: float) -> bool:
    """Every human commenter may release full CI; edited/old comments do not count."""
    return (
        comment.get("body", "").strip() == "/ci full"
        and comment.get("user", {}).get("type") == "User"
        and timestamp(comment["created_at"]) > since
        and comment.get("updated_at") == comment["created_at"]
    )


def parse_event(config: GateConfig, event: str, value: dict) -> dict | None:
    """Discard unconfigured events before queueing; never retain arbitrary callback URLs."""
    policies = {p.repository_id: p for p in config.repositories}
    policy = policies.get(value.get("repository", {}).get("id"))
    if policy is None:
        return None
    if (
        value["repository"].get("full_name", "").lower() != policy.repository
        or value.get("installation", {}).get("id") != policy.installation_id
    ):
        return None
    common = {"repository_id": policy.repository_id, "installation_id": policy.installation_id}
    if event == "issue_comment" and value.get("action") == "created":
        issue = value["issue"]
        if issue.get("pull_request") and command(value["comment"], 0):
            return {**common, "kind": "comment", "pr": positive_id(issue["number"])}
    if event == "deployment_protection_rule" and value.get("action") == "requested":
        if value.get("event") != "pull_request" or value.get("environment") != policy.environment:
            return None
        match = re.fullmatch(
            rf"https://api\.github\.com/repos/{re.escape(policy.repository)}/actions/runs/"
            r"([1-9][0-9]*)/deployment_protection_rule",
            value["deployment_callback_url"],
        )
        deployment = value["deployment"]
        if (
            match is None
            or deployment.get("environment") != policy.environment
            or not re.fullmatch(r"[0-9a-f]{40}", deployment["sha"])
        ):
            return None
        timestamp(deployment["created_at"])
        return {
            **common,
            "kind": "gate",
            "run": positive_id(int(match[1])),
            "deployment": positive_id(deployment["id"]),
            "sha": deployment["sha"],
            "created_at": deployment["created_at"],
            "environment": policy.environment,
        }
    return None
