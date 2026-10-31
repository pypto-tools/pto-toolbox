"""Repository boundaries, live policy changes and quota regression tests."""

from pathlib import Path

import httpx
import pytest
from conftest import REPO, Remote, confirm
from fastapi.testclient import TestClient

from pr_rerun.api import create_app
from pr_rerun.app_auth import AppAuth
from pr_rerun.github import GitHub
from pr_rerun.policy import PolicyUpdate, RepositoryPolicy


class FakeApp(AppAuth):
    def __init__(self):
        self.remote = Remote()
        self.allowed = True
        self.lookups = []

    def close(self):
        pass

    def credentials(self, name, repository_id=None, installation_id=None):
        repo_id = repository_id
        self.lookups.append((name, repo_id, installation_id))
        if not self.allowed:
            from pr_rerun.models import ServiceError

            raise ServiceError(403, "Installation revoked")
        assert (name, repo_id, installation_id) == (REPO, 44, 55)
        return "github-test-token", 44, 55

    def validate_workflows(self, name, repository_id, installation_id, workflows):
        self.credentials(name, repository_id, installation_id)

    def github(self, name, repository_id, installation_id):
        repo_id = repository_id
        parent = self

        class Client(GitHub):
            def _request(self, method, path, *, payload=None, write=False):
                parent.credentials(name, repo_id, installation_id)
                return super()._request(method, path, payload=payload, write=write)

        return Client(name, "github-test-token", httpx.MockTransport(self.remote))


@pytest.fixture
def multi(settings):
    auth = FakeApp()
    settings = settings.model_copy(update={"app_id": 1, "app_key_file": Path("/unused")})
    app = create_app(settings, app_auth=auth)
    registry = app.state.service
    user = registry.store.issue_token(7, "alice")
    manager = registry.store.issue_token(8, "manager")
    outsider = registry.store.issue_token(9, "outsider")
    policy = RepositoryPolicy(
        maintainers=[8], users=[7], workflows=list(settings.workflows), cooldown_seconds=0
    )
    registry.policies.enroll(REPO, 44, 55, policy)
    registry.policies.enroll("other/repo", 66, 77, policy.model_copy(update={"users": [9]}))
    with TestClient(app, headers={"Authorization": f"Bearer {user}"}) as client:
        yield client, registry, auth, manager, outsider


def preview(client):
    response = client.post("/api/reruns/preview", json={"repository": REPO, "pr": 123})
    assert response.status_code == 200, response.text
    return response.json()


def change(registry, manager_token, **fields):
    record = registry.policies.get(REPO)
    policy = record["policy"].model_copy(update=fields)
    manager = registry.store.authenticate(f"Bearer {manager_token}")
    return registry.policies.update(
        REPO, PolicyUpdate(expected_revision=record["revision"], policy=policy), manager
    )


def test_repository_selection_and_user_grants(multi):
    client, registry, auth, manager, outsider = multi
    assert client.post("/api/reruns/preview", json={"pr": 123}).status_code == 400
    assert (
        client.post("/api/reruns/preview", json={"repository": "other/repo", "pr": 123}).status_code
        == 403
    )
    assert not auth.lookups
    plan = preview(client)
    assert plan["policy_revision"] == 1
    assert confirm(client, plan).json()["state"] == "accepted"
    assert len(auth.remote.writes) == 2


@pytest.mark.parametrize(
    "fields", [dict(enabled=False), dict(users=[]), dict(base_branches=["release/*"])]
)
def test_policy_changes_invalidate_old_confirmations(multi, fields):
    client, registry, auth, manager, _ = multi
    plan = preview(client)
    change(registry, manager, **fields)
    response = confirm(client, plan)
    assert response.status_code == 403 or response.json()["state"] == "rejected"
    assert not auth.remote.writes


def test_policy_version_changes_even_when_rules_restored(multi):
    client, registry, auth, manager, _ = multi
    plan = preview(client)
    change(registry, manager, cooldown_seconds=1)
    change(registry, manager, cooldown_seconds=0)
    assert confirm(client, plan).json()["state"] == "rejected"
    assert not auth.remote.writes


