"""GitHub App installation authentication, isolated from rerun API operations."""

import base64
import json
import os
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pr_rerun.github import API, GitHub
from pr_rerun.models import ServiceError, UpstreamError, positive_id
from pr_rerun.policy import repository_name

PERMISSIONS = {"actions": "write", "checks": "read", "contents": "read", "pull_requests": "read"}


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class AppAuth:
    def __init__(self, app_id: int, key_file: Path, transport: httpx.BaseTransport | None = None):
        self.app_id = positive_id(app_id)
        self.key_file = key_file
        self.client = httpx.Client(
            timeout=30, trust_env=False, follow_redirects=False, transport=transport
        )
        self.lock = threading.Lock()
        self.tokens: dict[tuple[str, int], tuple[str, float]] = {}
        self.read_tokens: dict[tuple[str, int, int], tuple[str, float]] = {}

    def close(self):
        self.client.close()

    def jwt(self) -> str:
        now = int(time.time())
        message = (
            encode(b'{"alg":"RS256","typ":"JWT"}')
            + "."
            + encode(
                # Leave room for modest host clock skew on both sides of GitHub's
                # ten-minute expiration limit; these JWTs are generated per use.
                json.dumps({"iss": str(self.app_id), "iat": now - 120, "exp": now + 300}).encode()
            )
        )
        # Keep key bytes out of command arguments, environment and diagnostics.
        descriptor = os.open(self.key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
                or info.st_nlink != 1
            ):
                raise ValueError("App key must be an owner-only regular file (0400 or 0600)")
            try:
                signed = subprocess.run(
                    ["openssl", "dgst", "-sha256", "-sign", f"/proc/self/fd/{descriptor}"],
                    input=message.encode(),
                    capture_output=True,
                    pass_fds=(descriptor,),
                    timeout=10,
                    check=True,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                raise ValueError(
                    "Cannot sign GitHub App authentication; check the RSA key and OpenSSL"
                ) from None
        finally:
            os.close(descriptor)
        return message + "." + encode(signed)

    def _value(
        self, method: str, path: str, token: str, payload: dict | None = None
    ) -> dict | list:
        # Only internal, fixed paths below call this method; no client URL is accepted.
        try:
            with self.client.stream(
                method,
                API + path,
                json=payload,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as response:
                if response.status_code not in {200, 201}:
                    raise UpstreamError(
                        f"GitHub App authorization failed (HTTP {response.status_code})."
                    )
                value = json.loads(GitHub._read_bounded(response))
                if not isinstance(value, (dict, list)):
                    raise ValueError
                return value
        except (httpx.HTTPError, ValueError):
            raise UpstreamError("GitHub App authorization could not be verified.") from None

    def _json(self, method: str, path: str, token: str, payload: dict | None = None) -> dict:
        value = self._value(method, path, token, payload)
        if not isinstance(value, dict):
            raise UpstreamError("Invalid GitHub App response.")
        return value

    def installations(self) -> list[dict]:
        """Inventory only: fresh metadata-only tokens, never rerun credentials."""
        installations = []
        seen = set()
        for page in range(1, 101):
            rows = self._value("GET", f"/app/installations?per_page=100&page={page}", self.jwt())
            if not isinstance(rows, list):
                raise UpstreamError("Invalid GitHub installation listing.")
            try:
                for row in rows:
                    identity = positive_id(row["id"])
                    if row.get("app_id") != self.app_id or identity in seen:
                        raise ValueError
                    seen.add(identity)
                    installations.append(
                        {
                            "installation_id": identity,
                            "account": str(row["account"]["login"]),
                            "selection": row["repository_selection"],
                            "suspended": row.get("suspended_at") is not None,
                            "permissions_ok": all(
                                row.get("permissions", {}).get(key)
                                in ({"write"} if mode == "write" else {"read", "write"})
                                for key, mode in PERMISSIONS.items()
                            ),
                            "repositories": [],
                            "repositories_checked": False,
                            "error": None,
                        }
                    )
            except (KeyError, TypeError, ValueError, AttributeError):
                raise UpstreamError("Invalid GitHub installation listing.") from None
            if len(rows) < 100:
                break
        else:
            raise UpstreamError("GitHub installation inventory exceeded the pagination limit.")
        for installation in installations:
            if installation["suspended"]:
                installation["error"] = (
                    "Installation is suspended; repository access was not checked."
                )
                continue
            try:
                identity = installation["installation_id"]
                value = self._json(
                    "POST",
                    f"/app/installations/{identity}/access_tokens",
                    self.jwt(),
                    {"permissions": {"metadata": "read"}},
                )
                token = value.get("token")
                if not isinstance(token, str) or not token:
                    raise ValueError
                permissions = value.get("permissions", {})
                if any(mode not in {"read", "none"} for mode in permissions.values()):
                    raise ValueError
                repositories = []
                repo_ids = set()
                for page in range(1, 101):
                    value = self._json(
                        "GET", f"/installation/repositories?per_page=100&page={page}", token
                    )
                    rows = value["repositories"]
                    if not isinstance(rows, list):
                        raise ValueError
                    for row in rows:
                        repo_id = positive_id(row["id"])
                        if repo_id in repo_ids:
                            raise ValueError
                        repo_ids.add(repo_id)
                        repositories.append(
                            {
                                "repository": repository_name(row["full_name"]),
                                "repository_id": repo_id,
                            }
                        )
                    if len(rows) < 100:
                        break
                else:
                    raise ValueError
                installation["repositories"] = sorted(
                    repositories, key=lambda row: row["repository"]
                )
                installation["repositories_checked"] = True
            except (ServiceError, KeyError, TypeError, ValueError, AttributeError):
                # Partial failures must be explicit and never expose token response bodies.
                installation["error"] = (
                    "Repository access could not be checked; "
                    "retry after inspecting installation permissions and connectivity."
                )
        return sorted(installations, key=lambda row: row["installation_id"])

    def credentials(
        self, name: str, repository_id: int | None = None, installation_id: int | None = None
    ) -> tuple[str, int, int]:
        name = repository_name(name)
        jwt = self.jwt()
        installation = self._json("GET", f"/repos/{name}/installation", jwt)
        current = positive_id(installation["id"])
        if (
            installation.get("app_id") != self.app_id
            or installation.get("suspended_at") is not None
            or installation.get("repository_selection") != "selected"
            or (installation_id is not None and installation_id != current)
        ):
            raise ServiceError(403, "A current, selected-repository App installation is required.")
        permissions = installation.get("permissions", {})
        if any(
            permissions.get(key) not in ({"write"} if mode == "write" else {"read", "write"})
            for key, mode in PERMISSIONS.items()
        ):
            raise ServiceError(403, "App installation is missing required permissions.")
        with self.lock:
            cached = self.tokens.get((name, current))
            if cached is None or cached[1] <= time.time() + 60:
                value = self._json(
                    "POST",
                    f"/app/installations/{current}/access_tokens",
                    jwt,
                    {"repositories": [name.split("/")[1]], "permissions": PERMISSIONS},
                )
                expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
                token = value["token"]
                if (
                    not isinstance(token, str)
                    or not token
                    or expires.tzinfo is None
                    or expires <= datetime.now(timezone.utc)
                ):
                    raise UpstreamError("Invalid installation token response.")
                cached = (token, expires.timestamp())
                self.tokens[(name, current)] = cached
        repo = self._json("GET", f"/repos/{name}", cached[0])
        identity = positive_id(repo["id"])
        if repo["full_name"].lower() != name or (
            repository_id is not None and identity != repository_id
        ):
            raise ServiceError(
                403, "Repository identity changed; administrator enrollment is required."
            )
        return cached[0], identity, current

    def read_credentials(self, name: str, repository_id: int, installation_id: int):
        """Separate Actions-read credentials for analytics; never reuse write tokens."""
        name = repository_name(name)
        positive_id(repository_id)
        positive_id(installation_id)
        jwt = self.jwt()
        installation = self._json("GET", f"/repos/{name}/installation", jwt)
        if (
            installation.get("id") != installation_id
            or installation.get("app_id") != self.app_id
            or installation.get("suspended_at") is not None
            or installation.get("repository_selection") != "selected"
            or installation.get("permissions", {}).get("actions") not in {"read", "write"}
        ):
            raise ServiceError(403, "Current repository App authorization is required.")
        key = (name, repository_id, installation_id)
        with self.lock:
            cached = self.read_tokens.get(key)
            if cached is None or cached[1] <= time.time() + 60:
                value = self._json(
                    "POST",
                    f"/app/installations/{installation_id}/access_tokens",
                    jwt,
                    {
                        "repository_ids": [repository_id],
                        "permissions": {"actions": "read", "metadata": "read"},
                    },
                )
                permissions = value.get("permissions", {})
                if permissions.get("actions") != "read" or any(
                    mode not in {"read", "none"} for mode in permissions.values()
                ):
                    raise UpstreamError("Expected a read-only Actions token.")
                expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
                token = value.get("token")
                if (
                    not isinstance(token, str)
                    or not token
                    or expires.tzinfo is None
                    or expires.timestamp() <= time.time() + 60
                ):
                    raise UpstreamError("Invalid installation token response.")
                cached = token, expires.timestamp()
                self.read_tokens[key] = cached
        repo = self._json("GET", f"/repos/{name}", cached[0])
        if repo.get("id") != repository_id or repo.get("full_name", "").lower() != name:
            self.read_tokens.pop(key, None)
            raise ServiceError(403, "Repository identity changed; statistics access denied.")
        return cached

    def validate_workflows(
        self, name: str, repository_id: int, installation_id: int, workflows
    ) -> None:
        token, _, _ = self.credentials(name, repository_id, installation_id)
        for rule in workflows:
            filename = rule.path.rsplit("/", 1)[1]
            workflow = self._json(
                "GET", f"/repos/{repository_name(name)}/actions/workflows/{filename}", token
            )
            if workflow.get("path") != rule.path or workflow.get("state") != "active":
                raise ServiceError(
                    400, "Configured workflow must exist and be active in the repository."
                )

    def github(self, name: str, repository_id: int, installation_id: int) -> GitHub:
        provider = self
        refresh_lock = threading.Lock()

        class InstallationGitHub(GitHub):
            def _request(self, method, path, *, payload=None, write=False):
                # Fresh installation/repository authorization even with a cached token.
                # Serialize refreshes, but let independent metadata reads overlap.
                with refresh_lock:
                    cached = provider.tokens.get((name, installation_id))
                    if (
                        write
                        or self._token == "installation-managed"
                        or cached is None
                        or cached[1] <= time.time() + 60
                    ):
                        self._token = provider.credentials(name, repository_id, installation_id)[0]
                return super()._request(method, path, payload=payload, write=write)

        return InstallationGitHub(name, "installation-managed")
