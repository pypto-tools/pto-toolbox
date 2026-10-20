import copy
import io
import json
import zipfile
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from pr_rerun.api import create_app
from pr_rerun.github import CHECK_QUERY, GitHub
from pr_rerun.models import Settings

REPO = "hw-native-sys/pypto"
HEAD = "a" * 40


def archive(content: dict, name: str = "review-target.json") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(name, json.dumps(content))
    return buffer.getvalue()


class Remote:
    """A deterministic GitHub transport; every unrecognized operation fails the test."""

    def __init__(self):
        self.pr = {
            "number": 123,
            "node_id": "PR_123",
            "state": "open",
            "draft": False,
            "title": "Fix allocation",
            "user": {"id": 7, "login": "alice"},
            "head": {"sha": HEAD, "ref": "feature", "repo": {"id": 88}},
            "base": {"ref": "main", "repo": {"id": 44, "full_name": REPO}},
        }
        self.runs = {101: self.run(101, "ci"), 202: self.run(202, "codex-review")}
        self.linked = [101, 202]
        self.writes = []
        self.calls = []
        self.fail_write = None
        self.graph_head = HEAD
        self.manifest = {"repository": REPO, "number": 123, "head": HEAD, "base_ref": "main"}
        self.on_call = None

    def run(self, run_id: int, workflow: str) -> dict:
        return {
            "id": run_id,
            "run_attempt": 1,
            "run_number": run_id,
            "workflow_id": 11 if workflow == "ci" else 22,
            "path": f".github/workflows/{workflow}.yml",
            "name": "CI" if workflow == "ci" else "Codex Review",
            "repository": {"id": 44, "full_name": REPO},
            "event": "pull_request" if workflow == "ci" else "pull_request_target",
            "pull_requests": [
                {
                    "number": 123,
                    "head": {"sha": HEAD, "repo": {"id": 88}},
                    "base": {"ref": "main", "repo": {"id": 44}},
                }
            ]
            if workflow == "ci"
            else [],
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "failure",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        if self.on_call:
            self.on_call(request)
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer github-test-token"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        path = request.url.path
        if path == "/graphql":
            body = json.loads(request.content)
            assert body["query"] == CHECK_QUERY
            assert body["variables"]["pr"] == 123
            checks = [
                {
                    "__typename": "CheckRun",
                    "checkSuite": {
                        "workflowRun": {"resourcePath": f"/{REPO}/actions/runs/{run_id}"}
                    },
                }
                for run_id in self.linked
            ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "headRefOid": self.graph_head,
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "oid": self.graph_head,
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": checks,
                                                        "pageInfo": {
                                                            "hasNextPage": False,
                                                            "endCursor": None,
                                                        },
                                                    }
                                                },
                                            }
                                        }
                                    ]
                                },
                            }
                        }
                    },
                },
            )
        if path == f"/repos/{REPO}/pulls/123":
            return httpx.Response(200, json=copy.deepcopy(self.pr))
        if path == "/users/alice":
            return httpx.Response(200, json={"id": 7, "login": "alice", "type": "User"})
        for run_id, run in self.runs.items():
            prefix = f"/repos/{REPO}/actions/runs/{run_id}"
            if path == prefix:
                return httpx.Response(200, json=copy.deepcopy(run))
            if path == f"{prefix}/attempts/{run['run_attempt']}/jobs":
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "id": run_id * 10,
                                "name": "review" if run_id == 202 else "unit-tests",
                                "conclusion": "failure",
                            }
                        ]
                    },
                )
            if path == f"{prefix}/artifacts":
                return httpx.Response(
                    200,
                    json={
                        "artifacts": [
                            {
                                "id": run_id * 10,
                                "name": f"codex-target-{run_id}-1",
                                "expired": False,
                                "size_in_bytes": 400,
                            }
                        ]
                    },
                )
            if path in (f"{prefix}/rerun", f"{prefix}/rerun-failed-jobs"):
                assert request.method == "POST"
                assert not request.content
                self.writes.append(path)
                if self.fail_write:
                    return self.fail_write(request)
                return httpx.Response(201)
        if path == f"/repos/{REPO}/actions/artifacts/2020/zip":
            return httpx.Response(200, content=archive(self.manifest))
        raise AssertionError(f"Unexpected GitHub request: {request.method} {path}")


@pytest.fixture
def remote():
    return Remote()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database=tmp_path / "state.sqlite",
        github_token=SecretStr("github-test-token"),
        preview_interval=0,
        confirm_interval=0,
    )


@pytest.fixture
def github(remote):
    client = GitHub(REPO, "github-test-token", transport=httpx.MockTransport(remote))
    yield client
    client.close()


@pytest.fixture
def app(settings, github):
    return create_app(settings, github)


@pytest.fixture
def client(app):
    token = app.state.service.store.issue_token(7, "alice")
    with TestClient(app) as value:
        value.headers["Authorization"] = f"Bearer {token}"
        yield value


def preview(client):
    response = client.post("/api/reruns/preview", json={"pr": 123})
    assert response.status_code == 200, response.text
    assert response.json()["eligible"]
    return response.json()


def confirm(client, plan):
    return client.post(
        "/api/reruns/confirm",
        json={
            "ticket": plan["ticket"],
            "confirmation": plan["confirmation"],
        },
    )
