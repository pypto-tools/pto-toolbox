import json

import pytest
from pydantic import ValidationError

from pr_rerun.models import Settings, WorkflowRule


def test_file_credential_and_json_configuration(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("test-only-github-token\n")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "repository": "owner/repo",
                "workflows": [{"path": ".github/workflows/ci.yml"}],
            }
        )
    )
    monkeypatch.delenv("PR_RERUN_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PR_RERUN_GITHUB_AUTH", raising=False)
    monkeypatch.setenv("PR_RERUN_GITHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("PR_RERUN_DATABASE", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("PR_RERUN_CONFIG", str(config))
    settings = Settings.from_env()
    assert settings.repository == "owner/repo"
    assert settings.workflows[0].mode == "failed"
    assert settings.github_token.get_secret_value() == "test-only-github-token"
    assert "test-only-github-token" not in repr(settings)


@pytest.mark.parametrize(
    "path",
    [
        "../ci.yml",
        "https://evil.example/ci.yml",
        ".github/workflows/../../secret",
    ],
)
def test_configuration_paths_are_not_arbitrary_urls_or_files(path):
    with pytest.raises(ValidationError):
        WorkflowRule(path=path)


def test_all_mode_requires_explicit_codex_identity_rule():
    with pytest.raises(ValidationError):
        WorkflowRule(path=".github/workflows/ci.yml", mode="all")


def test_unknown_configuration_fields_are_rejected(settings):
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "github_url": "https://evil.example"})


def test_environment_credentials_are_explicit(monkeypatch):
    for key in (
        "PR_RERUN_CONFIG",
        "PR_RERUN_GITHUB_TOKEN",
        "PR_RERUN_GITHUB_TOKEN_FILE",
        "PR_RERUN_GITHUB_AUTH",
        "PR_RERUN_GITHUB_USER",
        "PR_RERUN_DATABASE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GH_TOKEN", "do-not-use-ambient-credentials")
    with pytest.raises(ValueError, match="Configure"):
        Settings.from_env()
