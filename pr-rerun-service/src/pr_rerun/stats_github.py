"""Bounded Actions metadata reads. This client has no workflow mutation method."""

import json
import re
import time
from datetime import datetime, timezone

import httpx

from pr_rerun.app_auth import AppAuth
from pr_rerun.github import API, GitHub
from pr_rerun.models import UpstreamError, positive_id
from pr_rerun.policy import repository_name


def timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Expected a UTC timestamp")
    return int(parsed.timestamp())


def iso(value: int | float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


class StatsGitHub:
    def __init__(self, auth: AppAuth, record: dict, *, transport=None, budget=1500):
        self.auth, self.record = auth, record
        self.name = repository_name(record["name"])
        self.prefix = f"/repos/{self.name}/actions/runs"
        self.client = httpx.Client(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )
        self.token, self.expires = "", 0
        self.budget = budget
        self.requests = 0
        self.blocked = False

    def close(self):
        self.client.close()

    def authorize(self):
        self.token, self.expires = self.auth.read_credentials(
            self.name, self.record["repository_id"], self.record["installation_id"]
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        if not re.fullmatch(
            re.escape(self.prefix) + r"(?:/[1-9][0-9]*(?:/attempts/[1-9][0-9]*/jobs)?)?", path
        ):
            raise ValueError("Unsupported statistics endpoint")
        if self.requests >= self.budget:
            raise UpstreamError("Statistics request budget exhausted; sync will resume next time.")
        if self.expires <= time.time() + 60:
            self.authorize()
        self.requests += 1
        try:
            with self.client.stream(
                "GET",
                API + path,
                params=params,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as response:
                if response.status_code != 200:
                    self.blocked = response.status_code in {401, 403, 429}
                    raise UpstreamError(f"Statistics lookup failed (HTTP {response.status_code}).")
                remaining = response.headers.get("x-ratelimit-remaining")
                if remaining is not None and int(remaining) < 100:
                    self.blocked = True
                    raise UpstreamError("Statistics paused to preserve the GitHub API budget.")
                value = json.loads(GitHub._read_bounded(response))
                if not isinstance(value, dict):
                    raise ValueError
                return value
        except (httpx.HTTPError, ValueError, TypeError):
            raise UpstreamError("Invalid or unavailable GitHub statistics response.") from None

    def runs(self, since: int, until: int):
        """Split inclusive second-resolution windows before GitHub's 1,000-run cap."""
        params = {"created": f"{iso(since)}..{iso(until)}", "per_page": 100, "page": 1}
        first = self.get(self.prefix, params)
        total = first.get("total_count")
        if type(total) is not int or total < 0:
            raise UpstreamError("Invalid run count.")
        if total >= 1000:
            if since >= until:
                raise UpstreamError("Run listing exceeds GitHub's per-second search capacity.")
            midpoint = (since + until) // 2
            yield from self.runs(since, midpoint)
            yield from self.runs(midpoint + 1, until)
            return
        seen = set()
        for page in range(1, max(1, (total + 99) // 100) + 1):
            value = first if page == 1 else self.get(self.prefix, {**params, "page": page})
            rows = value.get("workflow_runs")
            if not isinstance(rows, list) or value.get("total_count") != total:
                raise UpstreamError("Run listing changed during pagination; retry the sync.")
            for row in rows:
                identity = positive_id(row["id"])
                if identity in seen or not since <= timestamp(row["created_at"]) <= until:
                    raise UpstreamError("Run listing is inconsistent; retry the sync.")
                seen.add(identity)
                yield row
        if len(seen) != total:
            raise UpstreamError("Run listing is incomplete; retry the sync.")

    def jobs(self, run_id: int, attempt: int):
        seen = set()
        path = f"{self.prefix}/{positive_id(run_id)}/attempts/{positive_id(attempt)}/jobs"
        total = None
        for page in range(1, 101):
            value = self.get(path, {"per_page": 100, "page": page})
            count, rows = value.get("total_count"), value.get("jobs")
            if (
                type(count) is not int
                or count < 0
                or not isinstance(rows, list)
                or (total is not None and total != count)
            ):
                raise UpstreamError("Job listing changed or is malformed.")
            total = count
            for row in rows:
                identity = positive_id(row["id"])
                if identity in seen or row.get("run_id") != run_id:
                    raise UpstreamError("Invalid job identity.")
                seen.add(identity)
                yield row
            if len(seen) == total:
                return
            if len(rows) < 100:
                break
        raise UpstreamError("Job listing is incomplete.")


def run_signature(run: dict) -> list:
    return [
        positive_id(run["run_attempt"]),
        run["updated_at"],
        run["status"],
        run.get("conclusion"),
    ]


def read_run(remote: StatsGitHub, run: dict) -> dict:
    if run.get("repository", {}).get("id") != remote.record["repository_id"]:
        raise UpstreamError("Run repository identity mismatch.")
    run_id, attempt = positive_id(run["id"]), positive_id(run["run_attempt"])
    if attempt > 100:
        raise UpstreamError("Run attempt limit exceeded.")
    result = {
        "id": run_id,
        "created_at": timestamp(run["created_at"]),
        "signature": run_signature(run),
        "workflow_id": positive_id(run["workflow_id"]),
        "workflow": str(run["path"]).split("@", 1)[0],
        "event": str(run["event"]),
        "branch": str(run.get("head_branch") or ""),
        "head_sha": str(run["head_sha"]),
        "jobs": [],
    }
    seen = set()
    for number in range(1, attempt + 1):
        for job in remote.jobs(run_id, number):
            job_id = positive_id(job["id"])
            # GitHub may include jobs carried forward from an earlier attempt.
            actual = positive_id(job.get("run_attempt", number))
            if actual > number or job.get("head_sha", run["head_sha"]) != run["head_sha"]:
                raise UpstreamError("Job attempt or commit identity mismatch.")
            if actual < number or job_id in seen:
                continue
            seen.add(job_id)
            if not isinstance(job.get("name"), str) or not isinstance(job.get("steps", []), list):
                raise UpstreamError("Invalid job metadata.")
            start, end = job.get("started_at"), job.get("completed_at")
            duration = max(0, timestamp(end) - timestamp(start)) if start and end else None
            result["jobs"].append(
                {
                    "id": job_id,
                    "attempt": number,
                    "name": job["name"],
                    "status": job["status"],
                    "conclusion": job.get("conclusion"),
                    "duration_seconds": duration,
                    "failed_steps": [
                        str(step["name"])
                        for step in job.get("steps", [])
                        if step.get("conclusion") in {"failure", "timed_out"}
                    ],
                }
            )
    latest = remote.get(f"{remote.prefix}/{run_id}")
    if latest.get("id") != run_id or run_signature(latest) != run_signature(run):
        raise UpstreamError("Run changed during collection; retry the sync.")
    return result
