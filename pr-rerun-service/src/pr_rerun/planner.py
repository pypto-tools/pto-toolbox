"""Build plans from current PR checks and independently verifiable run identities."""

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase

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
LOOKUP_WORKERS = 4
LOG = logging.getLogger("pr_rerun.performance")


class Planner:
    def __init__(
        self,
        github: GitHub,
        settings: Settings,
        authorize: Callable[[Principal], None] | None = None,
    ) -> None:
        self.authorize = authorize
        self.github = github
        self.settings = settings
        self.rules = {rule.path: rule for rule in settings.workflows}

    def pull(self, principal: Principal, number: int) -> PullRequest:
        if self.authorize is not None:
            self.authorize(principal)
        pr = self.github.pull(number)
        if positive_id(pr["user"]["id"]) != principal.github_id:
            raise ServiceError(403, "You may rerun only PRs that you authored.")
        if (
            pr["number"] != number
            or pr["state"] != "open"
            or pr["base"]["repo"]["full_name"].lower() != self.settings.repository.lower()
        ):
            raise ServiceError(409, "An open PR in the configured repository is required.")
        if (
            self.settings.repository_id is not None
            and pr["base"]["repo"]["id"] != self.settings.repository_id
        ):
            raise ServiceError(403, "Repository identity does not match enrollment.")
        if pr["draft"] and not self.settings.allow_drafts:
            raise ServiceError(403, "Draft PRs are disabled by repository policy.")
        if self.settings.base_branches and not any(
            fnmatchcase(pr["base"]["ref"], pattern) for pattern in self.settings.base_branches
        ):
            raise ServiceError(403, "Target branch is not allowed by repository policy.")
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

    def matches(
        self,
        run: dict,
        pr: PullRequest,
        rule: WorkflowRule,
        *,
        branch_lookup: Callable[[str], list[dict]] | None = None,
    ) -> bool:
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
            for candidate in (branch_lookup or self.github.head_pull_requests)(pr.head_ref)
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
        started = time.monotonic()
        pr = self.pull(principal, number)
        latest: dict[str, dict] = {}
        skipped = []
        # Only this build shares branch history. Confirmation and the final
        # per-run check must obtain new evidence rather than reuse preview data.
        branch_cache: dict[str, list[dict]] = {}
        branch_lock = threading.Lock()

        def branch_history(head: str) -> list[dict]:
            with branch_lock:
                if head not in branch_cache:
                    branch_cache[head] = self.github.head_pull_requests(head)
                return branch_cache[head]

        def inspect(run_id: int) -> tuple[dict, str | None]:
            run = self.github.run(run_id)
            if positive_id(run["id"]) != run_id:
                raise UpstreamError("GitHub returned a different workflow run.")
            path = run["path"].split("@", 1)[0]
            rule = self.rules.get(path)
            if rule is None:
                return run, f"Run {run_id}: workflow is not allowed."
            if not self.matches(run, pr, rule, branch_lookup=branch_history):
                return run, f"Run {run_id}: PR ownership could not be verified."
            return run, None

        run_ids = self.github.checked_runs(number, pr.head_sha)
        executor = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS)
        try:
            # map preserves input order, including deterministic exclusions and
            # selection when GitHub responses complete in a different order.
            for run, reason in executor.map(inspect, run_ids):
                if reason is not None:
                    skipped.append(reason)
                    continue
                path = run["path"].split("@", 1)[0]
                # Choose the newest run BEFORE filtering by failure. An older failed
                # run must not be resurrected after a new success or active run.
                if path not in latest or run["run_number"] > latest[path]["run_number"]:
                    latest[path] = run
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
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
        LOG.info(
            "Plan verified repository=%s pr=%d checked_runs=%d "
            "eligible_runs=%d elapsed_seconds=%.2f",
            self.settings.repository,
            number,
            len(run_ids),
            len(selected),
            time.monotonic() - started,
        )
        return Plan(
            policy_revision=self.settings.policy_revision,
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
