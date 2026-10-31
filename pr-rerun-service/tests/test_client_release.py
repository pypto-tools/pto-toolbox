"""Offline installer invariants; never touch shared paths or contact GitHub."""

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

RELEASE = Path(__file__).resolve().parents[1] / "release"
spec = importlib.util.spec_from_file_location("client_install", RELEASE / "client_install.py")
assert spec and spec.loader
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(autouse=True)
def installer_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def bundle(tmp_path, version="0.4.0", machine=None, broken=False, extra=()):
    root = tmp_path / version
    root.mkdir(exist_ok=True)
    archive = root / "pr-rerun-client-linux-test.tar.gz"
    info = {
        "format": 1,
        "version": version,
        "architecture": machine or installer.architecture(),
        "glibc_min": "2.28",
    }
    program = "#!/bin/sh\n" + ("exit 1\n" if broken else f"echo 'pr-rerun {version}'\n")
    with tarfile.open(archive, "w:gz") as target:
        for name, content, mode in [
            ("pr-rerun-client/pr-rerun", program.encode(), 0o755),
            ("pr-rerun-client/release.json", json.dumps(info).encode(), 0o644),
            *extra,
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            target.addfile(member, io.BytesIO(content))
    sums = root / "SHA256SUMS"
    sums.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    return archive, sums


def arguments(tmp_path, archive=None, sums=None, **changes):
    values = {
        "prefix": str(tmp_path / "installation"),
        "bin_dir": str(tmp_path / "bin"),
        "bundle": str(archive) if archive else None,
        "checksums": str(sums) if sums else None,
        "version": None,
        "rollback": False,
        "migrate_existing": False,
        "proxy": None,
        "dry_run": False,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_install_update_rollback_preserves_login(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    login = config / "client.json"
    login.write_text("test login must not be touched")
    login.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    archive, sums = bundle(tmp_path)
    args = arguments(tmp_path, archive, sums)
    installer.install(args)
    original = (Path(args.prefix) / "current").resolve()
    assert (
        subprocess.check_output([str(Path(args.bin_dir) / "pr-rerun"), "--version"]).strip()
        == b"pr-rerun 0.4.0"
    )
    newer, new_sums = bundle(tmp_path, "0.4.1")
    installer.install(arguments(tmp_path, newer, new_sums))
    assert (Path(args.prefix) / "previous").resolve() == original
    installer.install(arguments(tmp_path, rollback=True))
    assert (Path(args.prefix) / "current").resolve() == original
    assert login.read_text() == "test login must not be touched"
    assert login.stat().st_mode & 0o777 == 0o600


def test_idempotent_install_does_not_destroy_rollback(tmp_path):
    installer.install(arguments(tmp_path, *bundle(tmp_path)))
    installer.install(arguments(tmp_path, *bundle(tmp_path, "0.4.1")))
    # Reuse the exact immutable archive, rather than regenerate its gzip timestamp.
    root = tmp_path / "0.4.1"
    installer.install(
        arguments(tmp_path, root / "pr-rerun-client-linux-test.tar.gz", root / "SHA256SUMS")
    )
    assert (tmp_path / "installation/previous").resolve().name == "0.4.0"


@pytest.mark.parametrize("failure", ["checksum", "selftest", "architecture", "version"])
def test_rejected_update_leaves_current_unchanged(tmp_path, failure):
    installer.install(arguments(tmp_path, *bundle(tmp_path)))
    archive, sums = bundle(
        tmp_path,
        "0.4.1",
        machine="wrong" if failure == "architecture" else None,
        broken=failure == "selftest",
    )
    if failure == "checksum":
        archive.write_bytes(archive.read_bytes() + b"tampering")
    args = arguments(tmp_path, archive, sums, version="0.4.2" if failure == "version" else None)
    with pytest.raises((installer.InstallError, subprocess.CalledProcessError)):
        installer.install(args)
    assert (tmp_path / "installation/current").resolve().name == "0.4.0"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "pr-rerun-client/../../escape"])
def test_rejects_archive_traversal(tmp_path, name):
    archive, sums = bundle(tmp_path, extra=[(name, b"bad", 0o777)])
    with pytest.raises(installer.InstallError, match="Unsafe"):
        installer.install(arguments(tmp_path, archive, sums))
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_rejects_links_and_special_files(tmp_path, kind):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        member = tarfile.TarInfo("pr-rerun-client/unsafe")
        member.type = kind
        member.linkname = "/etc/passwd"
        target.addfile(member)
    with pytest.raises(installer.InstallError, match="Unsafe"):
        installer.extract(archive, tmp_path / "unpacked")


def test_refuses_unmanaged_command_and_explicitly_migrates_known_legacy(tmp_path):
    args = arguments(tmp_path, *bundle(tmp_path))
    binary = Path(args.bin_dir) / "pr-rerun"
    binary.parent.mkdir()
    binary.write_text("unrelated command")
    with pytest.raises(installer.InstallError, match="not managed"):
        installer.install(args)
    args.migrate_existing = True
    with pytest.raises(installer.InstallError, match="not managed"):
        installer.install(args)
    binary.write_text(installer.LEGACY)
    binary.chmod(0o755)
    installer.install(args)
    assert binary.is_symlink()
    assert (Path(args.prefix) / "legacy-pr-rerun").read_text() == installer.LEGACY


def test_activation_failure_restores_current_and_previous(tmp_path, monkeypatch):
    installer.install(arguments(tmp_path, *bundle(tmp_path)))
    installer.install(arguments(tmp_path, *bundle(tmp_path, "0.4.1")))
    real_probe = installer.probe

    def fail_after_switch(directory, version):
        if directory.name == "current":
            raise installer.InstallError("Simulated activation failure")
        return real_probe(directory, version)

    monkeypatch.setattr(installer, "probe", fail_after_switch)
    with pytest.raises(installer.InstallError, match="Simulated"):
        installer.install(arguments(tmp_path, *bundle(tmp_path, "0.4.2")))
    assert (tmp_path / "installation/current").resolve().name == "0.4.1"
    assert (tmp_path / "installation/previous").resolve().name == "0.4.0"


def test_dry_run_never_downloads_or_creates_directories(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("dry run downloaded a file")

    monkeypatch.setattr(installer, "download", unexpected)
    installer.install(arguments(tmp_path, dry_run=True))
    assert list(tmp_path.iterdir()) == []


def test_rejects_writable_or_symlinked_install_directory(tmp_path):
    args = arguments(tmp_path, *bundle(tmp_path))
    prefix = Path(args.prefix)
    prefix.mkdir(mode=0o777)
    prefix.chmod(0o777)
    with pytest.raises(installer.InstallError, match="protected"):
        installer.install(args)
    prefix.rmdir()
    prefix.symlink_to(tmp_path / "0.4.0", target_is_directory=True)
    with pytest.raises(installer.InstallError, match="protected"):
        installer.install(args)


def test_bootstrap_source_parses_on_python39():
    ast.parse((RELEASE / "client_install.py").read_text(), feature_version=(3, 9))


def test_version_command_without_login(monkeypatch, capsys):
    from pr_rerun.cli import main
    from pr_rerun.version import VERSION

    monkeypatch.setattr("sys.argv", ["pr-rerun", "--version"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == "pr-rerun " + VERSION


def test_duplicate_checksum_entries_are_rejected(tmp_path):
    archive, sums = bundle(tmp_path)
    sums.write_text(sums.read_text() * 2)
    with pytest.raises(installer.InstallError, match="exactly one"):
        installer.verify_checksum(archive, sums)


def test_archive_is_verified_before_extraction(tmp_path, monkeypatch):
    archive, sums = bundle(tmp_path)
    archive.write_bytes(b"not a tar file")

    def unexpected(*args):
        pytest.fail("Extracted unverified archive")

    monkeypatch.setattr(installer, "extract", unexpected)
    with pytest.raises(installer.InstallError, match="SHA256 mismatch"):
        installer.install(arguments(tmp_path, archive, sums))


def test_rejects_concurrent_install(tmp_path):
    args = arguments(tmp_path, *bundle(tmp_path))
    prefix = Path(args.prefix)
    prefix.mkdir()
    with (prefix / ".lock").open("w") as lock:
        installer.fcntl.flock(lock, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            installer.install(args)


def test_no_automatic_downgrade(tmp_path):
    installer.install(arguments(tmp_path, *bundle(tmp_path, "0.4.1")))
    with pytest.raises(installer.InstallError, match="downgrade"):
        installer.install(arguments(tmp_path, *bundle(tmp_path)))
    assert (tmp_path / "installation/current").resolve().name == "0.4.1"
