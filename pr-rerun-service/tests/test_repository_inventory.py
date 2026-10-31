import json
from pathlib import Path

import httpx
import pytest
from test_cli_login import invoke, mock_server
from test_registry import change
from test_registry import multi as registry_fixture

from pr_rerun import admin
from pr_rerun.app_auth import PERMISSIONS, AppAuth
from pr_rerun.client_config import SavedLogin, save_login
from pr_rerun.inventory import inventory, read_enrollments
from pr_rerun.models import UpstreamError

multi = registry_fixture
REPO = "hw-native-sys/pypto"
TOKEN = "prr_" + "a" * 43


def test_repos_visible_access_and_no_github_requests(multi):
    client, registry, auth, manager, outsider = multi
    result = client.get("/api/repositories").json()
    assert result["scope"] == "visible" and result["github_checked"] is False
    assert [row["repository"] for row in result["repositories"]] == [REPO]
    assert result["repositories"][0]["aliases"] == []
    change(registry, manager, enabled=False)
    assert client.get("/api/repositories").json()["repositories"] == []
    client.headers["Authorization"] = "Bearer " + manager
    rows = client.get("/api/repositories").json()["repositories"]
    assert len(rows) == 2 and rows[0]["enabled"] is False
    assert all(row["role"] == "maintainer" for row in rows)
    client.headers["Authorization"] = "Bearer " + outsider
    assert [
        row["repository"] for row in client.get("/api/repositories").json()["repositories"]
    ] == ["other/repo"]
    registry.store.set_service_admin(9, enabled=True)
    result = client.get("/api/repositories").json()
    assert result["scope"] == "all" and len(result["repositories"]) == 2
    assert all(row["role"] == "service_admin" for row in result["repositories"])
    registry.store.set_service_admin(9, enabled=False)
    assert len(client.get("/api/repositories").json()["repositories"]) == 1
    assert not auth.lookups and not auth.remote.writes
    client.headers["Authorization"] = "Bearer invalid"
    assert client.get("/api/repositories").status_code == 401


def test_authenticated_users_see_enabled_repository_without_explicit_grant(multi):
    client, registry, _, manager, outsider = multi
    change(registry, manager, access="authenticated_users")
    client.headers["Authorization"] = "Bearer " + outsider
    assert len(client.get("/api/repositories").json()["repositories"]) == 2


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_repos_ignores_saved_or_environment_repository(tmp_path, monkeypatch, capsys, as_json):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PR_RERUN_REPOSITORY", "unknown-alias")
    monkeypatch.delenv("PR_RERUN_ACCESS_TOKEN", raising=False)
    for name in ("PR_RERUN_URL", "PR_RERUN_NETBIRD_URL"):
        monkeypatch.delenv(name, raising=False)
    save_login(SavedLogin("http://127.0.0.1:8080", False, TOKEN, "removed/repo"))
    payload = {
        "scope": "all",
        "github_checked": False,
        "repositories": [
            {
                "repository": "owner/repo",
                "aliases": [],
                "enabled": False,
                "role": "service_admin",
                "revision": 3,
            },
        ],
    }
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return httpx.Response(200, json=payload)

    mock_server(monkeypatch, respond)
    monkeypatch.setattr("sys.argv", ["pr-rerun", "repos", *(["--json"] if as_json else [])])
    admin_cli = __import__("pr_rerun.cli", fromlist=["main"])
    admin_cli.main()
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output) == payload
    else:
        assert "disabled" in output and "owner/repo" in output and "not checked" in output
    assert calls == [("GET", "/api/repositories")]
    assert TOKEN not in output


def test_cli_repos_old_server_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    mock_server(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    assert invoke(monkeypatch, "repos", "--server", "http://127.0.0.1:8080") == 2
    assert "server version 0.4.1" in capsys.readouterr().err


def installation(identity=10, **updates):
    return {
        "id": identity,
        "app_id": 1,
        "account": {"login": "example"},
        "repository_selection": "selected",
        "permissions": PERMISSIONS,
        "suspended_at": None,
        **updates,
    }


def make_auth(monkeypatch, respond):
    auth = AppAuth(1, Path("/unused"), httpx.MockTransport(respond))
    monkeypatch.setattr(auth, "jwt", lambda: "test-jwt")
    return auth


def test_live_inventory_paginates_and_only_mints_metadata_tokens(monkeypatch):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path, request.url.params.get("page")))
        if request.url.path == "/app/installations":
            assert request.headers["Authorization"] == "Bearer test-jwt"
            return httpx.Response(200, json=[installation()])
        if request.url.path.endswith("/access_tokens"):
            assert json.loads(request.content) == {"permissions": {"metadata": "read"}}
            return httpx.Response(
                201, json={"token": "secret-inventory-token", "permissions": {"metadata": "read"}}
            )
        assert request.method == "GET" and request.url.path == "/installation/repositories"
        assert request.headers["Authorization"] == "Bearer secret-inventory-token"
        page = int(request.url.params["page"])
        rows = (
            [{"id": i, "full_name": f"example/repo{i}"} for i in range(1, 101)]
            if page == 1
            else [{"id": 101, "full_name": "example/last"}]
        )
        return httpx.Response(200, json={"repositories": rows, "total_count": 101})

    auth = make_auth(monkeypatch, respond)
    try:
        result = auth.installations()
        assert len(result[0]["repositories"]) == 101 and result[0]["repositories_checked"]
        assert "secret-inventory-token" not in json.dumps(result)
        assert not auth.tokens
        assert calls[-1] == ("GET", "/installation/repositories", "2")
    finally:
        auth.close()


