import json

import pytest
from test_registry import change
from test_registry import multi as registry_fixture

from pr_rerun import admin
from pr_rerun.models import ServiceError
from pr_rerun.policy import PolicyUpdate

multi = registry_fixture
REPO = "hw-native-sys/pypto"


def test_service_admin_manages_policies_with_audit_and_revision_checks(multi):
    client, registry, auth, _, outsider = multi
    client.headers["Authorization"] = "Bearer " + outsider
    endpoint = f"/api/repositories/{REPO}/policy"
    assert client.get(endpoint).status_code == 403
    registry.store.set_service_admin(9, enabled=True)
    for repository in (REPO, "other/repo"):
        assert client.get(f"/api/repositories/{repository}/policy").status_code == 200
    record = client.get(endpoint).json()
    record["policy"]["aliases"] = ["pypto"]
    update = {"expected_revision": record["revision"], "policy": record["policy"]}
    response = client.post(endpoint, json=update)
    assert response.status_code == 200
    assert response.json()["policy"]["maintainers"] == [8]
    assert client.post(endpoint, json=update).status_code == 409
    assert client.get("/api/repository-aliases/pypto").json() == {"repository": REPO}
    history = client.get(f"/api/repositories/{REPO}/history").json()["history"]
    assert history[0]["actor"] == 9 and history[0]["revision"] == 2
    registry.store.set_service_admin(9, enabled=False)
    assert client.get(endpoint).status_code == 403
    assert client.get("/api/repository-aliases/pypto").status_code == 404
    assert not auth.remote.writes


def test_service_admin_cannot_bypass_rerun_access_or_pr_author(multi):
    client, registry, auth, manager, outsider = multi
    registry.store.set_service_admin(9, enabled=True)
    client.headers["Authorization"] = "Bearer " + outsider
    payload = {"repository": REPO, "pr": 123}
    assert client.post("/api/reruns/preview", json=payload).status_code == 403
    assert not auth.lookups
    change(registry, manager, access="authenticated_users")
    response = client.post("/api/reruns/preview", json=payload)
    assert response.status_code == 403
    assert not auth.remote.writes


def test_role_revoked_during_validation_is_rechecked_in_update_transaction(multi):
    client, registry, auth, _, outsider = multi
    registry.store.set_service_admin(9, enabled=True)
    client.headers["Authorization"] = "Bearer " + outsider
    endpoint = f"/api/repositories/{REPO}/policy"
    record = client.get(endpoint).json()
    record["policy"]["aliases"] = ["pypto"]
    auth.validate_workflows = lambda *args: registry.store.set_service_admin(9, enabled=False)
    result = client.post(
        endpoint, json={"expected_revision": record["revision"], "policy": record["policy"]}
    )
    assert result.status_code == 403
    assert registry.policies.get(REPO)["revision"] == 1
    assert registry.policies.get(REPO)["policy"].aliases == []


def test_revoked_session_cannot_use_retained_admin_role(multi):
    _, registry, _, _, _ = multi
    token = registry.store.login_session(9, "outsider")
    actor = registry.store.authenticate("Bearer " + token)
    registry.store.set_service_admin(9, enabled=True)
    registry.store.logout_session(actor.token_hash)
    record = registry.policies.get(REPO)
    with pytest.raises(ServiceError) as result:
        registry.policies.update(
            REPO, PolicyUpdate(expected_revision=1, policy=record["policy"]), actor
        )
    assert result.value.status == 401


def test_local_role_commands_validate_identity_and_record_local_audit(multi, monkeypatch, capsys):
    _, registry, _, _, _ = multi
    monkeypatch.setenv("PR_RERUN_DATABASE", str(registry.store.path))

    def run(*arguments):
        monkeypatch.setattr("sys.argv", ["pr-rerun-admin", "service-admin", *arguments])
        admin.main()

    with pytest.raises(SystemExit) as error:
        run("grant", "999")
    assert error.value.code == 2
    run("grant", "9")
    capsys.readouterr()
    run("list")
    assert json.loads(capsys.readouterr().out) == [
        {"github_id": 9, "login": "outsider", "enabled": 1}
    ]
    registry.store.revoke(9)
    assert not registry.store.is_service_admin(9)
    run("revoke", "9")
    assert registry.store.service_admins() == []
    with registry.store.connection() as db:
        rows = db.execute(
            "SELECT github_id, enabled, local_uid FROM service_admin_history"
        ).fetchall()
    assert [(r["github_id"], r["enabled"]) for r in rows] == [(9, 1), (9, 0)]
    assert all(r["local_uid"] >= 0 for r in rows)
