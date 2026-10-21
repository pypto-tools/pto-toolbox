import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import HEAD, REPO, confirm, preview
from fastapi.testclient import TestClient

from pr_rerun.api import create_app
from pr_rerun.github import HEAD_PULLS_QUERY, GitHub
from pr_rerun.models import ServiceError
from pr_rerun.store import Store


def test_preview_is_read_only_and_confirmation_uses_correct_modes(client, remote):
    plan = preview(client)
    assert remote.writes == []
    assert plan["pr"]["author"] == "alice"
    assert plan["pr"]["head_sha"] == HEAD
    assert [run["mode"] for run in plan["runs"]] == ["failed", "all"]
    result = confirm(client, plan)
    assert result.status_code == 202
    assert result.json()["state"] == "accepted"
    assert remote.writes == [
        f"/repos/{REPO}/actions/runs/101/rerun-failed-jobs",
        f"/repos/{REPO}/actions/runs/202/rerun",
    ]
    status = client.get(f"/api/reruns/{plan['request_id']}")
    assert status.json()["state"] == "accepted"
    assert "ticket" not in status.json()


def test_other_author_is_rejected_before_check_lookup(client, remote):
    remote.pr["user"]["id"] = 99
    assert client.post("/api/reruns/preview", json={"pr": 123}).status_code == 403
    assert len(remote.calls) == 1
    assert not remote.writes


def test_username_change_preserves_numeric_identity(client, remote):
    remote.pr["user"]["login"] = "alice-renamed"
    plan = preview(client)
    assert "@alice-renamed" in plan["confirmation"]
    assert confirm(client, plan).json()["state"] == "accepted"


def test_wrong_confirmation_does_not_consume_ticket(client, remote):
    plan = preview(client)
    bad = dict(plan, confirmation="hw-native-sys/pypto#999 @alice")
    assert confirm(client, bad).status_code == 400
    assert not remote.writes
    assert confirm(client, plan).json()["state"] == "accepted"


def test_ticket_is_bound_to_user_and_credential(client, app, remote):
    plan = preview(client)
    other = app.state.service.store.issue_token(9, "bob")
    client.headers["Authorization"] = f"Bearer {other}"
    assert confirm(client, plan).status_code == 404
    assert client.get(f"/api/reruns/{plan['request_id']}").status_code == 404
    replacement = app.state.service.store.issue_token(7, "alice")
    client.headers["Authorization"] = f"Bearer {replacement}"
    assert confirm(client, plan).status_code == 404
    assert not remote.writes


def test_revoked_token_cannot_preview_or_confirm(client, app):
    plan = preview(client)
    app.state.service.store.revoke(7)
    assert confirm(client, plan).status_code == 401
    assert client.post("/api/reruns/preview", json={"pr": 123}).status_code == 401


def test_expired_and_superseded_preview(client, app, remote):
    first = preview(client)
    second = preview(client)
    assert confirm(client, first).status_code == 409
    with app.state.service.store.connection() as db:
        db.execute("UPDATE requests SET expires=0 WHERE id=?", (second["request_id"],))
    assert confirm(client, second).status_code == 409
    assert not remote.writes


@pytest.mark.parametrize(
    "change", ["head", "base", "title", "closed", "draft", "attempt", "running", "success"]
)
def test_changed_state_requires_new_preview(client, remote, change):
    plan = preview(client)
    if change == "head":
        remote.pr["head"]["sha"] = "b" * 40
        remote.graph_head = "b" * 40
    elif change == "base":
        remote.pr["base"]["ref"] = "release"
    elif change == "title":
        remote.pr["title"] = "A different explanation"
    elif change == "closed":
        remote.pr["state"] = "closed"
    elif change == "draft":
        remote.pr["draft"] = True
    elif change == "attempt":
        remote.runs[101]["run_attempt"] = 2
    elif change == "running":
        remote.runs[101]["status"] = "in_progress"
    else:
        remote.runs[101]["conclusion"] = "success"
    assert confirm(client, plan).json()["state"] == "rejected"
    assert not remote.writes


