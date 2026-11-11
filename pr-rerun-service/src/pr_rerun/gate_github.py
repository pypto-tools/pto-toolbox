"""Repository-scoped credentials and fixed API operations for environment approval."""

import json
import re

import httpx

from pr_rerun.app_auth import AppAuth
from pr_rerun.gate_policy import GatePolicy
from pr_rerun.github import API, GitHub
from pr_rerun.models import ServiceError, UpstreamError, positive_id

GATE_PERMISSIONS = {
    "actions": "read",
    "contents": "read",
    "pull_requests": "read",
    "deployments": "write",
}


class GateGitHub:
    """Never uses the rerun credential cache or the rerun client's write allowlist."""

    def __init__(
        self, auth: AppAuth, policy: GatePolicy, transport: httpx.BaseTransport | None = None
    ):
        self.auth = auth
        self.policy = policy
        self.client = httpx.Client(
            timeout=20, trust_env=False, follow_redirects=False, transport=transport
        )
        self.token = ""

    def close(self):
        self.client.close()

    def authorize(self):
        policy = self.policy
        jwt = self.auth.jwt()
        installation = self.auth._json("GET", f"/repos/{policy.repository}/installation", jwt)
        if (
            installation.get("id") != policy.installation_id
            or installation.get("app_id") != self.auth.app_id
            or installation.get("suspended_at") is not None
            or installation.get("repository_selection") != "selected"
            or any(
                installation.get("permissions", {}).get(name)
                not in ({"write"} if mode == "write" else {"read", "write"})
                for name, mode in GATE_PERMISSIONS.items()
            )
        ):
            raise ServiceError(403, "Current selected-repository gate authorization is required")
        result = self.auth._json(
            "POST",
            f"/app/installations/{policy.installation_id}/access_tokens",
            jwt,
            {"repository_ids": [policy.repository_id], "permissions": GATE_PERMISSIONS},
        )
        if not isinstance(result.get("token"), str) or not result["token"]:
            raise UpstreamError("Invalid gate token response")
        granted = result.get("permissions", {})
        if any(granted.get(name) != mode for name, mode in GATE_PERMISSIONS.items()) or any(
            mode not in {"read", "none"} and name != "deployments" for name, mode in granted.items()
        ):
            raise UpstreamError("Expected a deployment-only write token")
        self.token = result["token"]
        repository = self.auth._json("GET", f"/repos/{policy.repository}", self.token)
        if (
            repository.get("id") != policy.repository_id
            or repository.get("full_name", "").lower() != policy.repository
        ):
            self.token = ""
            raise ServiceError(403, "Repository identity changed; gate approval denied")

    def _request(self, method: str, path: str, payload: dict | None = None):
        reads = (
            r"actions/runs/[1-9][0-9]*(/attempts/[1-9][0-9]*/jobs\?per_page=100&page=[1-9][0-9]*)?"
            r"|actions/workflows/[1-9][0-9]*|pulls/[1-9][0-9]*|git/commits/[0-9a-f]{40}"
            r"|issues/[1-9][0-9]*/comments\?per_page=100&page=[1-9][0-9]*"
        )
        write = method == "POST" and re.fullmatch(
            r"actions/runs/[1-9][0-9]*/deployment_protection_rule", path
        )
        if not (write or (method == "GET" and payload is None and re.fullmatch(reads, path))):
            raise ValueError("Unsupported gate API operation")
        if write or not self.token:
            self.authorize()
        try:
            with self.client.stream(
                method,
                f"{API}/repos/{self.policy.repository}/{path}",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                json=payload,
            ) as response:
                if response.status_code != (204 if write else 200):
                    raise UpstreamError(
                        f"Gate API returned HTTP {response.status_code}", uncertain=bool(write)
                    )
                return None if write else json.loads(GitHub._read_bounded(response))
        except (httpx.HTTPError, ValueError):
            raise UpstreamError(
                "Gate API request could not be verified", uncertain=bool(write)
            ) from None

    def _get(self, path: str) -> dict:
        result = self._request("GET", path)
        if not isinstance(result, dict):
            raise UpstreamError("Expected a gate metadata object")
        return result

    def run(self, run_id: int) -> dict:
        return self._get(f"actions/runs/{positive_id(run_id)}")

    def pull(self, number: int):
        return self._get(f"pulls/{positive_id(number)}")

    def workflow(self, identity: int):
        return self._get(f"actions/workflows/{positive_id(identity)}")

    def commit(self, sha: str):
        return self._get(f"git/commits/{sha}")

    def _pages(self, path: str, key: str | None = None):
        rows = []
        for page in range(1, 101):
            result = self._request("GET", f"{path}?per_page=100&page={page}")
            if key:
                if not isinstance(result, dict):
                    raise UpstreamError("Expected a gate listing object")
                batch = result[key]
            else:
                batch = result
            if not isinstance(batch, list):
                raise UpstreamError("Invalid gate listing")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise UpstreamError("Gate listing exceeded pagination limit")

    def comments(self, number: int):
        return self._pages(f"issues/{positive_id(number)}/comments")

    def jobs(self, run_id: int, attempt: int):
        return self._pages(
            f"actions/runs/{positive_id(run_id)}/attempts/{positive_id(attempt)}/jobs", "jobs"
        )

    def approve(self, run_id: int, number: int):
        self._request(
            "POST",
            f"actions/runs/{positive_id(run_id)}/deployment_protection_rule",
            {
                "environment_name": self.policy.environment,
                "state": "approved",
                "comment": f"Fresh /ci full comment verified for PR #{positive_id(number)}.",
            },
        )
