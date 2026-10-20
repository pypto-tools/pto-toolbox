import httpx
import pytest

from pr_rerun.cli import LOGIN_PROBE, main, selected_token
from pr_rerun.client_config import SavedLogin, config_path, load_login, save_login

TOKEN = "prr_" + "a" * 43
VPN = "http://100.64.0.10:8080"


@pytest.fixture(autouse=True)
def isolated_login(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("PR_RERUN_URL", "PR_RERUN_NETBIRD_URL", "PR_RERUN_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr("sys.argv", ["pr-rerun", *arguments])
    with pytest.raises(SystemExit) as result:
        main()
    return result.value.code


def mock_server(monkeypatch, respond):
    client_type = httpx.Client

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("pr_rerun.cli.httpx.Client", factory)


def test_login_then_rerun_uses_saved_endpoint_and_token(monkeypatch, capsys):
    calls = []
    answers = iter([TOKEN])
    monkeypatch.setattr("pr_rerun.cli.getpass.getpass", lambda prompt: next(answers))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert str(request.url).startswith(VPN + "/")
        if request.url.path == LOGIN_PROBE:
            return httpx.Response(404, json={"error": "Request not found."})
        return httpx.Response(
            200,
            json={
                "eligible": False,
                "repository": "owner/repo",
                "pr": {
                    "number": 123,
                    "title": "Fix",
                    "author": "alice",
                    "head_sha": "a" * 40,
                    "base_ref": "main",
                    "url": "https://github.com/owner/repo/pull/123",
                },
            },
        )

    mock_server(monkeypatch, respond)
    assert invoke(monkeypatch, "login", "--netbird-server", VPN) == 0
    assert load_login() == SavedLogin(VPN, True, TOKEN)
    assert calls == [("GET", LOGIN_PROBE)]  # No preview or rerun during login.
    assert invoke(monkeypatch, "123") == 2
    assert calls[-1] == ("POST", "/api/reruns/preview")
    output = capsys.readouterr()
    assert "Login saved" in output.out
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize("kind", ["invalid", "redirect", "generic404", "success", "timeout"])
def test_failed_verification_does_not_replace_saved_login(monkeypatch, capsys, kind):
    old = SavedLogin("https://old.example", False, "prr_" + "b" * 43)
    save_login(old)
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ConnectTimeout("timeout", request=request)
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://elsewhere.example"})
        return httpx.Response(
            {"invalid": 401, "generic404": 404, "success": 200}[kind],
            json={"error": TOKEN},
        )

    mock_server(monkeypatch, respond)
    assert invoke(monkeypatch, "login", "--netbird-server", VPN) == 2
    assert load_login() == old
    assert len(calls) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_invalid_token_is_never_saved_or_sent(monkeypatch):
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", "bad-token")
    mock_server(monkeypatch, lambda request: pytest.fail("No HTTP call expected"))
    assert invoke(monkeypatch, "login", "--netbird-server", VPN) == 2
    assert not config_path().exists()


@pytest.mark.parametrize("override", ["argument", "environment"])
def test_changed_server_never_receives_stored_token(monkeypatch, override):
    save_login(SavedLogin(VPN, True, TOKEN))
    new_token = "prr_" + "c" * 43
    monkeypatch.setattr("pr_rerun.cli.getpass.getpass", lambda prompt: new_token)
    new_server = "https://new.example"
    args = ["status", "rr_" + "a" * 32]
    if override == "argument":
        args += ["--server", new_server]
    else:
        monkeypatch.setenv("PR_RERUN_URL", new_server)

    def respond(request):
        assert request.url.host == "new.example"
        assert request.headers["authorization"] == f"Bearer {new_token}"
        return httpx.Response(404, json={"error": "Request not found."})

    mock_server(monkeypatch, respond)
    assert invoke(monkeypatch, *args) == 2
    assert load_login() == SavedLogin(VPN, True, TOKEN)


def test_environment_token_overrides_saved_login(monkeypatch):
    other = "prr_" + "b" * 43
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", other)
    assert selected_token(VPN, SavedLogin(VPN, True, TOKEN), login=False) == other


def test_equivalent_origin_reuses_saved_token(monkeypatch):
    monkeypatch.setattr("pr_rerun.cli.getpass.getpass", lambda prompt: pytest.fail("No prompt"))
    assert (
        selected_token(
            "https://rerun.example",
            SavedLogin("https://RERUN.example:443", False, TOKEN),
            login=False,
        )
        == TOKEN
    )


def test_logout_only_removes_local_login(monkeypatch, capsys):
    save_login(SavedLogin(VPN, True, TOKEN))
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    mock_server(monkeypatch, lambda request: pytest.fail("Logout must not call the server"))
    monkeypatch.setattr("sys.argv", ["pr-rerun", "logout"])
    main()
    assert load_login() is None
    assert "still set" in capsys.readouterr().out
    main()  # Repeated logout is harmless.


def test_revoked_saved_token_prompts_reauthentication(monkeypatch, capsys):
    save_login(SavedLogin(VPN, True, TOKEN))
    mock_server(monkeypatch, lambda request: httpx.Response(401, json={"error": TOKEN}))
    assert invoke(monkeypatch, "status", "rr_" + "a" * 32) == 2
    output = capsys.readouterr()
    assert "pr-rerun login" in output.err
    assert TOKEN not in output.err
