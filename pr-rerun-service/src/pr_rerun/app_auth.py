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

    def _json(self, method: str, path: str, token: str, payload: dict | None = None) -> dict:
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
                if not isinstance(value, dict):
                    raise ValueError
                return value
        except (httpx.HTTPError, ValueError):
            raise UpstreamError("GitHub App authorization could not be verified.") from None

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
