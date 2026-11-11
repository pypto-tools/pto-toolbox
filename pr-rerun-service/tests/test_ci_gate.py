"""Native environment approval: signatures, provenance, retries, and fork PRs."""

import copy
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pr_rerun.app_auth import AppAuth
from pr_rerun.ci_gate import Worker, create_app, read_secret, reconcile
from pr_rerun.gate_github import GATE_PERMISSIONS, GateGitHub
from pr_rerun.gate_policy import GateConfig, GatePolicy, parse_event
from pr_rerun.gate_store import GateStore
from pr_rerun.models import ServiceError

HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
SECRET = b"test-webhook-secret-never-used-in-production"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    def date(seconds):
        return datetime.fromtimestamp(int(time.time()) + seconds, timezone.utc).isoformat()

    policy = GatePolicy(
        repository="team/project",
        repository_id=44,
        installation_id=55,
        workflow=".github/workflows/gate.yml",
        base_branch="sandbox",
        environment="ci-full",
        lint_jobs=["lint", "clang-tidy"],
    )
    event = {
        "kind": "gate",
        "repository_id": 44,
        "installation_id": 55,
        "run": 66,
        "deployment": 77,
        "sha": MERGE,
        "created_at": date(-60),
        "environment": "ci-full",
    }
    state = {
        "installation": {
            "id": 55,
            "app_id": 1,
            "repository_selection": "selected",
            "permissions": GATE_PERMISSIONS,
        },
        "run": {
            "id": 66,
            "display_title": f"CI gate PR #3 @{HEAD} base:{BASE}",
            "event": "pull_request",
            "repository": {"id": 44},
            "head_repository": {"id": 99},
            "head_branch": "feature",
            "head_sha": HEAD,
            "path": policy.workflow,
            "workflow_id": 88,
            "status": "waiting",
            "run_attempt": 1,
            "created_at": date(-120),
            "run_started_at": date(-90),
            "pull_requests": [],
        },
        "pull": {
            "number": 3,
            "state": "open",
            "head": {"sha": HEAD, "ref": "feature", "repo": {"id": 99}},
            "base": {"sha": BASE, "ref": "sandbox", "repo": {"id": 44}},
        },
        "jobs": [{"name": name, "conclusion": "success"} for name in policy.lint_jobs],
        "comments": [
            {
                "id": 101,
                "body": "/ci full",
                "user": {"type": "User", "login": "any-contributor"},
                "created_at": date(-30),
                "updated_at": date(-30),
            }
        ],
        "parents": [{"sha": BASE}, {"sha": HEAD}],
        "writes": [],
        "tokens": [],
        "run_reads": 0,
    }

    def respond(request):
        path = request.url.path
        if path.endswith("/installation"):
            return httpx.Response(200, json=state["installation"])
        if path == "/app/installations/55/access_tokens":
            state["tokens"].append(json.loads(request.content))
            return httpx.Response(
                201, json={"token": "gate-only-token", "permissions": GATE_PERMISSIONS}
            )
        if path == "/repos/team/project":
            return httpx.Response(200, json={"id": 44, "full_name": policy.repository})
        if path.endswith("/deployment_protection_rule"):
            assert request.method == "POST"
            state["writes"].append(json.loads(request.content))
            return httpx.Response(state.get("write_status", 204))
        if path.endswith("/actions/runs/66"):
            state["run_reads"] += 1
            if state.get("mutate_on_recheck") and state["run_reads"] == 2:
                state["pull"]["head"]["sha"] = "d" * 40
            return httpx.Response(200, json=state["run"])
        if path.endswith("/actions/workflows/88"):
            return httpx.Response(200, json={"path": policy.workflow, "state": "active"})
        if path.endswith("/pulls/3"):
            return httpx.Response(200, json=state["pull"])
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": state["jobs"]})
        if path.endswith("/comments"):
            return httpx.Response(200, json=state["comments"])
        if path.endswith("/git/commits/" + MERGE):
            return httpx.Response(200, json={"parents": state["parents"]})
        raise AssertionError(f"Unexpected request: {request.method} {path}")

    transport = httpx.MockTransport(respond)
    auth = AppAuth(1, tmp_path / "unused-key", transport)
    monkeypatch.setattr(auth, "jwt", lambda: "test-jwt")
    client = GateGitHub(auth, policy, transport)
    store = GateStore(tmp_path / "gate.sqlite")
    yield policy, event, client, store, state, auth, transport
    client.close()
    auth.close()


