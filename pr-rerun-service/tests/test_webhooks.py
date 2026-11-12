"""Comment authentication, durable delivery, and author-only rerun boundaries."""

import copy
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from conftest import REPO
from fastapi.testclient import TestClient
from test_registry import FakeApp

from pr_rerun.api import create_app
from pr_rerun.policy import RepositoryPolicy
from pr_rerun.webhooks import BODY_LIMIT, CommentCommands, read_secret

SECRET = b"webhook-test-secret-" + b"x" * 32


@pytest.fixture
def hook(settings, tmp_path, monkeypatch):
    key = tmp_path / "webhook.secret"
    key.write_bytes(SECRET)
    key.chmod(0o600)
    settings = settings.model_copy(
        update={
            "app_id": 1,
            "app_key_file": Path("/unused"),
            "webhook_secret_file": key,
        }
    )
    auth = FakeApp()
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    comment = {
        "id": 400,
        "body": "/ci rerun",
        "user": {"id": 7, "login": "alice", "type": "User"},
        "created_at": created,
        "updated_at": created,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/123",
    }
    parent = auth.remote

    def remote(request):
        if request.url.path == f"/repos/{REPO}/issues/comments/{comment['id']}":
            parent.calls.append((request.method, str(request.url)))
            if parent.on_call:
                parent.on_call(request)
            return httpx.Response(200, json=copy.deepcopy(comment))
        return parent(request)

    monkeypatch.setattr(auth, "remote", remote)
    monkeypatch.setattr(CommentCommands, "start", lambda _: None)
    app = create_app(settings, app_auth=auth)
    registry = app.state.service
    registry.store.issue_token(8, "manager")
    registry.policies.enroll(
        REPO,
        44,
        55,
        RepositoryPolicy(
            maintainers=[8],
            access="authenticated_users",
            workflows=list(settings.workflows),
            cooldown_seconds=0,
            comment_reruns_enabled=True,
        ),
    )
    payload = {
        "action": "created",
        "comment": comment,
        "sender": comment["user"].copy(),
        "repository": {"id": 44, "full_name": REPO},
        "installation": {"id": 55},
        "issue": {
            "number": 123,
            "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/123"},
        },
    }
    with TestClient(app) as client:
        yield client, app.state.comment_commands, parent, payload, comment, registry, auth


def post(hook, payload=None, delivery="delivery-1", event="issue_comment", signature=None):
    body = json.dumps(payload if payload is not None else hook[3]).encode()
    return hook[0].post(
        "/github/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature
            or "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
        },
    )


def rows(hook):
    with hook[1].store.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM comment_commands ORDER BY id")]


def test_delivery_is_read_only_then_worker_uses_existing_rerun_modes(hook):
    response = post(hook)
    assert response.json() == {"state": "queued"}
    assert not hook[2].calls and not hook[2].writes
    assert hook[1].process_next()
    assert rows(hook)[0]["state"] == "accepted"
    assert hook[2].writes == [
        f"/repos/{REPO}/actions/runs/101/rerun-failed-jobs",
        f"/repos/{REPO}/actions/runs/202/rerun",
    ]
    assert not hook[1].process_next()
    with hook[1].store.connection() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "signature", ["sha256=" + "0" * 64, "sha1=1234", "missing", "sha256=invalid"]
)
def test_signature_is_required_before_parsing_or_enrollment(hook, signature):
    response = post(hook, signature=signature)
    assert response.status_code == 401
    assert not rows(hook) and not hook[2].calls


def test_ping_and_unknown_event_do_not_create_commands(hook):
    assert post(hook, {}, event="ping").json() == {"state": "pong"}
    assert post(hook, {}, event="push").json() == {"state": "ignored"}
    assert not rows(hook)


def test_repeated_delivery_and_repeated_comment_are_deduplicated(hook):
    assert post(hook).json()["state"] == "queued"
    assert post(hook).json()["state"] == "duplicate"
    assert post(hook, delivery="delivery-2").json()["state"] == "duplicate"
    hook[1].process_next()
    assert post(hook, delivery="delivery-3").json()["state"] == "duplicate"
    assert len(hook[2].writes) == 2


@pytest.mark.parametrize(
    "body",
    ["/ci full", "/ci rerun all", "hello /ci rerun", "```\n/ci rerun\n```", "/ci rerun\nanything"],
)
def test_only_exact_comment_command_is_accepted(hook, body):
    hook[3]["comment"]["body"] = body
    assert post(hook).json()["state"] == "ignored"
    assert not rows(hook)


@pytest.mark.parametrize(
    "change", ["edit", "issue", "bot", "sender", "repository", "installation", "expired", "future"]
)
def test_ineligible_delivery_does_not_enqueue(hook, change):
    payload = hook[3]
    if change == "edit":
        payload["action"] = "edited"
    elif change == "issue":
        payload["issue"].pop("pull_request")
    elif change == "bot":
        payload["comment"]["user"]["type"] = "Bot"
    elif change == "sender":
        payload["sender"]["id"] = 999
    elif change == "repository":
        payload["repository"]["id"] = 99
    elif change == "installation":
        payload["installation"]["id"] = 99
    else:
        date = datetime.now(timezone.utc) + timedelta(seconds=60 if change == "future" else -700)
        payload["comment"]["created_at"] = payload["comment"]["updated_at"] = date.isoformat()
    assert post(hook).json()["state"] in {"ignored", "expired"}
    assert not rows(hook)