def test_installation_pagination_includes_second_page(monkeypatch):
    seen = []

    def respond(request):
        assert request.method == "GET" and request.url.path == "/app/installations"
        page = int(request.url.params["page"])
        seen.append(page)
        ids = range(1, 101) if page == 1 else [101]
        return httpx.Response(200, json=[installation(i, suspended_at="yesterday") for i in ids])

    auth = make_auth(monkeypatch, respond)
    try:
        result = auth.installations()
        assert len(result) == 101 and seen == [1, 2]
        assert all(not row["repositories_checked"] for row in result)
    finally:
        auth.close()


@pytest.mark.parametrize(
    "failure", ["suspended", "token", "repository", "write_token", "malformed"]
)
def test_partial_failure_is_unknown_not_missing_authorization(monkeypatch, failure):
    def respond(request):
        if request.url.path == "/app/installations":
            return httpx.Response(
                200, json=[installation(suspended_at="now" if failure == "suspended" else None)]
            )
        if request.url.path.endswith("/access_tokens"):
            if failure == "token":
                return httpx.Response(403, json={"error": "secret body"})
            return httpx.Response(
                201,
                json={
                    "token": "secret",
                    "permissions": {"actions": "write"}
                    if failure == "write_token"
                    else {"metadata": "read"},
                },
            )
        return httpx.Response(
            403 if failure == "repository" else 200,
            json={"repositories": "bad", "secret": "secret body"},
        )

    auth = make_auth(monkeypatch, respond)
    enrolled = [{"repository": "example/old", "repository_id": 42, "installation_id": 10}]
    try:
        result = inventory(auth, enrolled)
        assert result["complete"] is False
        assert result["service_only"][0]["github_status"] == "unknown"
        assert "secret" not in json.dumps(result)
    finally:
        auth.close()


def test_reconciliation_distinguishes_registration_identity_and_revocation(monkeypatch):
    auth = make_auth(monkeypatch, lambda r: pytest.fail("No network expected"))
    monkeypatch.setattr(
        auth,
        "installations",
        lambda: [
            {
                "installation_id": 10,
                "repositories_checked": True,
                "repositories": [
                    {"repository": "example/enrolled", "repository_id": 1},
                    {"repository": "example/new", "repository_id": 2},
                    {"repository": "example/reinstalled", "repository_id": 3},
                    {"repository": "example/renamed", "repository_id": 4},
                ],
            }
        ],
    )
    enrolled = [
        {"repository": "example/" + name, "repository_id": i, "installation_id": install}
        for name, i, install in [
            ("enrolled", 1, 10),
            ("reinstalled", 3, 9),
            ("before-rename", 4, 10),
            ("removed", 5, 10),
            ("uninstalled", 6, 20),
        ]
    ]
    try:
        result = inventory(auth, enrolled)
        assert result["complete"]
        assert [r["enrollment"] for r in result["installations"][0]["repositories"]] == [
            "enrolled",
            "not_enrolled",
            "identity_changed",
            "identity_changed",
        ]
        assert [r["github_status"] for r in result["service_only"]] == [
            "not_authorized",
            "installation_missing",
        ]
    finally:
        auth.close()


def test_database_query_never_creates_or_writes_database(multi, tmp_path):
    _, registry, _, _, _ = multi
    database = registry.store.path
    before = database.read_bytes()
    result = read_enrollments(database)
    assert len(result) == 2 and database.read_bytes() == before
    assert all("maintainers" not in row and "users" not in row for row in result)
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="existing App-mode"):
        read_enrollments(missing)
    assert not missing.exists()


def test_admin_partial_inventory_json_exit_status(multi, monkeypatch, capsys):
    _, registry, _, _, _ = multi
    monkeypatch.setenv("PR_RERUN_DATABASE", str(registry.store.path))
    monkeypatch.setenv("PR_RERUN_APP_ID", "1")
    monkeypatch.setenv("PR_RERUN_APP_PRIVATE_KEY_FILE", "/unused")
    monkeypatch.delenv("PR_RERUN_CONFIG", raising=False)
    monkeypatch.setattr(
        AppAuth,
        "installations",
        lambda self: [
            {
                "installation_id": 55,
                "repositories": [],
                "repositories_checked": False,
                "error": "unavailable",
            }
        ],
    )
    monkeypatch.setattr("sys.argv", ["pr-rerun-admin", "installations", "--json"])
    with pytest.raises(SystemExit) as result:
        admin.main()
    assert result.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["complete"] is False
    assert "incomplete" in captured.err


def test_invalid_installation_listing_fails_without_guessing(monkeypatch):
    auth = make_auth(monkeypatch, lambda r: httpx.Response(200, json=[installation(app_id=99)]))
    try:
        with pytest.raises(UpstreamError, match="Invalid"):
            auth.installations()
    finally:
        auth.close()
