import json

import httpx
import pytest

from pr_rerun.cli import configured_server, main, run_batch, run_client, safe_text, server_origin


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


@pytest.mark.parametrize(
    "answer,expected_posts",
    [("", 1), ("n", 1), ("yes", 1), ("owner/repo#123 @alice", 1), ("y", 2), (" Y ", 2)],
)
@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("jobs", ["legacy", "mixed", "cancelled"])
def test_cli_requires_explicit_yes_and_preserves_server_confirmation(
    monkeypatch, capsys, answer, expected_posts, verbose, jobs
):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers = iter([answer, "n"])

    def respond_to_prompt(prompt):
        assert prompt == "Confirm? [y/N] "
        return next(answers)

    monkeypatch.setattr("builtins.input", respond_to_prompt)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/preview"):
            return httpx.Response(
                200,
                json={
                    "eligible": True,
                    "skipped": ["Run 456: completed / success."],
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
                            "failed_jobs": [] if jobs == "cancelled" else ["test"],
                            **({"cancelled_jobs": ["compile"]} if jobs != "legacy" else {}),
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
        run_client(client, "123", None, verbose=verbose)
    assert len(calls) == expected_posts
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert r"\x1b" in output
    assert "Confirm reruns for: owner/repo#123 @alice" in output
    assert "Type exactly" not in output
    assert ("Excluded: Run 456: completed / success." in output) == verbose
    assert ("    Failed: test" in output) == (jobs != "cancelled")
    assert ("    Cancelled: compile" in output) == (jobs != "legacy")
    assert "    Failed: \n" not in output


def test_cli_reports_request_id_after_lost_confirmation_response(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
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


class BatchRemote:
    def __init__(self):
        self.events = []
        self.scenarios = {}
        self.alias_lookups = 0

    @staticmethod
    def request_id(number):
        return f"rr_{number:032x}"

    def __call__(self, request):
        if request.url.path == "/api/repository-aliases/demo":
            self.alias_lookups += 1
            assert request.method == "GET"
            return httpx.Response(200, json={"repository": "owner/repo"})
        assert request.method == "POST"
        payload = json.loads(request.content)
        if request.url.path == "/api/reruns/preview":
            number = payload["pr"]
            assert payload.get("repository", "owner/repo") == "owner/repo"
            self.events.append(("preview", number))
            scenario = self.scenarios.get(number)
            if scenario == "forbidden":
                return httpx.Response(403, json={"error": "You may rerun only your own PRs."})
            if scenario == "preview_timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(
                200,
                json={
                    "eligible": scenario != "ineligible",
                    "repository": "owner/repo",
                    "pr": {
                        "number": number + 100 if scenario == "wrong_pr" else number,
                        "title": f"Fix {number}",
                        "author": "alice",
                        "head_sha": "a" * 40,
                        "base_ref": "main",
                        "url": f"https://github.com/owner/repo/pull/{number}",
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
                    "request_id": self.request_id(number),
                    "ticket": f"ticket-{number}",
                    "confirmation": f"owner/repo#{number} @alice",
                },
            )
        assert request.url.path == "/api/reruns/confirm"
        number = int(payload["ticket"].removeprefix("ticket-"))
        assert payload["confirmation"] == f"owner/repo#{number} @alice"
        self.events.append(("confirm", number))
        scenario = self.scenarios.get(number)
        if scenario == "confirm_timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if scenario == "confirm_interrupt":
            raise KeyboardInterrupt
        return httpx.Response(
            202,
            json={
                "request_id": self.request_id(number),
                "state": "partial" if scenario == "partial" else "accepted",
                "runs": [],
            },
        )


@pytest.fixture
def batch_remote(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    return BatchRemote()


def test_batch_checks_and_confirms_each_pr_in_order_and_deduplicates(batch_remote, capsys):
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_batch(client, ["10", "11", "10", "12"], "owner/repo") == 0
    assert batch_remote.events == [
        ("preview", 10),
        ("confirm", 10),
        ("preview", 11),
        ("confirm", 11),
        ("preview", 12),
        ("confirm", 12),
    ]
    output = capsys.readouterr().out
    assert "Batch results" in output
    assert "PR 3/3: #12" in output
    assert all(batch_remote.request_id(n) in output for n in (10, 11, 12))


def test_batch_decline_skips_only_current_pr(batch_remote, monkeypatch, capsys):
    answers = iter(["n", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_batch(client, ["10", "11", "12"], "owner/repo") == 1
    assert batch_remote.events == [
        ("preview", 10),
        ("preview", 11),
        ("confirm", 11),
        ("preview", 12),
        ("confirm", 12),
    ]
    assert "declined" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["y", "n", ""])
def test_invalid_confirmation_reprompts_without_submitting_or_skipping_pr(
    batch_remote, monkeypatch, capsys, answer
):
    invalid = ["yes", "maybe", "ｙ", "y\u200b", "y\x00", "\x1b[200~y\x1b[201~"]
    answers = iter([*invalid, answer])

    def respond(prompt):
        assert prompt == "Confirm? [y/N] "
        assert batch_remote.events == [("preview", 10)]
        return next(answers)

    monkeypatch.setattr("builtins.input", respond)
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_client(client, "10", None, "owner/repo") == (0 if answer == "y" else 1)
    expected = [("preview", 10)] + ([("confirm", 10)] if answer == "y" else [])
    assert batch_remote.events == expected
    output = capsys.readouterr().out
    assert output.count("Input not recognized.") == len(invalid)
    assert ("Cancelled. No rerun was requested." in output) == (answer != "y")
    assert "\x1b" not in output and "\x00" not in output


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_invalid_confirmation_then_interrupt_never_submits(batch_remote, monkeypatch, interrupt):
    calls = 0

    def respond(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "maybe"
        raise interrupt

    monkeypatch.setattr("builtins.input", respond)
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        with pytest.raises(interrupt):
            run_client(client, "10", None, "owner/repo")
    assert batch_remote.events == [("preview", 10)]


@pytest.mark.parametrize(
    "scenario",
    [
        "forbidden",
        "ineligible",
        "preview_timeout",
        "confirm_timeout",
        "partial",
        "wrong_pr",
    ],
)
def test_batch_continues_after_one_pr_error_without_retrying(batch_remote, capsys, scenario):
    batch_remote.scenarios[11] = scenario
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_batch(client, ["10", "11", "12"], "owner/repo") == 2
    assert batch_remote.events[0:2] == [("preview", 10), ("confirm", 10)]
    assert batch_remote.events[-2:] == [("preview", 12), ("confirm", 12)]
    assert batch_remote.events.count(("preview", 11)) == 1
    assert batch_remote.events.count(("confirm", 11)) == (
        1 if scenario in {"confirm_timeout", "partial"} else 0
    )
    output = capsys.readouterr().out
    assert "Batch results" in output
    if scenario == "confirm_timeout":
        assert "needs inspection" in output
        assert f"pr-rerun status {batch_remote.request_id(11)}" in output


@pytest.mark.parametrize("exception", [EOFError, KeyboardInterrupt])
def test_batch_interrupt_at_prompt_stops_remaining_prs(
    batch_remote, monkeypatch, capsys, exception
):
    count = 0

    def answer(prompt):
        nonlocal count
        count += 1
        if count == 2:
            raise exception
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_batch(client, ["10", "11", "12"], "owner/repo") == 1
    assert batch_remote.events == [("preview", 10), ("confirm", 10), ("preview", 11)]
    output = capsys.readouterr().out
    assert "not processed" in output and "Earlier accepted requests remain submitted" in output


def test_batch_interrupt_during_confirm_keeps_uncertainty_and_request_id(batch_remote, capsys):
    batch_remote.scenarios[11] = "confirm_interrupt"
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        assert run_batch(client, ["10", "11", "12"], "owner/repo") == 2
    assert batch_remote.events == [
        ("preview", 10),
        ("confirm", 10),
        ("preview", 11),
        ("confirm", 11),
    ]
    output = capsys.readouterr().out
    assert "needs inspection" in output and "not processed" in output
    assert f"pr-rerun status {batch_remote.request_id(11)}" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["10", "11", "12", "--repo", "demo"],
        ["--repo", "demo", "10", "11", "12"],
        ["10", "--repo", "demo", "11", "12"],
    ],
)
def test_batch_cli_parses_numbers_and_options_and_resolves_alias_once(
    batch_remote, monkeypatch, arguments
):
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", "prr_" + "a" * 43)
    monkeypatch.setenv("PR_RERUN_URL", "https://example.test")
    monkeypatch.delenv("PR_RERUN_NETBIRD_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["pr-rerun", *arguments])
    original = httpx.Client
    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(batch_remote), **kwargs),
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert batch_remote.alias_lookups == 1
    assert batch_remote.events == [
        ("preview", 10),
        ("confirm", 10),
        ("preview", 11),
        ("confirm", 11),
        ("preview", 12),
        ("confirm", 12),
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["0", "10"],
        ["10", "0", "12"],
        ["10", "-1"],
        ["10", "11", "oops"],
        ["10", "1.5"],
        ["10", "12345678901"],
        ["10", "--repo", "demo", "login"],
        *[
            [name, "10", "11"]
            for name in ("login", "logout", "repos", "stats", "errors", "error", "policy", "status")
        ],
    ],
)
def test_invalid_batch_arguments_fail_before_credentials_or_requests(monkeypatch, arguments):
    monkeypatch.setattr("sys.argv", ["pr-rerun", *arguments])
    monkeypatch.setattr(
        "pr_rerun.cli.load_login", lambda: pytest.fail("Read credentials for invalid input")
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


def test_batch_noninteractive_input_never_submits(batch_remote, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(batch_remote)
    ) as client:
        with pytest.raises(ValueError, match="interactive"):
            run_batch(client, ["10", "11"], "owner/repo")
    assert not batch_remote.events
