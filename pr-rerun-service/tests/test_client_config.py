import json
import os

import pytest

from pr_rerun.client_config import SavedLogin, clear_login, config_path, load_login, save_login

TOKEN = "prr_" + "a" * 43
LOGIN = SavedLogin("http://100.64.0.10:8080", True, TOKEN)


@pytest.fixture(autouse=True)
def private_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def test_saved_login_round_trip_permissions_and_logout():
    assert load_login() is None
    previous = os.umask(0)
    try:
        save_login(LOGIN)
    finally:
        os.umask(previous)
    assert load_login() == LOGIN
    assert config_path().stat().st_mode & 0o777 == 0o600
    assert config_path().parent.stat().st_mode & 0o777 == 0o700
    assert TOKEN not in repr(LOGIN)
    assert clear_login()
    assert load_login() is None
    assert not clear_login()


def test_default_location_is_per_user(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_path() == tmp_path / ".config" / "pr-rerun" / "client.json"


def test_relative_xdg_path_is_rejected(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    with pytest.raises(ValueError, match="absolute"):
        save_login(LOGIN)


def test_other_os_user_cannot_load_saved_login(monkeypatch):
    save_login(LOGIN)
    other_uid = os.getuid() + 1
    monkeypatch.setattr("pr_rerun.client_config.os.getuid", lambda: other_uid)
    with pytest.raises(ValueError, match="owned by you"):
        load_login()


def test_separate_user_config_does_not_inherit_login(monkeypatch, tmp_path):
    save_login(LOGIN)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
    assert load_login() is None


@pytest.mark.parametrize("target", ["directory", "file"])
@pytest.mark.parametrize("operation", [load_login, clear_login, lambda: save_login(LOGIN)])
def test_unsafe_permissions_are_rejected(target, operation):
    save_login(LOGIN)
    path = config_path().parent if target == "directory" else config_path()
    path.chmod(0o755 if target == "directory" else 0o644)
    with pytest.raises(ValueError, match="0700"):
        operation()
    assert config_path().exists()


@pytest.mark.parametrize("target", ["directory", "file"])
@pytest.mark.parametrize("operation", [load_login, clear_login, lambda: save_login(LOGIN)])
def test_symlinks_are_rejected(tmp_path, target, operation):
    path = config_path().parent if target == "directory" else config_path()
    destination = tmp_path / "target"
    if target == "directory":
        destination.mkdir(mode=0o700)
        path.parent.mkdir()
    else:
        destination.write_text("must not be overwritten")
        path.parent.mkdir(mode=0o700, parents=True)
    path.symlink_to(destination)
    with pytest.raises(ValueError):
        operation()
    assert path.is_symlink()
    if target == "file":
        assert destination.read_text() == "must not be overwritten"


def test_hard_link_is_rejected(tmp_path):
    save_login(LOGIN)
    os.link(config_path(), tmp_path / "copy")
    with pytest.raises(ValueError, match="links"):
        load_login()


def test_fifo_is_rejected_without_blocking():
    config_path().parent.mkdir(mode=0o700, parents=True)
    os.mkfifo(config_path(), mode=0o600)
    with pytest.raises(ValueError, match="special"):
        load_login()


@pytest.mark.parametrize(
    "content",
    [
        '{"access_token":"' + TOKEN,
        json.dumps({"version": 99, "access_token": TOKEN}),
        json.dumps(
            {"version": True, "server": "https://x", "netbird": False, "access_token": TOKEN}
        ),
        json.dumps(
            {"version": 1, "server": "https://x", "netbird": "false", "access_token": TOKEN}
        ),
        " " * 8193 + "{}",
    ],
)
def test_invalid_config_never_echoes_secrets_and_can_be_cleared(content):
    save_login(LOGIN)
    config_path().write_text(content)
    with pytest.raises(ValueError, match="Saved login is invalid") as result:
        load_login()
    assert TOKEN not in str(result.value)
    assert clear_login()


def test_failed_atomic_replace_keeps_previous_login(monkeypatch):
    save_login(LOGIN)

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pr_rerun.client_config.os.replace", fail)
    with pytest.raises(ValueError):
        save_login(SavedLogin("https://other.example", False, "prr_" + "b" * 43))
    assert load_login() == LOGIN
    assert [path.name for path in config_path().parent.iterdir()] == ["client.json"]