def test_confirm_replay_is_idempotent(client, remote):
    plan = preview(client)
    first = confirm(client, plan).json()
    second = confirm(client, plan).json()
    assert first == second
    assert len(remote.writes) == 2
    assert confirm(client, preview(client)).json()["state"] == "rejected"
    assert len(remote.writes) == 2


def test_concurrent_confirmation_sends_each_write_once(client, app, remote):
    plan = preview(client)
    actor = app.state.service.store.authenticate(client.headers["Authorization"])

    def submit():
        return app.state.service.confirm(actor, plan["ticket"], plan["confirmation"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(), range(4)))
    assert len({result["request_id"] for result in results}) == 1
    assert len(remote.writes) == 2


def test_timeout_is_unknown_stops_batch_and_never_auto_retries(client, remote):
    plan = preview(client)

    def timeout(request):
        raise httpx.ReadTimeout("a message that must not reach the user", request=request)

    remote.fail_write = timeout
    result = confirm(client, plan).json()
    assert result["state"] == "unknown"
    assert [run["state"] for run in result["runs"]] == ["unknown", "not_attempted"]
    assert "must not reach" not in json.dumps(result)
    assert len(remote.writes) == 1
    assert confirm(client, preview(client)).json()["state"] == "rejected"
    assert len(remote.writes) == 1
    remote.runs[101]["run_attempt"] = 2
    state = client.get(f"/api/reruns/{plan['request_id']}").json()
    assert state["runs"][0]["state"] == "rerun_observed"
    assert len(remote.writes) == 1  # Reconciliation only reads.


def test_partial_success_and_known_rejection_release_only_unsubmitted_claims(client, app, remote):
    def failure(request):
        return httpx.Response(403 if request.url.path.endswith("/rerun") else 201)

    remote.fail_write = failure
    plan = preview(client)
    result = confirm(client, plan).json()
    assert result["state"] == "partial"
    assert [run["state"] for run in result["runs"]] == ["accepted", "rejected"]
    with app.state.service.store.connection() as db:
        assert [row["run_id"] for row in db.execute("SELECT run_id FROM claims")] == [101]


def test_immediate_revalidation_stops_a_push_between_workflows(client, remote):
    original = remote.fail_write

    def push_after_first(request):
        remote.pr["head"]["sha"] = "b" * 40
        return httpx.Response(201)

    remote.fail_write = push_after_first
    result = confirm(client, preview(client)).json()
    assert [run["state"] for run in result["runs"]] == ["accepted", "rejected"]
    assert len(remote.writes) == 1
    remote.fail_write = original


def test_restart_preserves_deduplication_and_status(settings, remote):
    first = create_app(settings, GitHub(REPO, "github-test-token", httpx.MockTransport(remote)))
    token = first.state.service.store.issue_token(7, "alice")
    with TestClient(first, headers={"Authorization": f"Bearer {token}"}) as client:
        plan = preview(client)
        confirm(client, plan)
    second = create_app(settings, GitHub(REPO, "github-test-token", httpx.MockTransport(remote)))
    with TestClient(second, headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.get(f"/api/reruns/{plan['request_id']}").json()["state"] == "accepted"
        assert confirm(client, preview(client)).json()["state"] == "rejected"
    assert len(remote.writes) == 2


def test_recovery_keeps_unknown_claim_and_releases_reserved(app, client):
    view = preview(client)
    service = app.state.service
    principal = service.store.authenticate(client.headers["Authorization"])
    request_id, plan, _ = service.store.claim(principal, view["ticket"], view["confirmation"])
    service.store.reserve(request_id, plan)
    service.store.item(request_id, 101, "submitting")
    assert service.store.recover() == 1
    result = service.store.result(principal, request_id)
    assert result["state"] == "interrupted"
    assert [item["state"] for item in result["runs"]] == ["unknown", "not_attempted"]
    with service.store.connection() as db:
        assert [row["run_id"] for row in db.execute("SELECT run_id FROM claims")] == [101]


def test_second_server_cannot_recover_an_active_database(client, settings, remote):
    other = create_app(settings, GitHub(REPO, "github-test-token", httpx.MockTransport(remote)))
    with pytest.raises(RuntimeError, match="Another server"):
        with TestClient(other):
            pass


def test_rate_limit_is_persistent_and_database_has_private_permissions(tmp_path):
    path = tmp_path / "store.sqlite"
    store = Store(path, clock=lambda: 100.0)
    store.rate_limit("alice:preview", 5)
    with pytest.raises(ServiceError) as error:
        Store(path, clock=lambda: 101.0).rate_limit("alice:preview", 5)
    assert error.value.status == 429
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "body",
    [
        {"pr": True},
        {"pr": "123"},
        {"pr": -1},
        {"pr": 123, "run_id": 777},
        {"pr": 123, "repository": "other/repo"},
        {"pr": 123, "mode": "all"},
    ],
)
def test_untrusted_selection_fields_are_rejected(client, remote, body):
    assert client.post("/api/reruns/preview", json=body).status_code == 422
    assert not remote.calls


