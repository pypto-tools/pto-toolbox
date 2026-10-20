import subprocess

import pytest

from pr_rerun.credentials import github_credential
from pr_rerun.models import Settings


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch):
    for key in (
        "PR_RERUN_GITHUB_AUTH",
        "PR_RERUN_GITHUB_TOKEN",
        "PR_RERUN_GITHUB_TOKEN_FILE",
        "PR_RERUN_GITHUB_USER",
        "PR_RERUN_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)


def test_gh_login_requires_explicit_selection(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ambient-secret")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("Do not invoke gh"))
    with pytest.raises(ValueError, match="Configure"):
        github_credential()


@pytest.mark.parametrize("selected_user", [None, "alice"])
def test_gh_credential_is_captured_and_ambient_tokens_are_removed(monkeypatch, selected_user):
    monkeypatch.setenv("PR_RERUN_GITHUB_AUTH", "gh")
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_DEBUG",
    ):
        monkeypatch.setenv(key, "must-not-propagate")
    if selected_user:
        monkeypatch.setenv("PR_RERUN_GITHUB_USER", selected_user)

    def run(arguments, **kwargs):
        expected = ["gh", "auth", "token", "--hostname", "github.com"]
        if selected_user:
            expected.extend(["--user", selected_user])
        assert arguments == expected
        assert kwargs["capture_output"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 15
        assert kwargs["check"] is True
        assert "must-not-propagate" not in kwargs["env"].values()
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        return subprocess.CompletedProcess(arguments, 0, stdout="gh-test-secret\n")

    monkeypatch.setattr("subprocess.run", run)
    assert github_credential() == "gh-test-secret"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("sensitive-path"),
        subprocess.TimeoutExpired("gh", 15, output="secret-output"),
        subprocess.CalledProcessError(1, "gh", output="secret-output", stderr="secret-stderr"),
    ],
)
def test_gh_failure_is_sanitized_without_fallback(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("subprocess.run", fail)
    with pytest.raises(ValueError, match="same OS user") as caught:
        github_credential("gh")
    assert "secret" not in str(caught.value)
    assert "sensitive" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("value", ["", "\n", "one two", "first\nsecond"])
def test_invalid_gh_output_is_not_used(monkeypatch, value):
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=value),
    )
    with pytest.raises(ValueError, match="usable credential"):
        github_credential("gh")


@pytest.mark.parametrize("key", ["PR_RERUN_GITHUB_TOKEN", "PR_RERUN_GITHUB_TOKEN_FILE"])
def test_conflicting_credentials_are_rejected_before_reading(monkeypatch, key):
    monkeypatch.setenv(key, "do-not-read")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("Do not invoke gh"))
    with pytest.raises(ValueError, match="Unset"):
        github_credential("gh")


def test_token_mode_does_not_invoke_gh(monkeypatch):
    monkeypatch.setenv("PR_RERUN_GITHUB_TOKEN", "dedicated-token")
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("Do not invoke gh"))
    assert github_credential() == "dedicated-token"


def test_settings_support_explicit_gh_auth_without_exposing_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_RERUN_DATABASE", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("PR_RERUN_GITHUB_AUTH", "token")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="gh-test-secret\n"),
    )
    settings = Settings.from_env(github_auth="gh")
    assert settings.github_token.get_secret_value() == "gh-test-secret"
    assert "gh-test-secret" not in repr(settings)
    assert "gh-test-secret" not in settings.model_dump_json()


@pytest.mark.parametrize("mode", ["auto", "GH", "", "invalid"])
def test_unknown_auth_modes_fail_closed(mode):
    with pytest.raises(ValueError, match="token or gh"):
        github_credential(mode)
