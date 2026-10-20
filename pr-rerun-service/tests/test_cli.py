import json

import httpx
import pytest

from pr_rerun.cli import configured_server, main, run_client, safe_text, server_origin


@pytest.fixture(autouse=True)
def isolate_saved_login(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://localhost.evil.example",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?token=secret",
        "file:///tmp/socket",
    ],
)
def test_credentials_require_trusted_origin_and_remote_tls(url):
    with pytest.raises(ValueError):
        server_origin(url)


def test_loopback_http_and_remote_https_are_accepted():
    assert server_origin("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert server_origin("https://rerun.example.com") == "https://rerun.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.10:8080",
        "http://10.1.2.3:8080",
        "http://172.16.1.2:8080",
        "http://192.168.1.2:8080",
        "http://[fd00::1234]:8080",
    ],
)
def test_remote_http_requires_explicit_netbird_configuration(url):
    with pytest.raises(ValueError, match="HTTPS"):
        server_origin(url)
    assert server_origin(url, netbird=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://peer.netbird.cloud",
        "http://8.8.8.8:8080",
        "http://100.63.255.255:8080",
        "http://100.128.0.1:8080",
        "http://169.254.169.254",
        "http://0.0.0.0",
        "http://127.0.0.1",
        "http://[::1]",
        "http://[fd00::1%eth0]:8080",
        "http://user:secret@100.64.0.10:8080",
        "http://100.64.0.10:8080/path",
        "http://100.64.0.10:8080?secret=value",
        "http://100.64.0.10:8080#fragment",
        "http://100.64.0.10:0",
        "http://100.64.0.10:65536",
        "http://100.64.0.10:invalid",
        "file://100.64.0.10",
    ],
)
def test_netbird_option_does_not_allow_arbitrary_destinations(url):
    with pytest.raises(ValueError):
        server_origin(url, netbird=True)


def test_server_selection_and_environment_conflicts(monkeypatch):
    monkeypatch.delenv("PR_RERUN_URL", raising=False)
    monkeypatch.delenv("PR_RERUN_NETBIRD_URL", raising=False)
    assert configured_server(None, None) == "http://127.0.0.1:8080"
    vpn = "http://100.64.0.10:8080"
    monkeypatch.setenv("PR_RERUN_NETBIRD_URL", vpn)
    assert configured_server(None, None) == vpn
    monkeypatch.setenv("PR_RERUN_URL", "https://rerun.example.com")
    with pytest.raises(ValueError, match="only one"):
        configured_server(None, None)
    assert configured_server("https://explicit.example.com", None) == "https://explicit.example.com"
    assert configured_server(None, vpn) == vpn


@pytest.mark.parametrize("source", ["environment", "argument"])
def test_netbird_cli_keeps_auth_and_does_not_follow_redirects(monkeypatch, capsys, source):
    token = "prr_" + "a" * 43
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", token)
    monkeypatch.delenv("PR_RERUN_NETBIRD_URL", raising=False)
    monkeypatch.delenv("PR_RERUN_URL", raising=False)
    arguments = ["pr-rerun", "123"]
    if source == "environment":
        monkeypatch.setenv("PR_RERUN_NETBIRD_URL", "http://100.64.0.10:8080")
    else:
        arguments.extend(["--netbird-server", "http://100.64.0.10:8080"])
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == f"Bearer {token}"
        assert str(request.url) == "http://100.64.0.10:8080/api/reruns/preview"
        return httpx.Response(307, headers={"location": "http://8.8.8.8/stolen"})

    client_type = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("pr_rerun.cli.httpx.Client", client)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert len(calls) == 1
    assert "redirects are not accepted" in capsys.readouterr().err


def test_terminal_control_characters_are_escaped():
    assert safe_text("\x1b[2Jtitle\n\u202e") == r"\x1b[2Jtitle\n\u202e"
    assert safe_text("修复测试") == "修复测试"


def test_no_noninteractive_confirmation(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("No request should have been made")
        ),
    ) as client:
        with pytest.raises(ValueError, match="interactive"):
            run_client(client, "123", None)


@pytest.mark.parametrize("answer,expected_posts", [("yes", 1), ("owner/repo#123 @alice", 2)])
def test_cli_requires_exact_confirmation(monkeypatch, capsys, answer, expected_posts):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/preview"):
            return httpx.Response(
                200,
                json={
                    "eligible": True,
                    "repository": "owner/repo",
                    "pr": {
                        "number": 123,
                        "title": "\x1b[2JFake title",
                        "author": "alice",
                        "head_sha": "a" * 40,
                        "base_ref": "main",
                        "url": "https://github.com/pr",
                    },
                    "runs": [
                        {
                            "name": "CI",
                            "mode": "failed",
                            "attempt": 1,
                            "failed_jobs": ["test"],
                            "url": "https://github.com/run",
                        }
                    ],
                    "ticket": "t" * 43,
                    "confirmation": "owner/repo#123 @alice",
                    "request_id": "rr_" + "a" * 32,
                },
            )
        assert json.loads(request.content) == {
            "ticket": "t" * 43,
            "confirmation": "owner/repo#123 @alice",
        }
        return httpx.Response(
            202,
            json={
                "request_id": "rr_" + "a" * 32,
                "state": "accepted",
                "runs": [],
            },
        )

    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(respond)
    ) as client:
        run_client(client, "123", None)
    assert len(calls) == expected_posts
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert r"\x1b" in output


def test_cli_reports_request_id_after_lost_confirmation_response(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "owner/repo#123 @alice")
    request_id = "rr_" + "a" * 32

    def respond(request):
        if request.url.path.endswith("/confirm"):
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(
            200,
            json={
                "eligible": True,
                "repository": "owner/repo",
                "pr": {
                    "number": 123,
                    "title": "Fix",
                    "author": "alice",
                    "head_sha": "a" * 40,
                    "base_ref": "main",
                    "url": "https://github.com/pr",
                },
                "runs": [],
                "ticket": "t" * 43,
                "confirmation": "owner/repo#123 @alice",
                "request_id": request_id,
            },
        )

    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(respond)
    ) as client:
        assert run_client(client, "123", None) == 2
    assert f"pr-rerun status {request_id}" in capsys.readouterr().out