def test_http_boundary_and_secret_redaction(client, remote):
    assert client.get("/api/reruns/preview").status_code in {404, 405}
    assert client.post("/api/cancel", json={}).status_code == 404
    assert client.post("/api/reruns/preview", content="pr=123").status_code == 415
    assert (
        client.post(
            "/api/reruns/preview",
            content=b"x" * 4097,
            headers={"Content-Type": "application/json"},
        ).status_code
        == 413
    )
    response = client.post("/api/reruns/preview", json={"pr": "github-test-token"})
    assert "github-test-token" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    assert not remote.calls


@pytest.mark.parametrize(
    "state,conclusion",
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "success"),
        ("completed", "cancelled"),
        ("completed", "action_required"),
        ("completed", "skipped"),
    ],
)
def test_ineligible_runs_are_reported_without_ticket(client, remote, state, conclusion):
    for run in remote.runs.values():
        run.update(status=state, conclusion=conclusion)
    result = client.post("/api/reruns/preview", json={"pr": 123}).json()
    assert not result["eligible"]
    assert "ticket" not in result
    assert len(result["skipped"]) == 2


def test_latest_success_prevents_resurrecting_older_failure(client, remote):
    newer = copy.deepcopy(remote.runs[101])
    newer.update(id=303, run_number=303, conclusion="success")
    remote.runs[303] = newer
    remote.linked.append(303)
    plan = preview(client)
    assert [run["run_id"] for run in plan["runs"]] == [202]


@pytest.mark.parametrize("identity", ["number", "head", "base_ref", "repository"])
def test_codex_manifest_cannot_route_another_pr(client, remote, identity):
    remote.manifest[identity] = 999 if identity == "number" else "unrelated"
    plan = preview(client)
    assert [run["run_id"] for run in plan["runs"]] == [101]
    assert any("202" in reason and "verified" in reason for reason in plan["skipped"])


def test_fork_without_ci_link_uses_unique_branch_history_and_preserves_modes(client, remote):
    remote.runs[101]["pull_requests"] = []
    plan = preview(client)
    assert [(run["run_id"], run["mode"]) for run in plan["runs"]] == [(101, "failed"), (202, "all")]
    assert remote.head_queries == 1
    assert not remote.writes
    assert confirm(client, plan).json()["state"] == "accepted"
    assert remote.head_queries == 3  # Preview, confirmation lookup, immediately before POST.
    assert remote.writes == [
        f"/repos/{REPO}/actions/runs/101/rerun-failed-jobs",
        f"/repos/{REPO}/actions/runs/202/rerun",
    ]


@pytest.mark.parametrize(
    "change",
    ["source", "branch", "sha", "missing_source", "null_links", "event", "explicit_other_pr"],
)
def test_fork_fallback_never_overrides_other_run_identity(client, remote, change):
    run = remote.runs[101]
    run["pull_requests"] = []
    if change == "source":
        run["head_repository"]["id"] = 99
    elif change == "branch":
        run["head_branch"] = "someone-else"
    elif change == "sha":
        run["head_sha"] = "b" * 40
    elif change == "missing_source":
        run.pop("head_repository")
    elif change == "null_links":
        run["pull_requests"] = None
    elif change == "event":
        run["event"] = "push"
    else:
        run["pull_requests"] = [
            {
                "number": 999,
                "head": {"sha": HEAD, "repo": {"id": 88}},
                "base": {"ref": "main", "repo": {"id": 44}},
            }
        ]
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [202]
    assert remote.head_queries == 0
    assert not remote.writes