def test_external_fork_and_narrow_token_approve_once(gate):
    policy, event, client, store, state, *_ = gate
    assert reconcile(policy, event, client, store) == "done"
    assert state["writes"] == [
        {
            "environment_name": "ci-full",
            "state": "approved",
            "comment": "Fresh /ci full comment verified for PR #3.",
        }
    ]
    assert all(
        token == {"repository_ids": [44], "permissions": GATE_PERMISSIONS}
        for token in state["tokens"]
    )
    assert len(state["tokens"]) == 2  # Reauthorize before approving, not just before reads.
    assert reconcile(policy, event, client, store) == "done"
    assert len(state["writes"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "no_comment",
        "old_comment",
        "edited_comment",
        "bot",
        "wrong_command",
        "lint_failure",
        "lint_skipped",
        "duplicate_lint",
        "wrong_sha",
        "closed",
        "wrong_base",
        "wrong_branch",
        "wrong_repository",
        "wrong_workflow",
        "wrong_event",
        "wrong_head_repository",
        "wrong_merge",
        "push_during_reads",
        "stale_attempt",
        "old_deployment",
    ],
)
def test_no_approval_when_evidence_does_not_match(gate, mutation):
    policy, event, client, store, state, *_ = gate
    comment = state["comments"][0]
    if mutation == "no_comment":
        state["comments"] = []
    elif mutation == "old_comment":
        comment["created_at"] = comment["updated_at"] = state["run"]["created_at"]
    elif mutation == "edited_comment":
        comment["updated_at"] = event["created_at"]
    elif mutation == "bot":
        comment["user"]["type"] = "Bot"
    elif mutation == "wrong_command":
        comment["body"] = "Please /ci full now"
    elif mutation in {"lint_failure", "lint_skipped"}:
        state["jobs"][0]["conclusion"] = mutation.removeprefix("lint_")
    elif mutation == "duplicate_lint":
        state["jobs"].append(state["jobs"][0])
    elif mutation == "wrong_sha":
        state["pull"]["head"]["sha"] = BASE
    elif mutation == "closed":
        state["pull"]["state"] = "closed"
    elif mutation == "wrong_base":
        state["pull"]["base"]["sha"] = HEAD
    elif mutation == "wrong_branch":
        state["pull"]["base"]["ref"] = "main"
    elif mutation == "wrong_repository":
        state["run"]["repository"]["id"] = 123
    elif mutation == "wrong_workflow":
        state["run"]["path"] = ".github/workflows/other.yml"
    elif mutation == "wrong_event":
        state["run"]["event"] = "workflow_dispatch"
    elif mutation == "wrong_head_repository":
        state["pull"]["head"]["repo"]["id"] = 123
    elif mutation == "wrong_merge":
        state["parents"].reverse()
    elif mutation == "push_during_reads":
        state["mutate_on_recheck"] = True
    elif mutation == "stale_attempt":
        event["attempt"] = 2
    elif mutation == "old_deployment":
        event["created_at"] = state["run"]["created_at"]
    assert reconcile(policy, event, client, store) in {"waiting", "obsolete"}
    assert state["writes"] == []


@pytest.mark.parametrize(
    "field,value",
    [("suspended_at", "now"), ("id", 56), ("app_id", 2), ("repository_selection", "all")],
)
def test_revoked_or_changed_installation_fails_closed(gate, field, value):
    policy, event, client, store, state, *_ = gate
    state["installation"][field] = value
    with pytest.raises(ServiceError):
        reconcile(policy, event, client, store)
    assert state["writes"] == []


def webhook_payload(policy, event):
    return {
        "action": "requested",
        "event": "pull_request",
        "environment": policy.environment,
        "repository": {"id": policy.repository_id, "full_name": policy.repository},
        "installation": {"id": policy.installation_id},
        "deployment_callback_url": f"https://api.github.com/repos/{policy.repository}/actions/runs/66/deployment_protection_rule",
        "deployment": {
            "id": event["deployment"],
            "sha": event["sha"],
            "created_at": event["created_at"],
            "environment": policy.environment,
        },
    }


def post_webhook(
    client, payload, *, event="deployment_protection_rule", delivery="delivery-1", signed=True
):
    data = json.dumps(payload).encode()
    return client.post(
        "/github/ci-gate",
        content=data,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-github-delivery": delivery,
            "x-hub-signature-256": "sha256="
            + hmac.new(SECRET if signed else b"wrong", data, hashlib.sha256).hexdigest(),
        },
    )


def test_signature_and_durable_duplicate_delivery(gate, tmp_path):
    policy, event, _, _, _, auth, _ = gate
    config = GateConfig(repositories=[policy])
    database = tmp_path / "webhooks.sqlite"
    app = create_app(config, database, SECRET, auth, worker_enabled=False)
    with TestClient(app) as client:
        payload = webhook_payload(policy, event)
        assert post_webhook(client, payload, signed=False).status_code == 401
        assert app.state.store.next() is None
        assert post_webhook(client, payload).status_code == 202
        assert post_webhook(client, payload).status_code == 202
        assert client.get("/openapi.json").status_code == 404
        assert client.post("/v1/confirm").status_code == 404
    restored = GateStore(database)
    row = restored.next()
    assert row is not None
    assert json.loads(row["payload"]) == event
    restored.update(row["delivery"], event, "done")
    assert restored.next() is None


