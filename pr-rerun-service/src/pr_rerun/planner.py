"""Build plans from current PR checks and independently verifiable run identities."""

from datetime import datetime, timedelta, timezone

from pr_rerun.github import GitHub
from pr_rerun.models import (
    Plan,
    Principal,
    PullRequest,
    RunPlan,
    ServiceError,
    Settings,
    UpstreamError,
    WorkflowRule,
    positive_id,
)

FAILURES = {"failure", "timed_out"}
# A cancelled workflow can still contain failed jobs. Job conclusions, checked
# below, decide eligibility; cancellation by itself is never enough.
RERUN_CONCLUSIONS = FAILURES | {"cancelled"}


class Planner:
    def __init__(self, github: GitHub, settings: Settings) -> None:
        self.github = github
        self.settings = settings
        self.rules = {rule.path: rule for rule in settings.workflows}

    def pull(self, principal: Principal, number: int) -> PullRequest:
        pr = self.github.pull(number)
        if positive_id(pr["user"]["id"]) != principal.github_id:
            raise ServiceError(403, "You may rerun only PRs that you authored.")
        if (
            pr["number"] != number
            or pr["state"] != "open"
            or pr["base"]["repo"]["full_name"].lower() != self.settings.repository.lower()
        ):
            raise ServiceError(409, "An open PR in the configured repository is required.")
        if pr["head"]["repo"] is None:
            raise ServiceError(409, "The PR source repository no longer exists.")
        return PullRequest(
            repository_id=positive_id(pr["base"]["repo"]["id"]),
            number=number,
            node_id=pr["node_id"],
            title=pr["title"],
            author_id=principal.github_id,
            author=pr["user"]["login"],
            head_sha=pr["head"]["sha"],
            head_repository_id=positive_id(pr["head"]["repo"]["id"]),
            head_ref=pr["head"]["ref"],
            base_ref=pr["base"]["ref"],
            draft=pr["draft"],
            url=f"https://github.com/{self.settings.repository}/pull/{number}",
        )

    def matches(self, run: dict, pr: PullRequest, rule: WorkflowRule) -> bool:
        if (
            run["repository"]["id"] != pr.repository_id
            or run["repository"]["full_name"].lower() != self.settings.repository.lower()
            or run["path"].split("@", 1)[0] != rule.path
        ):
            return False
        if rule.identity == "codex_manifest":
            # This trusted base-workflow artifact is needed because fork
            # pull_request_target runs can omit their PR association entirely.
            if run["event"] != "pull_request_target":
                return False
            manifest = self.github.codex_identity(run["id"], run["run_attempt"])
            return manifest is not None and all(
                manifest.get(key) == value
                for key, value in {
                    "repository": self.settings.repository,
                    "number": pr.number,
                    "head": pr.head_sha,
                    "base_ref": pr.base_ref,
                }.items()
            )
        if run["event"] != "pull_request":
            return False
        links = run.get("pull_requests")
        if not isinstance(links, list):
            return False
        if links:
            # An explicit link to another PR must never be overridden by fallback.
            return any(
                link["number"] == pr.number
                and link["head"]["sha"] == pr.head_sha
                and link["head"]["repo"]["id"] == pr.head_repository_id
                and link["base"]["repo"]["id"] == pr.repository_id
                and link["base"]["ref"] == pr.base_ref
                for link in links
            )
        head_repository = run.get("head_repository")
        if (
            not isinstance(head_repository, dict)
            or head_repository.get("id") != pr.head_repository_id
            or run.get("head_branch") != pr.head_ref
            or run.get("head_sha") != pr.head_sha
        ):
            return False
        # SHA/ref alone is insufficient. The same source repository and branch
        # must have exactly one PR in this target, including all closed/merged
        # history. Reused branches and multiple target branches remain ambiguous.
        candidates = [
            candidate
            for candidate in self.github.head_pull_requests(pr.head_ref)
            if candidate["headRepository"] is not None
            and candidate["headRepository"]["databaseId"] == pr.head_repository_id
        ]
        if len(candidates) != 1:
            return False
        candidate = candidates[0]
        if (
            candidate["number"] != pr.number
            or candidate["state"] != "OPEN"
            or candidate["headRefOid"] != pr.head_sha
            or candidate["baseRefName"] != pr.base_ref
            or candidate["baseRepository"]["databaseId"] != pr.repository_id
        ):
            return False
        try:
            pr_created = datetime.fromisoformat(candidate["createdAt"].replace("Z", "+00:00"))
            run_created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if pr_created.tzinfo is None or run_created.tzinfo is None:
                raise ValueError("Missing timezone")
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise UpstreamError("Invalid PR/run identity timestamp.") from error
        return pr_created <= run_created

    def build(self, principal: Principal, number: int) -> Plan:
        pr = self.pull(principal, number)
        latest: dict[str, dict] = {}
        skipped = []
        for run_id in self.github.checked_runs(number, pr.head_sha):
            run = self.github.run(run_id)
            if positive_id(run["id"]) != run_id:
                raise UpstreamError("GitHub returned a different workflow run.")
            path = run["path"].split("@", 1)[0]
            rule = self.rules.get(path)
            if rule is None:
                skipped.append(f"Run {run_id}: workflow is not allowed.")
                continue
            if not self.matches(run, pr, rule):
                skipped.append(f"Run {run_id}: PR ownership could not be verified.")
                continue
            # Choose the newest run BEFORE filtering by failure. An older failed
            # run must not be resurrected after a new success or active run.
            if path not in latest or run["run_number"] > latest[path]["run_number"]:
                latest[path] = run
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        selected = []
        for path, run in sorted(latest.items()):
            run_id, attempt = positive_id(run["id"]), positive_id(run["run_attempt"])
            created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                raise UpstreamError("GitHub returned a timestamp without a timezone.")
            if created <= cutoff:
                skipped.append(f"Run {run_id}: outside GitHub's 30-day rerun window.")
                continue
            if run["status"] != "completed" or run["conclusion"] not in RERUN_CONCLUSIONS:
                skipped.append(f"Run {run_id}: {run['status']} / {run['conclusion'] or 'pending'}.")
                continue
            if self.rules[path].identity == "codex_manifest" and pr.draft:
                skipped.append(f"Run {run_id}: Codex review does not run for draft PRs.")
                continue
            jobs = self.github.jobs(run_id, attempt)
            failed = tuple(sorted(job["name"] for job in jobs if job["conclusion"] in FAILURES))
            if not failed:
                skipped.append(f"Run {run_id}: no failed jobs were found.")
                continue
            selected.append(
                RunPlan(
                    run_id=run_id,
                    attempt=attempt,
                    workflow_id=positive_id(run["workflow_id"]),
                    workflow=path,
                    name=run["name"],
                    mode=self.rules[path].mode,
                    failed_jobs=failed,
                    url=f"https://github.com/{self.settings.repository}/actions/runs/{run_id}",
                )
            )
        if self.pull(principal, number) != pr:
            raise ServiceError(409, "PR changed during lookup; request another preview.")
        return Plan(
            repository=self.settings.repository,
            pr=pr,
            runs=tuple(selected),
            skipped=tuple(skipped),
        )

    def validate_run(self, principal: Principal, plan: Plan, run: RunPlan) -> None:
        """Check again immediately before each write, without choosing a replacement run."""
        if self.pull(principal, plan.pr.number) != plan.pr:
            raise ServiceError(409, "PR changed after confirmation.")
        current = self.github.run(run.run_id)
        rule = self.rules.get(run.workflow)
        if (
            rule is None
            or rule.mode != run.mode
            or current["id"] != run.run_id
            or current["workflow_id"] != run.workflow_id
            or current["run_attempt"] != run.attempt
            or current["status"] != "completed"
            or current["conclusion"] not in RERUN_CONCLUSIONS
            or not self.matches(current, plan.pr, rule)
        ):
            raise ServiceError(409, "Workflow state changed after confirmation.")
        jobs = self.github.jobs(run.run_id, run.attempt)
        failed = tuple(sorted(job["name"] for job in jobs if job["conclusion"] in FAILURES))
        if not failed or failed != run.failed_jobs:
            raise ServiceError(409, "Failed jobs changed after confirmation.")