def test_policy_changes_between_workflow_posts_stop_batch(multi):
    client, registry, auth, manager, _ = multi

    def after_first(request):
        change(registry, manager, enabled=False)
        return httpx.Response(201)

    auth.remote.fail_write = after_first
    result = confirm(client, preview(client)).json()
    assert result["state"] == "partial"
    assert len(auth.remote.writes) == 1


def test_revoked_app_installation_stops_confirmation(multi):
    client, _, auth, _, _ = multi
    plan = preview(client)
    auth.allowed = False
    assert confirm(client, plan).json()["state"] == "rejected"
    assert not auth.remote.writes


def test_policy_management_requires_repository_maintainer_and_expected_version(multi):
    client, registry, auth, manager, _ = multi
    endpoint = f"/api/repositories/{REPO}/policy"
    assert client.get(endpoint).status_code == 403
    client.headers["Authorization"] = f"Bearer {manager}"
    current = client.get(endpoint).json()
    update = {"expected_revision": current["revision"], "policy": current["policy"]}
    update["policy"]["users"] = [7, 9]
    assert client.post(endpoint, json=update).json()["revision"] == 2
    assert client.post(endpoint, json=update).status_code == 409
    history = client.get(f"/api/repositories/{REPO}/history").json()["history"]
    assert [entry["revision"] for entry in history] == [2, 1]
    assert history[0]["actor"] == 8
    update["expected_revision"] = 2
    update["policy"]["users"] = [999]
    assert client.post(endpoint, json=update).status_code == 400
    update["policy"]["users"] = [7]
    update["policy"]["github_token"] = "must-not-echo"
    response = client.post(endpoint, json=update)
    assert response.status_code == 422 and "must-not-echo" not in response.text
    assert not auth.remote.writes


def test_foreign_policy_admin_is_rejected(multi):
    client, registry, auth, manager, outsider = multi
    client.headers["Authorization"] = f"Bearer {outsider}"
    assert client.get(f"/api/repositories/{REPO}/policy").status_code == 403
    assert client.get(f"/api/repositories/{REPO}/history").status_code == 403
    plan = {"expected_revision": 1, "policy": registry.policies.get(REPO)["policy"].model_dump()}
    assert client.post(f"/api/repositories/{REPO}/policy", json=plan).status_code == 403
    assert not auth.lookups


def test_authenticated_users_policy_still_requires_own_pr_and_explicit_manager(multi):
    client, registry, auth, manager, outsider = multi
    change(registry, manager, users=[], access="authenticated_users")
    # The existing caller has no explicit repository grant, but owns the PR.
    assert preview(client)["eligible"] is True
    assert client.get(f"/api/repositories/{REPO}/policy").status_code == 403
    client.headers["Authorization"] = f"Bearer {outsider}"
    assert (
        client.post("/api/reruns/preview", json={"repository": REPO, "pr": 123}).status_code == 403
    )
    assert not auth.remote.writes


@pytest.mark.parametrize("draft,base", [(True, "main"), (False, "release/next")])
def test_repository_pr_restrictions(multi, draft, base):
    client, _, auth, _, _ = multi
    auth.remote.pr.update(draft=draft)
    auth.remote.pr["base"]["ref"] = base
    assert (
        client.post("/api/reruns/preview", json={"repository": REPO, "pr": 123}).status_code == 403
    )
    assert not auth.remote.writes


def test_daily_quota_and_uncertain_request_limit(multi):
    client, registry, auth, manager, _ = multi
    change(registry, manager, max_requests_per_user_per_day=1)
    assert confirm(client, preview(client)).json()["state"] == "accepted"
    for run in auth.remote.runs.values():
        run["run_attempt"] += 1
    result = confirm(client, preview(client)).json()
    assert result["state"] == "rejected"
    assert "daily" in result["error"]
    assert len(auth.remote.writes) == 2


