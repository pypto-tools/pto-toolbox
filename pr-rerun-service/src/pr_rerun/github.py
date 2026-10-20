"""Fixed GitHub operations with no generic public write or URL proxy."""

import io
import json
import re
import zipfile
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from pr_rerun.models import Mode, ServiceError, UpstreamError, positive_id

API = "https://api.github.com"
MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100
# A fixed query is the only permitted POST besides the two rerun operations.
CHECK_QUERY = """
query Checks($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      headRefOid
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              contexts(first: 100, after: $cursor) {
                nodes {
                  __typename
                  ... on CheckRun { checkSuite { workflowRun { resourcePath } } }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHub:
    def __init__(
        self, repository: str, token: str, transport: httpx.BaseTransport | None = None
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        if not token:
            raise ValueError("A GitHub token is required")
        self.repository = repository
        self.prefix = f"/repos/{repository}"
        self._token = token
        # No implicit retries, redirect following, ambient netrc credentials, or
        # credentialed client defaults that could reach artifact storage.
        self._client = httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self, method: str, path: str, *, payload: dict | None = None, write: bool = False
    ) -> httpx.Response:
        allowed_get = (
            rf"{re.escape(self.prefix)}/(pulls/[1-9][0-9]*|actions/runs/[1-9][0-9]*"
            r"(/artifacts\?per_page=100&page=[1-9][0-9]*"
            r"|/attempts/[1-9][0-9]*/jobs\?per_page=100&page=[1-9][0-9]*)?"
            r"|actions/artifacts/[1-9][0-9]*/zip)"
            r"|/users/[A-Za-z0-9][A-Za-z0-9-]{0,38}"
        )
        allowed_write = rf"{re.escape(self.prefix)}/actions/runs/[1-9][0-9]*/rerun(-failed-jobs)?"
        authorized = (
            (method == "GET" and payload is None and re.fullmatch(allowed_get, path))
            or (
                method == "POST" and write and payload is None and re.fullmatch(allowed_write, path)
            )
            or (
                method == "POST"
                and not write
                and path == "/graphql"
                and payload is not None
                and payload.get("query") == CHECK_QUERY
            )
        )
        if not authorized:
            raise ServiceError(400, "Unsupported GitHub operation.")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        try:
            with self._client.stream(
                method, f"{API}{path}", headers=headers, json=payload
            ) as response:
                content = self._read_bounded(response)
                # iter_bytes() has already decoded compression and transfer framing.
                # Retaining those headers makes Response decode the same body again.
                decoded_headers = response.headers.copy()
                for name in ("content-encoding", "content-length", "transfer-encoding"):
                    decoded_headers.pop(name, None)
                result = httpx.Response(
                    response.status_code, headers=decoded_headers, content=content
                )
        except httpx.DecodingError as error:
            raise UpstreamError(
                "GitHub returned an invalid compressed response.", uncertain=write
            ) from error
        except (httpx.HTTPError, OSError) as error:
            raise UpstreamError(
                "GitHub connection failed. The request may need inspection.", uncertain=write
            ) from error
        expected = {201} if write else {200, 302}
        if result.status_code not in expected:
            # Do not relay upstream bodies, URLs containing signed tokens, or credentials.
            raise UpstreamError(
                f"GitHub returned HTTP {result.status_code}.",
                uncertain=write and result.status_code >= 500,
            )
        return result

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise UpstreamError("GitHub response exceeded the size limit.", uncertain=True)
            chunks.append(chunk)
        return b"".join(chunks)

    def _json(self, path: str, payload: dict | None = None) -> Any:
        response = self._request("GET" if payload is None else "POST", path, payload=payload)
        if response.status_code != 200:
            raise UpstreamError("Unexpected GitHub metadata redirect.")
        try:
            return response.json()
        except ValueError as error:
            raise UpstreamError("GitHub returned invalid JSON.") from error

    def user(self, login: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
            raise ServiceError(400, "Invalid GitHub login.")
        return self._json(f"/users/{login}")

    def pull(self, number: int) -> dict:
        return self._json(f"{self.prefix}/pulls/{positive_id(number)}")

    def run(self, run_id: int) -> dict:
        return self._json(f"{self.prefix}/actions/runs/{positive_id(run_id)}")

    def _pages(self, path: str, key: str) -> Iterator[dict]:
        for page in range(1, MAX_PAGES + 1):
            response = self._json(f"{path}?per_page=100&page={page}")
            batch = response[key]
            if not isinstance(batch, list) or len(batch) > 100:
                raise UpstreamError("Invalid GitHub pagination response.")
            yield from batch
            if len(batch) < 100:
                return
        raise UpstreamError("GitHub result exceeded the pagination limit; nothing was selected.")

    def jobs(self, run_id: int, attempt: int) -> list[dict]:
        return list(
            self._pages(
                f"{self.prefix}/actions/runs/{positive_id(run_id)}"
                f"/attempts/{positive_id(attempt)}/jobs",
                "jobs",
            )
        )

    def checked_runs(self, number: int, head: str) -> list[int]:
        owner, repo = self.repository.split("/")
        cursor = None
        seen, result = set(), set()
        for _ in range(MAX_PAGES):
            response = self._json(
                "/graphql",
                {
                    "query": CHECK_QUERY,
                    "variables": {
                        "owner": owner,
                        "repo": repo,
                        "pr": positive_id(number),
                        "cursor": cursor,
                    },
                },
            )
            if response.get("errors"):
                raise UpstreamError("Unable to read complete PR checks.")
            pr = response["data"]["repository"]["pullRequest"]
            commits = pr["commits"]["nodes"]
            if len(commits) != 1 or pr["headRefOid"] != head or commits[0]["commit"]["oid"] != head:
                raise ServiceError(409, "PR head changed; request another preview.")
            rollup = commits[0]["commit"]["statusCheckRollup"]
            if rollup is None:
                if cursor is not None:
                    raise ServiceError(409, "PR checks changed during pagination.")
                return []
            contexts = rollup["contexts"]
            for check in contexts["nodes"]:
                if check["__typename"] != "CheckRun":
                    continue
                suite = check["checkSuite"]
                run = suite.get("workflowRun") if suite else None
                if run is None:
                    continue  # External checks are never rerunnable through this service.
                match = re.fullmatch(
                    rf"/{re.escape(self.repository)}/actions/runs/([1-9][0-9]*)",
                    run["resourcePath"],
                    flags=re.IGNORECASE,
                )
                if match is None:
                    raise UpstreamError("A PR check has an unexpected workflow run origin.")
                result.add(positive_id(int(match[1])))
            page = contexts["pageInfo"]
            if type(page["hasNextPage"]) is not bool:
                raise UpstreamError("Invalid check pagination state.")
            if not page["hasNextPage"]:
                return sorted(result, reverse=True)
            cursor = page["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise UpstreamError("Invalid or repeated check pagination cursor.")
            seen.add(cursor)
        raise UpstreamError("Too many PR checks; nothing was selected.")

    def _artifact_bytes(self, artifact_id: int) -> bytes:
        response = self._request(
            "GET", f"{self.prefix}/actions/artifacts/{positive_id(artifact_id)}/zip"
        )
        if response.status_code == 200:
            return response.content
        location = response.headers.get("location", "")
        parsed = urlsplit(location)
        host = parsed.hostname or ""
        # GitHub's signed blob redirect is the only allowed non-API destination.
        # Never send Authorization to it, and never follow a second redirect.
        allowed = host.endswith((".blob.core.windows.net", ".actions.githubusercontent.com"))
        allowed = allowed or host == "objects.githubusercontent.com"
        if (
            parsed.scheme != "https"
            or not allowed
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UpstreamError("Untrusted artifact download origin.")
        try:
            with self._client.stream("GET", location) as artifact:
                if artifact.status_code != 200:
                    raise UpstreamError("Artifact download failed.")
                return self._read_bounded(artifact)
        except httpx.HTTPError as error:
            raise UpstreamError("Artifact download failed.") from error

    def codex_identity(self, run_id: int, attempt: int) -> dict | None:
        candidates = []
        for artifact in self._pages(
            f"{self.prefix}/actions/runs/{positive_id(run_id)}/artifacts", "artifacts"
        ):
            match = re.fullmatch(
                rf"codex-(target|context)-{run_id}-([1-9][0-9]*)", artifact["name"]
            )
            if match and not artifact["expired"] and int(match[2]) <= attempt:
                candidates.append((int(match[2]), match[1] == "target", artifact))
        if not candidates:
            return None
        # Retain the newest available identity, preferring the small trusted
        # manifest over a discussion archive from the same attempt.
        _, target, artifact = max(candidates, key=lambda item: item[:2])
        if not 0 < artifact["size_in_bytes"] <= MAX_BYTES:
            raise UpstreamError("Invalid artifact size.")
        filename = "review-target.json" if target else "discussion.json"
        try:
            with zipfile.ZipFile(io.BytesIO(self._artifact_bytes(artifact["id"]))) as archive:
                if archive.namelist() != [filename]:
                    raise UpstreamError("Unexpected routing artifact members.")
                member = archive.getinfo(filename)
                if member.file_size > MAX_BYTES or member.flag_bits & 1:
                    raise UpstreamError("Invalid routing artifact member.")
                value = json.loads(archive.read(member))
                if not isinstance(value, dict):
                    raise UpstreamError("Invalid routing artifact object.")
                return value
        except (ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
            raise UpstreamError("Invalid routing artifact.") from error

    def rerun(self, run_id: int, mode: Mode) -> None:
        if mode not in ("failed", "all"):
            raise ServiceError(400, "Invalid rerun mode.")
        suffix = "rerun" if mode == "all" else "rerun-failed-jobs"
        self._request(
            "POST", f"{self.prefix}/actions/runs/{positive_id(run_id)}/{suffix}", write=True
        )
