import json

import httpx
import pytest

from pr_rerun.aliases import load_aliases, resolve_repository
from pr_rerun.aliases import main as alias_main
from pr_rerun.cli import LOGIN_PROBE, main
from pr_rerun.client_config import SavedLogin, clear_login, config_path, load_login, save_login

TOKEN = "prr_" + "a" * 43


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (
        "PR_RERUN_URL",
        "PR_RERUN_NETBIRD_URL",
        "PR_RERUN_ACCESS_TOKEN",
        "PR_RERUN_REPOSITORY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_aliases_persist_independently_of_login_and_logout(capsys):
    assert alias_main(["set", "pto", "hw-native-sys/pypto"]) == 0
    path = config_path().with_name("aliases.json")
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_aliases() == {"pto": "hw-native-sys/pypto"}
    save_login(SavedLogin("http://127.0.0.1:8080", False, TOKEN))
    assert resolve_repository("pto") == "hw-native-sys/pypto"
    assert clear_login()
    assert resolve_repository("pto") == "hw-native-sys/pypto"
    alias_main(["list"])
    assert "pto -> hw-native-sys/pypto" in capsys.readouterr().out
    alias_main(["set", "pto", "other/project"])
    assert resolve_repository("pto") == "other/project"
    alias_main(["remove", "pto"])
    assert load_aliases() == {}


@pytest.mark.parametrize(
    "arguments",
    [
        ["set", "../bad", "owner/repo"],
        ["set", "pto", "another-alias"],
        ["set", "pto", "https://github.com/owner/repo"],
        ["remove", "missing"],
    ],
)
def test_invalid_alias_commands_do_not_create_login(arguments):
    with pytest.raises(SystemExit) as result:
        alias_main(arguments)
    assert result.value.code == 2
    assert load_login() is None


def test_unknown_alias_is_rejected_before_network_or_token_prompt(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pr-rerun", "123", "--repo", "missing"])
    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client", lambda **kwargs: pytest.fail("unexpected network")
    )
    monkeypatch.setattr(
        "pr_rerun.cli.getpass.getpass", lambda _: pytest.fail("unexpected token prompt")
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


def test_alias_resolves_to_full_repository_and_does_not_change_default(monkeypatch):
    alias_main(["set", "other", "another/project"])
    saved = SavedLogin("http://127.0.0.1:8080", False, TOKEN, "team/default")
    save_login(saved)
    monkeypatch.setattr("sys.argv", ["pr-rerun", "123", "--repo", "other"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    client_class = httpx.Client

    def respond(request):
        assert json.loads(request.content) == {"pr": 123, "repository": "another/project"}
        return httpx.Response(403, json={"error": "Repository grant denied"})

    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert load_login() == saved


def test_login_saves_expanded_repository_so_remapping_does_not_change_default(monkeypatch):
    alias_main(["set", "pto", "hw-native-sys/pypto"])
    monkeypatch.setenv("PR_RERUN_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr("sys.argv", ["pr-rerun", "login", "--token-login", "--repo", "pto"])
    client_class = httpx.Client

    def respond(request):
        assert request.url.path == LOGIN_PROBE
        return httpx.Response(404, json={"error": "Request not found."})

    monkeypatch.setattr(
        "pr_rerun.cli.httpx.Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    alias_main(["set", "pto", "another/project"])
    saved = load_login()
    assert saved is not None and saved.repository == "hw-native-sys/pypto"


def test_alias_file_does_not_follow_symlinks(tmp_path):
    config_path().parent.mkdir(mode=0o700, parents=True)
    target = tmp_path / "target"
    target.write_text("untouched")
    config_path().with_name("aliases.json").symlink_to(target)
    with pytest.raises(ValueError):
        load_aliases()
    with pytest.raises(SystemExit):
        alias_main(["set", "pto", "owner/repo"])
    assert target.read_text() == "untouched"