def test_unlimited_policy_keeps_repository_grants_and_duplicate_protection(multi):
    client, registry, auth, manager, outsider = multi
    change(
        registry,
        manager,
        workflows=[registry.policies.get(REPO)["policy"].workflows[0]],
        allow_drafts=True,
        base_branches=["*"],
        cooldown_seconds=0,
        max_requests_per_user_per_day=None,
        max_active_requests_per_pr=None,
    )
    auth.remote.pr["draft"] = True
    auth.remote.pr["base"]["ref"] = "feature/test"
    auth.remote.manifest["base_ref"] = "feature/test"
    for run in auth.remote.runs.values():
        for linked_pr in run["pull_requests"]:
            linked_pr["base"]["ref"] = "feature/test"
    for _ in range(3):
        preview(client)
        assert confirm(client, preview(client)).json()["state"] == "accepted"
        for run in auth.remote.runs.values():
            run["run_attempt"] += 1
    assert len(auth.remote.writes) == 3
    for run in auth.remote.runs.values():
        run["run_attempt"] -= 1
    assert confirm(client, preview(client)).json()["state"] == "rejected"
    assert len(auth.remote.writes) == 3
    client.headers["Authorization"] = f"Bearer {outsider}"
    assert (
        client.post("/api/reruns/preview", json={"repository": REPO, "pr": 123}).status_code == 403
    )
    assert len(auth.remote.writes) == 3


def test_an_uncertain_submission_blocks_another_attempt(multi):
    client, registry, auth, _, _ = multi

    def timeout(request):
        raise httpx.ReadTimeout("timeout")

    auth.remote.fail_write = timeout
    assert confirm(client, preview(client)).json()["state"] == "unknown"
    for run in auth.remote.runs.values():
        run["run_attempt"] += 1
    result = confirm(client, preview(client)).json()
    assert result["state"] == "rejected"
    assert "uncertain" in result["error"]
    assert len(auth.remote.writes) == 1


def test_ticket_routing_cannot_be_changed_and_revoked_token_cannot_read(multi):
    client, registry, auth, _, outsider = multi
    plan = preview(client)
    assert (
        client.post(
            "/api/reruns/confirm",
            json={
                "ticket": plan["ticket"],
                "confirmation": plan["confirmation"],
                "repository": "other/repo",
            },
        ).status_code
        == 422
    )
    client.headers["Authorization"] = f"Bearer {outsider}"
    assert confirm(client, plan).status_code == 404
    assert client.get("/api/reruns/" + plan["request_id"]).status_code == 404
    assert not auth.remote.writes


def test_preview_on_another_repository_does_not_supersede_existing_ticket(multi):
    client, registry, _, _, _ = multi
    plan = preview(client)
    actor = registry.store.authenticate(client.headers["Authorization"])
    stored = registry.store.request_plan(actor, ticket=plan["ticket"])
    other_pr = stored.pr.model_copy(update={"repository_id": 66})
    other = stored.model_copy(update={"repository": "other/repo", "pr": other_pr})
    registry.store.preview(actor, other, 300)
    assert confirm(client, plan).json()["state"] == "accepted"


def test_second_repository_uses_its_own_client_and_independent_claims(multi, monkeypatch):
    import json

    client, registry, auth, manager, _ = multi
    actor = registry.store.authenticate(f"Bearer {manager}")
    record = registry.policies.get("other/repo")
    registry.policies.update(
        "other/repo",
        PolicyUpdate(
            expected_revision=1, policy=record["policy"].model_copy(update={"users": [7]})
        ),
        actor,
    )
    second = Remote()
    second.pr["base"]["repo"]["id"] = 66
    for run in second.runs.values():
        run["repository"]["id"] = 66
        for link in run["pull_requests"]:
            link["base"]["repo"]["id"] = 66

    def transport(request):
        mapped = httpx.Request(
            request.method,
            str(request.url).replace("/repos/other/repo/", f"/repos/{REPO}/"),
            headers=request.headers,
            content=request.content,
        )
        response = second(mapped)
        if response.headers.get("content-type", "").startswith("application/json"):
            return httpx.Response(
                response.status_code,
                json=json.loads(response.content.decode().replace(REPO, "other/repo")),
            )
        # Codex archive remains fixture-specific; select CI only below.
        return response

    original = auth.github

    def github(name, repository_id, installation_id):
        if name == "other/repo":
            assert (repository_id, installation_id) == (66, 77)
            return GitHub(name, "github-test-token", httpx.MockTransport(transport))
        return original(name, repository_id, installation_id)

    monkeypatch.setattr(auth, "github", github)
    second.linked = [101]
    first_plan = preview(client)
    response = client.post("/api/reruns/preview", json={"repository": "other/repo", "pr": 123})
    assert response.status_code == 200, response.text
    second_plan = response.json()
    assert second_plan["repository"] == "other/repo"
    assert confirm(client, first_plan).json()["state"] == "accepted"
    assert confirm(client, second_plan).json()["state"] == "accepted"
    assert len(auth.remote.writes) == 2 and len(second.writes) == 1
    with registry.store.connection() as db:
        assert {row["repository_id"] for row in db.execute("SELECT repository_id FROM claims")} == {
            44,
            66,
        }