@pytest.mark.parametrize(
    "change",
    [
        "author",
        "comment",
        "deleted_body",
        "issue",
        "edited",
        "disabled",
        "revoked",
        "installation",
        "expired",
    ],
)
def test_worker_revalidates_before_any_rerun(hook, change):
    assert post(hook).json()["state"] == "queued"
    if change == "author":
        hook[2].pr["user"]["id"] = 999
    elif change == "comment":
        hook[4]["user"]["id"] = 999
    elif change == "deleted_body":
        hook[4]["body"] = ""
    elif change == "issue":
        hook[4]["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/999"
    elif change == "edited":
        hook[4]["updated_at"] = datetime.now(timezone.utc).isoformat()
    elif change == "revoked":
        hook[5].store.issue_token(7, "alice")
        hook[5].store.revoke(7)
    elif change == "installation":
        hook[6].allowed = False
    elif change == "expired":
        hook[5].store.clock = lambda: datetime.now(timezone.utc).timestamp() + 700
    else:
        with hook[5].store.connection() as db:
            db.execute(
                "UPDATE repositories SET "
                "policy=json_set(policy,'$.comment_reruns_enabled',json('false'))"
            )
    hook[1].process_next()
    assert rows(hook)[0]["state"] == "rejected"
    assert not hook[2].writes


def test_foreign_author_does_not_get_service_identity(hook):
    hook[2].pr["user"]["id"] = 999
    post(hook)
    hook[1].process_next()
    assert [u["github_id"] for u in hook[5].store.users()] == [8]


def test_comment_changes_immediately_before_write_prevent_submission(hook):
    post(hook)
    reads = 0

    def change(request):
        nonlocal reads
        if request.url.path.endswith("/issues/comments/400"):
            reads += 1
            if reads == 2:
                hook[4]["body"] = "/ci full"

    hook[2].on_call = change
    hook[1].process_next()
    assert not hook[2].writes
    assert rows(hook)[0]["state"] == "rejected"


def test_existing_cli_login_survives_comment_command(hook):
    token = hook[5].store.login_session(7, "alice")
    post(hook)
    hook[1].process_next()
    assert hook[5].store.authenticate("Bearer " + token).github_id == 7


def test_unknown_post_outcome_is_not_replayed(hook):
    hook[2].fail_write = lambda request: httpx.Response(503)
    post(hook)
    hook[1].process_next()
    assert rows(hook)[0]["state"] == "unknown"
    post(hook, delivery="another-delivery")
    assert not hook[1].process_next()
    assert len(hook[2].writes) == 1


def test_request_body_boundaries_and_malformed_payload(hook):
    assert (
        hook[0]
        .post(
            "/github/webhook",
            content=b"x" * (BODY_LIMIT + 1),
            headers={"Content-Type": "application/json"},
        )
        .status_code
        == 413
    )
    assert post(hook, {"action": "created"}).status_code == 400
    assert not rows(hook)


def test_secret_file_is_private_and_not_a_symlink(tmp_path):
    key = tmp_path / "secret"
    key.write_bytes(SECRET)
    key.chmod(0o600)
    assert read_secret(key) == SECRET
    key.chmod(0o644)
    with pytest.raises(ValueError):
        read_secret(key)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(OSError):
        read_secret(link)


def test_webhook_unconfigured_by_default(client):
    assert client.post("/github/webhook", json={}).status_code == 404


def test_queue_full_is_not_acknowledged(hook):
    post(hook)
    with hook[5].store.connection() as db:
        row = rows(hook)[0]
        for i in range(1, 1000):
            db.execute(
                """INSERT INTO comment_commands
                (delivery,repository,repository_id,installation_id,pr,comment_id,actor,login,created,received,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"fill-{i}",
                    REPO,
                    44,
                    55,
                    123,
                    400 + i,
                    7,
                    "alice",
                    row["created"],
                    row["received"],
                    "queued",
                ),
            )
    hook[3]["comment"]["id"] = 5000
    assert post(hook, delivery="full").status_code == 503


def test_explicit_user_policy_is_not_bypassed_by_github_comment(hook):
    with hook[5].store.connection() as db:
        db.execute("UPDATE repositories SET policy=json_set(policy,'$.access','explicit_users')")
    post(hook)
    hook[1].process_next()
    assert rows(hook)[0]["state"] == "rejected"
    assert not hook[2].writes


def test_new_command_cannot_rerun_same_run_attempt_twice(hook):
    post(hook)
    hook[1].process_next()
    hook[3]["comment"]["id"] = 401
    post(hook, delivery="second-command")
    hook[1].process_next()
    assert len(hook[2].writes) == 2
    assert rows(hook)[1]["state"] == "rejected"


def test_internal_identity_revocation_during_validation_blocks_write(hook):
    post(hook)
    calls = 0

    def revoke(request):
        nonlocal calls
        if request.url.path.endswith("/pulls/123"):
            calls += 1
            if calls == 4:
                hook[5].store.revoke(7)

    hook[2].on_call = revoke
    hook[1].process_next()
    assert not hook[2].writes
    assert rows(hook)[0]["state"] == "rejected"


def test_recovery_does_not_replay_interrupted_submissions(hook):
    post(hook)
    with hook[5].store.connection() as db:
        db.execute("UPDATE comment_commands SET state='processing', request_id='rr_pending'")
    hook[1].recover()
    assert rows(hook)[0]["state"] == "interrupted"
    assert not hook[1].process_next()
    assert not hook[2].writes


def test_recovery_keeps_unprocessed_commands(hook):
    post(hook)
    hook[1].recover()
    assert hook[1].process_next()
    assert rows(hook)[0]["state"] == "accepted"