@pytest.mark.parametrize(
    "target",
    [
        "foreign_repo",
        "foreign_installation",
        "wrong_environment",
        "evil_url",
        "extra_url_path",
        "wrong_event",
    ],
)
def test_unenrolled_events_are_not_queued(gate, target):
    policy, event, *_ = gate
    payload = webhook_payload(policy, event)
    if target == "foreign_repo":
        payload["repository"]["id"] = 99
    elif target == "foreign_installation":
        payload["installation"]["id"] = 56
    elif target == "wrong_environment":
        payload["environment"] = "production"
    elif target == "evil_url":
        payload["deployment_callback_url"] = payload["deployment_callback_url"].replace(
            "api.github.com", "evil.example"
        )
    elif target == "extra_url_path":
        payload["deployment_callback_url"] += "?redirect=evil"
    elif target == "wrong_event":
        payload["event"] = "push"
    assert (
        parse_event(GateConfig(repositories=[policy]), "deployment_protection_rule", payload)
        is None
    )


def test_comment_wakes_waiting_gate_and_restart_retains_it(gate):
    policy, event, _, store, state, auth, transport = gate
    comments = state["comments"]
    state["comments"] = []
    worker = Worker(
        GateConfig(repositories=[policy]),
        store,
        auth,
        factory=lambda a, p: GateGitHub(a, p, transport),
    )
    store.enqueue("gate", event)
    assert worker.tick()
    assert state["writes"] == []
    assert store.next() is None
    state["comments"] = comments
    store.enqueue(
        "comment", {"kind": "comment", "repository_id": 44, "installation_id": 55, "pr": 3}
    )
    worker.store = GateStore(store.path)
    assert worker.tick()
    assert worker.tick()
    assert len(state["writes"]) == 1


def test_comment_arriving_before_gate_is_found_by_reconciliation(gate):
    policy, event, _, store, state, auth, transport = gate
    worker = Worker(
        GateConfig(repositories=[policy]),
        store,
        auth,
        factory=lambda a, p: GateGitHub(a, p, transport),
    )
    store.enqueue(
        "comment", {"kind": "comment", "repository_id": 44, "installation_id": 55, "pr": 3}
    )
    worker.tick()
    store.enqueue("gate", event)
    worker.tick()
    assert len(state["writes"]) == 1


def test_uncertain_write_retries_without_marking_approved(gate):
    policy, event, _, store, state, auth, transport = gate
    worker = Worker(
        GateConfig(repositories=[policy]),
        store,
        auth,
        factory=lambda a, p: GateGitHub(a, p, transport),
    )
    state["write_status"] = 503
    store.enqueue("gate", event)
    worker.tick()
    assert not store.was_approved(44, 66, 1)
    with store.connection() as db:
        row = db.execute("SELECT * FROM events WHERE delivery='gate'").fetchone()
        assert row["state"] == "waiting" and row["failures"] == 1
        db.execute("UPDATE events SET due=0")
    state["write_status"] = 204
    worker.tick()
    assert store.was_approved(44, 66, 1)


def test_gate_client_cannot_rerun_or_follow_arbitrary_urls(gate):
    client = gate[2]
    for method, path in [("POST", "actions/runs/66/rerun"), ("GET", "https://evil.example")]:
        with pytest.raises(ValueError, match="Unsupported"):
            client._request(method, path)


def test_secret_file_permissions(tmp_path):
    path = tmp_path / "test-secret"
    path.write_bytes(SECRET)
    path.chmod(0o600)
    assert read_secret(path) == SECRET
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        read_secret(path)


def test_missing_deployment_permission_does_not_mint_a_token(gate):
    policy, event, client, store, state, *_ = gate
    state["installation"] = copy.deepcopy(state["installation"])
    state["installation"]["permissions"].pop("deployments")
    with pytest.raises(ServiceError):
        reconcile(policy, event, client, store)
    assert state["tokens"] == []


def test_failed_delivery_can_be_redelivered_but_completed_delivery_stays_done(gate):
    _, event, _, store, *_ = gate
    store.enqueue("delivery", event)
    store.update("delivery", event, "failed", failures=20)
    assert store.next() is None
    assert store.enqueue("delivery", event)
    row = store.next()
    assert row is not None and row["failures"] == 0
    store.update("delivery", event, "done")
    assert store.enqueue("delivery", event)
    assert store.next() is None


def test_issue_comment_filter_accepts_any_human_but_not_edits_or_issues(gate):
    policy, _, _, _, state, *_ = gate
    config = GateConfig(repositories=[policy])
    payload = {
        "action": "created",
        "repository": {"id": 44, "full_name": policy.repository},
        "installation": {"id": 55},
        "issue": {"number": 3, "pull_request": {"url": "unused"}},
        "comment": state["comments"][0],
    }
    assert parse_event(config, "issue_comment", payload) == {
        "kind": "comment",
        "repository_id": 44,
        "installation_id": 55,
        "pr": 3,
    }
    payload["action"] = "edited"
    assert parse_event(config, "issue_comment", payload) is None
    payload["action"] = "created"
    payload["issue"].pop("pull_request")
    assert parse_event(config, "issue_comment", payload) is None