def test_local_bootstrap_is_required_before_remote_management(
    settings, monkeypatch, tmp_path, capsys
):
    from pr_rerun import admin
    from pr_rerun.store import Store

    auth = FakeApp()
    store = Store(settings.database)
    store.issue_token(8, "manager")
    monkeypatch.delenv("PR_RERUN_CONFIG", raising=False)
    monkeypatch.setenv("PR_RERUN_DATABASE", str(settings.database))
    monkeypatch.setenv("PR_RERUN_APP_ID", "1")
    monkeypatch.setenv("PR_RERUN_APP_PRIVATE_KEY_FILE", "/unused")

    def credentials(name, repository_id=None, installation_id=None):
        assert name == REPO
        return "github-test-token", 44, 55

    monkeypatch.setattr(auth, "credentials", credentials)
    monkeypatch.setattr(admin, "AppAuth", lambda *args: auth)
    policy = RepositoryPolicy(maintainers=[8], workflows=list(settings.workflows))
    path = tmp_path / "policy.json"
    path.write_text(policy.model_dump_json())
    monkeypatch.setattr(
        "sys.argv", ["pr-rerun-admin", "repo-add", REPO, "--policy-file", str(path)]
    )
    admin.main()
    assert "revision 1" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        admin.main()
    assert error.value.code == 2
    assert not auth.remote.writes


def test_disabling_repository_does_not_depend_on_github_availability(multi):
    client, registry, auth, manager, _ = multi
    client.headers["Authorization"] = f"Bearer {manager}"
    auth.allowed = False
    policy = registry.policies.get(REPO)["policy"].model_dump()
    policy["enabled"] = False
    response = client.post(
        f"/api/repositories/{REPO}/policy", json={"expected_revision": 1, "policy": policy}
    )
    assert response.status_code == 200
    assert not response.json()["policy"]["enabled"]
    assert not auth.remote.writes


def test_revoked_maintainer_token_cannot_commit_policy_after_remote_checks(multi, monkeypatch):
    client, registry, auth, manager, _ = multi
    client.headers["Authorization"] = f"Bearer {manager}"
    monkeypatch.setattr(auth, "validate_workflows", lambda *args: registry.store.revoke(8))
    policy = registry.policies.get(REPO)["policy"].model_dump()
    response = client.post(
        f"/api/repositories/{REPO}/policy", json={"expected_revision": 1, "policy": policy}
    )
    assert response.status_code == 401
    assert registry.policies.get(REPO)["revision"] == 1


def test_policy_change_during_final_app_authorization_stops_post(multi, monkeypatch):
    client, registry, auth, manager, _ = multi
    plan = preview(client)
    original = auth.github

    def github(*args):
        instance = original(*args)
        request = instance._request

        def change_before_post(method, path, *, payload=None, write=False):
            if write:
                change(registry, manager, enabled=False)
            return request(method, path, payload=payload, write=write)

        instance._request = change_before_post
        return instance

    monkeypatch.setattr(auth, "github", github)
    assert confirm(client, plan).json()["state"] == "rejected"
    assert not auth.remote.writes
