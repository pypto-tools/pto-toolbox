"""Device ownership, identity proof, persistent sessions and authorization boundaries."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from pr_rerun.api import create_app
from pr_rerun.client_config import load_login
from pr_rerun.login import DeviceLogin
from pr_rerun.models import ServiceError
from pr_rerun.policy import PolicyStore, PolicyUpdate, RepositoryPolicy
from pr_rerun.store import Store


@pytest.fixture
def flow(tmp_path):
    now = [100.0]
    state = {
        "error": "authorization_pending",
        "calls": [],
        "user": {"id": 7, "login": "alice", "type": "User"},
    }

    def remote(request):
        state["calls"].append(request.url.path)
        if request.url.path == "/login/device/code":
            assert parse_qs(request.content.decode())["client_id"] == ["test-app"]
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["device_code"] == ["private-device-code"]
            return httpx.Response(
                200,
                json={"error": state["error"]}
                if state["error"]
                else {
                    "access_token": "github-secret",
                    "refresh_token": "unused-refresh",
                    "token_type": "bearer",
                },
            )
        assert request.url.path == "/user"
        assert request.headers["Authorization"] == "Bearer github-secret"
        return httpx.Response(200, json=state["user"])

    store = Store(tmp_path / "login.sqlite", clock=lambda: now[0])
    auth = DeviceLogin("test-app", store, httpx.MockTransport(remote), clock=lambda: now[0])
    yield auth, store, now, state
    auth.close()


def authorize(flow, address="client"):
    auth, store, now, state = flow
    started = auth.start(address)
    assert "private-device-code" not in json.dumps(started)
    now[0] += 5
    state["error"] = None
    return auth.poll(started["ticket"])


def test_device_login_proves_identity_and_creates_independent_sessions(flow):
    auth, store, now, state = flow
    first = authorize(flow)
    second = authorize(flow, "second-device")
    assert first["access_token"] != second["access_token"]
    assert store.authenticate("Bearer " + first["access_token"]).github_id == 7
    assert store.authenticate("Bearer " + second["access_token"]).github_id == 7
    assert b"github-secret" not in store.path.read_bytes()
    assert first["access_token"].encode() not in store.path.read_bytes()
    assert "unused-refresh" not in json.dumps(first)
    reopened = Store(store.path, clock=lambda: now[0])
    actor = reopened.authenticate("Bearer " + first["access_token"])
    reopened.logout_session(actor.token_hash)
    with pytest.raises(ServiceError):
        reopened.authenticate("Bearer " + first["access_token"])
    assert reopened.authenticate("Bearer " + second["access_token"]).github_id == 7
    now[0] += 91 * 86400
    with pytest.raises(ServiceError):
        reopened.authenticate("Bearer " + second["access_token"])


def test_poll_interval_slowdown_expiry_and_replay(flow):
    auth, store, now, state = flow
    started = auth.start("client")
    assert auth.poll(started["ticket"])["state"] == "pending"
    assert state["calls"] == ["/login/device/code"]
    now[0] += 5
    state["error"] = "slow_down"
    assert auth.poll(started["ticket"])["interval"] == 10
    count = len(state["calls"])
    auth.poll(started["ticket"])
    assert len(state["calls"]) == count
    now[0] += 10
    state["error"] = None
    auth.poll(started["ticket"])
    with pytest.raises(ServiceError, match="expired or already completed"):
        auth.poll(started["ticket"])
    now[0] += 5
    started = auth.start("client")
    now[0] += 901
    with pytest.raises(ServiceError):
        auth.poll(started["ticket"])
    with pytest.raises(ServiceError):
        auth.poll("forged-ticket")


@pytest.mark.parametrize("error", ["access_denied", "expired_token"])
def test_failed_login_cannot_provision_user(flow, error):
    auth, store, now, state = flow
    started = auth.start("client")
    now[0] += 5
    state["error"] = error
    with pytest.raises(ServiceError):
        auth.poll(started["ticket"])
    assert store.users() == []


def test_admin_revocation_cannot_be_bypassed_with_github_login(flow):
    auth, store, now, state = flow
    first = authorize(flow)
    store.revoke(7)
    with pytest.raises(ServiceError):
        store.authenticate("Bearer " + first["access_token"])
    with pytest.raises(ServiceError, match="revoked"):
        authorize(flow, "another-device")


def test_session_authenticates_policy_admin_and_legacy_rotation_revokes_it(flow):
    _, store, _, _ = flow
    value = authorize(flow)
    actor = store.authenticate("Bearer " + value["access_token"])
    from pr_rerun.models import WorkflowRule

    policies = PolicyStore(store)
    policy = RepositoryPolicy(
        maintainers=[7], workflows=[WorkflowRule(path=".github/workflows/ci.yml")]
    )
    policies.enroll("team/repo", 44, 55, policy)
    assert (
        policies.update("team/repo", PolicyUpdate(expected_revision=1, policy=policy), actor)[
            "revision"
        ]
        == 2
    )
    store.issue_token(7, "alice")
    with pytest.raises(ServiceError):
        store.authenticate("Bearer " + value["access_token"])


@pytest.mark.parametrize("repository", ["team/repo", "official"])
def test_cli_device_login_saves_only_verified_service_session(
    flow, monkeypatch, tmp_path, capsys, repository
):
    auth, store, now, state = flow
    from pr_rerun.cli import LOGIN_PROBE, github_login

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        "pr_rerun.cli.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    monkeypatch.setattr("pr_rerun.cli.time.monotonic", lambda: now[0])
    state["error"] = None
    client_class = httpx.Client

    def remote(request):
        path = request.url.path
        if path == "/api/repository-aliases/official":
            store.authenticate(request.headers["Authorization"])
            return httpx.Response(200, json={"repository": "team/repo"})
        if path == "/api/auth/config":
            return httpx.Response(200, json={"github_login": True})
        if path == "/api/auth/device/start":
            return httpx.Response(200, json=auth.start("cli"))
        if path == "/api/auth/device/poll":
            return httpx.Response(200, json=auth.poll(json.loads(request.content)["ticket"]))
        assert path == LOGIN_PROBE
        store.authenticate(request.headers["Authorization"])
        return httpx.Response(404, json={"error": "Request not found."})

    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(remote), **kwargs),
    )
    github_login("http://127.0.0.1:8080", False, repository)
    saved = load_login()
    assert saved is not None and saved.repository == "team/repo"
    assert store.authenticate("Bearer " + saved.access_token).github_id == 7
    output = capsys.readouterr().out
    assert "ABCD-EFGH" in output and "@alice" in output
    assert saved.access_token not in output and "github-secret" not in output


def test_api_rejects_client_asserted_identity(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/auth/config").json() == {"github_login": False}
        assert client.post("/api/auth/device/start", json={}).status_code == 503
        assert (
            client.post(
                "/api/auth/device/poll", json={"ticket": "a" * 43, "login": "admin"}
            ).status_code
            == 422
        )


def test_explicit_login_proxy_is_used_without_ambient_proxy(flow, monkeypatch):
    _, store, _, _ = flow
    original_client = httpx.Client
    seen = []
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted.invalid:9999")

    def client(**kwargs):
        seen.append(kwargs.pop("proxy"))
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        return original_client(**kwargs)

    monkeypatch.setattr("pr_rerun.login.httpx.Client", client)
    auth = DeviceLogin("test-app", store, proxy="http://127.0.0.1:7890")
    try:
        assert auth.call("/login/device/code", {}) == {}
        assert auth.call("/user", token="private-test-token") == {}
        assert seen == ["http://127.0.0.1:7890"]
    finally:
        auth.close()


@pytest.mark.parametrize(
    "proxy",
    [
        "socks5://127.0.0.1:1080",
        "http://proxy.invalid/path",
        "http://proxy.invalid/?password=private-test-secret",
        "http://user:private-test-secret@proxy.invalid:bad",
    ],
)
def test_invalid_login_proxy_is_rejected_without_echoing_credentials(flow, proxy):
    _, store, _, _ = flow
    with pytest.raises(ValueError) as error:
        DeviceLogin("test-app", store, proxy=proxy)
    assert "PR_RERUN_LOGIN_PROXY" in str(error.value)
    assert "private-test-secret" not in str(error.value)


@pytest.mark.parametrize("failure", ["timeout", "http_error", "invalid_json"])
def test_login_diagnostics_record_stage_without_sensitive_data(flow, caplog, failure):
    auth, store, _, _ = flow

    def remote(request):
        if failure == "timeout":
            raise httpx.ConnectTimeout("private-test-secret", request=request)
        if failure == "http_error":
            return httpx.Response(503, text="private-test-secret")
        return httpx.Response(200, text="private-test-secret")

    auth.client.close()
    auth.client = httpx.Client(transport=httpx.MockTransport(remote))
    with pytest.raises(ServiceError):
        auth.call("/login/device/code", {"client_id": "test-app"})
    assert "operation=device_code" in caplog.text
    assert "private-test-secret" not in caplog.text
    assert store.users() == []
