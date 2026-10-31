import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_registry import change, preview
from test_registry import multi as registry_fixture

from pr_rerun.aliases import main as alias_main
from pr_rerun.cli import main
from pr_rerun.client_config import SavedLogin, save_login
from pr_rerun.models import ServiceError
from pr_rerun.policy import PolicyUpdate

multi = registry_fixture
REPO = "hw-native-sys/pypto"


def test_official_aliases_are_filtered_and_never_contact_github(multi):
    client, registry, auth, manager, outsider = multi
    change(registry, manager, aliases=["pto"])
    actor = registry.store.authenticate("Bearer " + manager)
    other = registry.policies.get("other/repo")
    registry.policies.update(
        "other/repo",
        PolicyUpdate(
            expected_revision=1, policy=other["policy"].model_copy(update={"aliases": ["private"]})
        ),
        actor,
    )
    assert client.get("/api/repository-aliases").json() == {"aliases": {"pto": REPO}}
    assert client.get("/api/repository-aliases/pto").json() == {"repository": REPO}
    assert client.get("/api/repository-aliases/private").status_code == 404
    assert client.get("/api/repository-aliases/missing").status_code == 404
    change(registry, manager, enabled=False)
    assert client.get("/api/repository-aliases").json() == {"aliases": {}}
    assert client.get("/api/repository-aliases/pto").status_code == 404
    client.headers["Authorization"] = "Bearer invalid"
    assert client.get("/api/repository-aliases").status_code == 401
    assert not auth.lookups and not auth.remote.writes


def test_alias_changes_require_repository_admin_and_invalidate_previews(multi):
    client, registry, auth, manager, outsider = multi
    plan = preview(client)
    record = registry.policies.get(REPO)
    data = {"expected_revision": record["revision"], "policy": record["policy"].model_dump()}
    data["policy"]["aliases"] = ["pto"]
    url = f"/api/repositories/{REPO}/policy"
    assert client.post(url, json=data).status_code == 403
    assert (
        client.post(url, json=data, headers={"Authorization": "Bearer " + outsider}).status_code
        == 403
    )
    result = client.post(url, json=data, headers={"Authorization": "Bearer " + manager})
    assert result.status_code == 200
    assert result.json()["revision"] == 2
    assert (
        client.post(url, json=data, headers={"Authorization": "Bearer " + manager}).status_code
        == 409
    )
    from conftest import confirm

    assert confirm(client, plan).json()["state"] == "rejected"
    assert registry.policies.history(REPO, registry.store.authenticate("Bearer " + manager))[0][
        "policy"
    ]["aliases"] == ["pto"]
    assert not auth.remote.writes


def test_conflicting_alias_updates_are_atomic_even_between_repositories(multi):
    _, registry, _, manager, _ = multi
    actor = registry.store.authenticate("Bearer " + manager)

    def update(name):
        record = registry.policies.get(name)
        try:
            registry.policies.update(
                name,
                PolicyUpdate(
                    expected_revision=1,
                    policy=record["policy"].model_copy(update={"aliases": ["shared"]}),
                ),
                actor,
            )
            return 200
        except ServiceError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [REPO, "other/repo"])) == [200, 409]
    records = [registry.policies.get(name) for name in (REPO, "other/repo")]
    assert sorted(r["revision"] for r in records) == [1, 2]
    assert sum(r["policy"].aliases == ["shared"] for r in records) == 1
    assert len(registry.policies.aliases(actor)) == 1


def test_remove_then_reassign_official_alias(multi):
    _, registry, _, manager, _ = multi
    change(registry, manager, aliases=["pto"])
    change(registry, manager, aliases=[])
    actor = registry.store.authenticate("Bearer " + manager)
    record = registry.policies.get("other/repo")
    registry.policies.update(
        "other/repo",
        PolicyUpdate(
            expected_revision=1, policy=record["policy"].model_copy(update={"aliases": ["pto"]})
        ),
        actor,
    )
    assert registry.policies.resolve_alias("pto", actor) == "other/repo"


@pytest.mark.parametrize("names", [["x", "x"], ["../bad"], ["org/repo"], ["1bad"]])
def test_invalid_official_alias_rejected(multi, names):
    client, registry, _, manager, _ = multi
    policy = registry.policies.get(REPO)["policy"].model_dump()
    policy["aliases"] = names
    assert (
        client.post(
            f"/api/repositories/{REPO}/policy",
            json={"expected_revision": 1, "policy": policy},
            headers={"Authorization": "Bearer " + manager},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("local", [False, True])
def test_cli_alias_precedence_and_full_repository_request(monkeypatch, tmp_path, local):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        "PR_RERUN_URL",
        "PR_RERUN_NETBIRD_URL",
        "PR_RERUN_ACCESS_TOKEN",
        "PR_RERUN_REPOSITORY",
    ):
        monkeypatch.delenv(key, raising=False)
    save_login(SavedLogin("http://127.0.0.1:8080", False, "prr_" + "a" * 43))
    if local:
        alias_main(["set", "pto", "local/project"])
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/repository-aliases/pto":
            assert not local
            return httpx.Response(200, json={"repository": REPO})
        assert request.url.path == "/api/reruns/preview"
        assert json.loads(request.content)["repository"] == ("local/project" if local else REPO)
        return httpx.Response(403, json={"error": "No matching PR"})

    client_class = httpx.Client
    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr("sys.argv", ["pr-rerun", "123", "--repo", "pto"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert len(calls) == (1 if local else 2)


def test_official_alias_cli_updates_policy_with_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("PR_RERUN_URL", "PR_RERUN_NETBIRD_URL", "PR_RERUN_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    save_login(SavedLogin("http://127.0.0.1:8080", False, "prr_" + "a" * 43))
    calls = []

    def respond(request):
        calls.append(request.method)
        assert request.url.path == f"/api/repositories/{REPO}/policy"
        if request.method == "GET":
            return httpx.Response(
                200, json={"revision": 4, "policy": {"aliases": ["existing"], "enabled": True}}
            )
        assert json.loads(request.content) == {
            "expected_revision": 4,
            "policy": {"aliases": ["existing", "pto"], "enabled": True},
        }
        return httpx.Response(200, json={"revision": 5})

    client_class = httpx.Client
    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr("sys.argv", ["pr-rerun", "alias", "set", "pto", REPO, "--official"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0 and calls == ["GET", "POST"]