@pytest.mark.parametrize("state", ["OPEN", "CLOSED", "MERGED"])
def test_reused_branch_or_multiple_base_branches_remain_ambiguous(client, remote, state):
    remote.runs[101]["pull_requests"] = []
    other = dict(
        remote.head_pull(), number=124, state=state, headRefOid="b" * 40, baseRefName="release"
    )
    remote.head_pulls = [remote.head_pull(), other]
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [202]
    assert not remote.writes


def test_same_ref_in_another_fork_does_not_change_source_identity(client, remote):
    remote.runs[101]["pull_requests"] = []
    other = dict(remote.head_pull(), number=124, headRepository={"databaseId": 99})
    remote.head_pulls = [other, remote.head_pull()]
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [101, 202]


def test_fork_without_matching_branch_history_is_excluded(client, remote):
    remote.runs[101]["pull_requests"] = []
    remote.head_pulls = []
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [202]
    assert not remote.writes


@pytest.mark.parametrize("timestamp", ["invalid", "2020-01-01T00:00:00"])
def test_fork_identity_requires_valid_timezone_aware_timestamps(client, remote, timestamp):
    remote.runs[101]["pull_requests"] = []
    remote.head_pulls = [dict(remote.head_pull(), createdAt=timestamp)]
    assert client.post("/api/reruns/preview", json={"pr": 123}).status_code == 502
    assert not remote.writes


@pytest.mark.parametrize("change", ["number", "state", "head", "base_ref", "base_repo", "newer_pr"])
def test_fork_branch_history_must_match_current_pr(client, remote, change):
    remote.runs[101]["pull_requests"] = []
    candidate = remote.head_pull()
    if change == "number":
        candidate["number"] = 999
    elif change == "state":
        candidate["state"] = "CLOSED"
    elif change == "head":
        candidate["headRefOid"] = "b" * 40
    elif change == "base_ref":
        candidate["baseRefName"] = "release"
    elif change == "base_repo":
        candidate["baseRepository"]["databaseId"] = 55
    else:
        candidate["createdAt"] = "2099-01-01T00:00:00Z"
    remote.head_pulls = [candidate]
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [202]
    assert not remote.writes


def test_branch_history_is_rechecked_immediately_before_rerun(client, remote):
    remote.runs[101]["pull_requests"] = []
    plan = preview(client)

    def add_another_pr_after_confirmation_lookup(request):
        if (
            request.url.path == "/graphql"
            and json.loads(request.content)["query"] == HEAD_PULLS_QUERY
        ):
            if remote.head_queries == 2:
                remote.head_pulls = [remote.head_pull(), dict(remote.head_pull(), number=124)]

    remote.on_call = add_another_pr_after_confirmation_lookup
    assert confirm(client, plan).json()["state"] == "rejected"
    assert remote.head_queries == 3
    assert not remote.writes


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "success")])
def test_fork_fallback_does_not_rerun_active_or_successful_attempts(
    client, remote, status, conclusion
):
    remote.runs[101].update(pull_requests=[], status=status, conclusion=conclusion, run_attempt=2)
    plan = preview(client)
    assert [item["run_id"] for item in plan["runs"]] == [202]
    assert any("101" in reason and "verified" not in reason for reason in plan["skipped"])
    assert not remote.writes


def test_unrelated_repository_run_never_reaches_rerun(client, remote):
    remote.runs[101]["repository"]["id"] = 123456
    plan = preview(client)
    confirm(client, plan)
    assert remote.writes == [f"/repos/{REPO}/actions/runs/202/rerun"]


def test_graphql_head_drift_and_missing_fields_fail_closed(client, remote):
    remote.graph_head = "b" * 40
    assert client.post("/api/reruns/preview", json={"pr": 123}).status_code == 409
    assert not remote.writes


def test_expired_run_is_excluded(client, remote):
    remote.runs[101]["created_at"] = "2020-01-01T00:00:00Z"
    plan = preview(client)
    assert [run["run_id"] for run in plan["runs"]] == [202]
