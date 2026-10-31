"""Real RSA JWT signing and mocked GitHub installation boundaries."""

import base64
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pr_rerun.app_auth import PERMISSIONS, AppAuth
from pr_rerun.github import GitHub
from pr_rerun.models import ServiceError, WorkflowRule


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "app.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def installation(key):
    state = {
        "installation": {
            "id": 55,
            "app_id": 1,
            "suspended_at": None,
            "repository_selection": "selected",
            "permissions": dict(PERMISSIONS),
        },
        "repository": {"id": 44, "full_name": "team/project"},
        "calls": [],
        "tokens": 0,
        "revoked": False,
    }

    def respond(request):
        state["calls"].append(request)
        assert request.url.host == "api.github.com"
        path = request.url.path
        if path.endswith("/installation"):
            return httpx.Response(404 if state["revoked"] else 200, json=state["installation"])
        if path == "/app/installations/55/access_tokens":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "repositories": ["project"],
                "permissions": PERMISSIONS,
            }
            state["tokens"] += 1
            return httpx.Response(
                201,
                json={
                    "token": f"installation-token-{state['tokens']}",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                },
            )
        if path == "/repos/team/project":
            assert request.headers["Authorization"].startswith("Bearer installation-token-")
            return httpx.Response(200, json=state["repository"])
        if path == "/repos/team/project/actions/workflows/ci.yml":
            return httpx.Response(200, json={"path": ".github/workflows/ci.yml", "state": "active"})
        raise AssertionError(f"Unexpected App request: {request.method} {path}")

    auth = AppAuth(1, key, httpx.MockTransport(respond))
    yield auth, state
    auth.close()


def test_jwt_is_real_rs256_and_does_not_include_key(key, tmp_path, monkeypatch):
    now = 1800000000
    monkeypatch.setattr("pr_rerun.app_auth.time.time", lambda: now)
    auth = AppAuth(123, key)
    try:
        token = auth.jwt()
        header, payload, signature = token.split(".")

        def decode(value):
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        assert json.loads(decode(header)) == {"alg": "RS256", "typ": "JWT"}
        claims = json.loads(decode(payload))
        assert claims["iss"] == "123"
        # GitHub must accept the time window even if the host is 90 seconds fast
        # or slow. Its exp limit is ten minutes beyond GitHub's current time.
        for github_now in (now - 90, now, now + 90):
            assert claims["iat"] <= github_now < claims["exp"] <= github_now + 600
        public = tmp_path / "public.pem"
        sig = tmp_path / "signature"
        sig.write_bytes(decode(signature))
        subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)],
            input=f"{header}.{payload}".encode(),
            capture_output=True,
            check=True,
        )
    finally:
        auth.close()


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "hardlink"])
def test_private_key_permissions_and_links_are_rejected(key, unsafe, tmp_path):
    if unsafe == "mode":
        key.chmod(0o644)
    elif unsafe == "symlink":
        link = tmp_path / "link"
        link.symlink_to(key)
        key = link
    else:
        os.link(key, tmp_path / "link")
    auth = AppAuth(1, key)
    try:
        with pytest.raises((ValueError, OSError)):
            auth.jwt()
    finally:
        auth.close()


def test_scoped_token_cache_refresh_and_live_revocation(installation):
    auth, state = installation
    assert auth.credentials("team/project", 44, 55) == ("installation-token-1", 44, 55)
    assert auth.credentials("TEAM/PROJECT", 44, 55) == ("installation-token-1", 44, 55)
    assert state["tokens"] == 1
    auth.tokens[("team/project", 55)] = ("installation-token-1", time.time() + 10)
    assert auth.credentials("team/project", 44, 55)[0] == "installation-token-2"
    state["revoked"] = True
    with pytest.raises(ServiceError):
        auth.credentials("team/project", 44, 55)


@pytest.mark.parametrize(
    "change",
    [
        "all_repos",
        "suspended",
        "wrong_app",
        "wrong_installation",
        "wrong_repo",
        "renamed_repo",
        "missing_actions",
    ],
)
def test_installation_and_repository_boundaries_fail_closed(installation, change):
    auth, state = installation
    if change == "all_repos":
        state["installation"]["repository_selection"] = "all"
    elif change == "suspended":
        state["installation"]["suspended_at"] = "2026-01-01T00:00:00Z"
    elif change == "wrong_app":
        state["installation"]["app_id"] = 2
    elif change == "wrong_installation":
        state["installation"]["id"] = 56
    elif change == "wrong_repo":
        state["repository"]["id"] = 99
    elif change == "renamed_repo":
        state["repository"]["full_name"] = "team/replacement"
    else:
        state["installation"]["permissions"]["actions"] = "read"
    with pytest.raises(ServiceError):
        auth.credentials("team/project", 44, 55)


def test_no_installation_token_stampede(installation):
    auth, state = installation
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.credentials("team/project", 44, 55), range(4)))
    assert len(set(results)) == 1
    assert state["tokens"] == 1


def test_revocation_between_read_and_write_prevents_rerun(installation, monkeypatch):
    auth, state = installation
    operations = []

    def request(self, method, path, *, payload=None, write=False):
        operations.append((method, path))
        return httpx.Response(201 if write else 200, json={})

    monkeypatch.setattr(GitHub, "_request", request)
    github = auth.github("team/project", 44, 55)
    try:
        github.pull(123)
        state["revoked"] = True
        with pytest.raises(ServiceError):
            github.rerun(1, "failed")
        assert len(operations) == 1 and operations[0][0] == "GET"
    finally:
        github.close()


def test_enrollment_checks_active_workflow_paths(installation):
    auth, _ = installation
    auth.validate_workflows("team/project", 44, 55, [WorkflowRule(path=".github/workflows/ci.yml")])
