#!/usr/bin/env python3
"""Install/update the standalone client. Bootstrap requires only Python 3.9+.

This same stdlib-only module is embedded in the standalone updater. It never
reads client logins, server state, private keys, or GitHub credentials.
"""

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

REPOSITORY = "https://github.com/pypto-tools/pto-toolbox"
VERSION_RE = r"[0-9]+\.[0-9]+\.[0-9]+"
ROOT = "pr-rerun-client"
MAX_BYTES = 256 * 1024 * 1024
LEGACY = (
    "#!/bin/sh\n"
    "# Shared executable; login and configuration still belong to the invoking user.\n"
    'exec /opt/pr-rerun-client/bin/python -I -c \'import sys; sys.argv[0] = "pr-rerun"; '
    'from pr_rerun.cli import main; main()\' "$@"\n'
)


class InstallError(Exception):
    pass


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(VERSION_RE, value):
        raise InstallError("Expected a version such as 0.4.0")
    return tuple(map(int, value.split(".")))


def architecture():
    machine = platform.machine()
    if platform.system() != "Linux" or machine not in {"aarch64", "x86_64"}:
        raise InstallError("Only Linux aarch64 and x86_64 are supported")
    try:
        libc = os.confstr("CS_GNU_LIBC_VERSION").split()[1]
        if tuple(map(int, libc.split("."))) < (2, 28):
            raise ValueError
    except (AttributeError, ValueError, OSError):
        raise InstallError("This release requires glibc 2.28 or newer") from None
    return machine


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise InstallError("Refusing a non-HTTPS download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, proxy=None, limit=MAX_BYTES):
    context = ssl.create_default_context()
    if getattr(sys, "frozen", False):
        # Portable Python cannot rely on certificate paths from the build image.
        # certifi is already bundled for httpx; the bootstrap stays stdlib-only.
        import certifi

        context.load_verify_locations(certifi.where())
    handlers = [HTTPSRedirect(), urllib.request.HTTPSHandler(context=context)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "pr-rerun-installer"})
    try:
        with opener.open(request, timeout=60) as response, destination.open("wb") as output:
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise InstallError("Download exceeds size limit")
                output.write(chunk)
    except InstallError:
        raise
    except Exception:
        # urllib exceptions may contain proxy credentials or signed redirect URLs.
        raise InstallError(
            "Download failed; check connectivity or use --proxy / --bundle"
        ) from None


def verify_checksum(bundle, checksums):
    matches = []
    if checksums.stat().st_size > 1024 * 1024:
        raise InstallError("Checksum file is too large")
    for line in checksums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == bundle.name:
            matches.append(parts[0])
    if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
        raise InstallError("Expected exactly one SHA256SUMS entry for this archive")
    if bundle.stat().st_size > MAX_BYTES:
        raise InstallError("Archive is too large")
    digest = hashlib.sha256()
    with bundle.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != matches[0]:
        raise InstallError("SHA256 mismatch; installation was not changed")
    return digest.hexdigest()


def extract(bundle, destination):
    """Extract regular files only, with bounded size and no links or traversal."""
    seen = set()
    total = 0
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                not name.parts
                or name.parts[0] != ROOT
                or ".." in name.parts
                or name.is_absolute()
                or str(name) in seen
                or not (member.isdir() or member.isfile())
            ):
                raise InstallError("Unsafe or duplicate archive member")
            seen.add(str(name))
            total += member.size
            if len(seen) > 10000 or total > MAX_BYTES * 3:
                raise InstallError("Expanded archive exceeds limits")
            target = destination.joinpath(*name.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return destination / ROOT


def manifest(directory, machine):
    value = json.loads((directory / "release.json").read_text())
    if not isinstance(value, dict):
        raise InstallError("Invalid release manifest")
    version_tuple(value.get("version"))
    if (
        value.get("format") != 1
        or value.get("architecture") != machine
        or value.get("glibc_min") != "2.28"
    ):
        raise InstallError("Bundle platform or format does not match this machine")
    return value


def probe(directory, version):
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"):
        environment.pop(key, None)
    for args, expected in ((["--version"], "pr-rerun " + version), (["--help"], None)):
        result = subprocess.run(
            [str(directory / "pr-rerun"), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if expected and result.stdout.strip() != expected:
            raise InstallError("Client self-test returned an unexpected version")


def trusted_directory(path):
    # Every existing ancestor must be protected from modification by other users.
    for item in reversed((path, *path.parents)):
        if not item.exists() and not item.is_symlink():
            item.mkdir(mode=0o755)
        info = item.lstat()
        sticky_temp = bool(info.st_mode & stat.S_ISVTX) and item != path
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or (info.st_mode & 0o022 and not sticky_temp)
        ):
            raise InstallError(
                "Installation directories must be owned and protected by the installer"
            )


def current_release(prefix, name):
    link = prefix / name
    if not link.is_symlink():
        if link.exists():
            raise InstallError("Unexpected installation state: " + name)
        return None
    target = link.resolve(strict=True)
    if target.parent != prefix / "releases":
        raise InstallError("Version link points outside the managed releases")
    version_tuple(target.name)
    if target.is_symlink() or not target.is_dir():
        raise InstallError("Invalid release directory")
    return target


def replace_link(path, target):
    # TemporaryDirectory is on the same filesystem, so replacement is atomic.
    with tempfile.TemporaryDirectory(prefix=".link-", dir=path.parent) as temporary:
        staged = Path(temporary) / "link"
        staged.symlink_to(target)
        os.replace(staged, path)


def check_commands(prefix, bin_dir, migrate):
    for name in ("pr-rerun", "pr-rerun-update"):
        path = bin_dir / name
        if path.is_symlink() and os.readlink(path) == str(prefix / "current" / name):
            continue
        if not path.exists() and not path.is_symlink():
            continue
        if (
            migrate
            and name == "pr-rerun"
            and not path.is_symlink()
            and path.is_file()
            and path.stat().st_size < 4096
            and path.read_text() == LEGACY
        ):
            continue
        raise InstallError("Existing command is not managed; refusing to replace " + str(path))


def activate(prefix, bin_dir, candidate, old):
    """Rollback changed pointers/launchers if publication or final probing fails."""
    paths = [
        bin_dir / "pr-rerun",
        bin_dir / "pr-rerun-update",
        prefix / "previous",
        prefix / "current",
    ]
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=prefix) as backup:
        saved = []
        for index, path in enumerate(paths):
            copy = Path(backup) / str(index)
            exists = path.exists() or path.is_symlink()
            if exists:
                shutil.copy2(path, copy, follow_symlinks=False)
            saved.append((path, copy, exists))
        try:
            for name in ("pr-rerun", "pr-rerun-update"):
                replace_link(bin_dir / name, prefix / "current" / name)
            if old and old != candidate:
                replace_link(prefix / "previous", old)
            replace_link(prefix / "current", candidate)
            probe(prefix / "current", candidate.name)
        except BaseException:
            for path, copy, exists in reversed(saved):
                if exists:
                    os.replace(copy, path)
                elif path.is_symlink():
                    path.unlink()
            raise


def install(args):
    machine = architecture()
    if args.version:
        version_tuple(args.version)
    if args.bundle and not args.checksums:
        raise InstallError("--bundle requires --checksums SHA256SUMS")
    if args.checksums and not args.bundle:
        raise InstallError("--checksums requires --bundle")
    if args.rollback and (args.bundle or args.version):
        raise InstallError("--rollback cannot be combined with a bundle or version")
    if args.proxy and not re.match(r"^https?://[^/]+/?$", args.proxy):
        raise InstallError("--proxy must be an HTTP or HTTPS proxy origin")
    prefix = Path(args.prefix).absolute()
    bin_dir = Path(args.bin_dir).absolute()
    if args.dry_run:
        print("Would manage client releases in", prefix, "and commands in", bin_dir)
        print("No downloads, installation changes, or login changes were made.")
        return
    for directory in (prefix, prefix / "releases", bin_dir):
        trusted_directory(directory)
    descriptor = os.open(prefix / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_commands(prefix, bin_dir, args.migrate_existing)
        old = current_release(prefix, "current")
        if args.rollback:
            previous = current_release(prefix, "previous")
            if previous is None:
                raise InstallError("No previous standalone client release is available")
            info = manifest(previous, machine)
            probe(previous, info["version"])
            activate(prefix, bin_dir, previous, old)
            print("Rolled back to pr-rerun", info["version"])
            return
        with tempfile.TemporaryDirectory(prefix=".stage-", dir=prefix) as temporary:
            stage = Path(temporary)
            if args.bundle:
                bundle, checksums = Path(args.bundle), Path(args.checksums)
                # Take a private snapshot before verifying; never execute a changing user archive.
                local_bundle = stage / bundle.name
                if bundle.stat().st_size > MAX_BYTES:
                    raise InstallError("Archive is too large")
                shutil.copyfile(bundle, local_bundle)
                bundle = local_bundle
            else:
                tag = "download/pr-rerun-v" + args.version if args.version else "latest/download"
                base = REPOSITORY + "/releases/" + tag
                bundle = stage / ("pr-rerun-client-linux-" + machine + ".tar.gz")
                checksums = stage / "SHA256SUMS"
                print("Downloading client for", machine, flush=True)
                download(base + "/SHA256SUMS", checksums, args.proxy, 1024 * 1024)
                download(base + "/" + bundle.name, bundle, args.proxy)
            digest = verify_checksum(bundle, checksums)
            extracted = extract(bundle, stage / "unpacked")
            info = manifest(extracted, machine)
            version = info["version"]
            if args.version and args.version != version:
                raise InstallError("Downloaded version does not match --version")
            if old and version_tuple(version) < version_tuple(old.name):
                raise InstallError("Refusing a downgrade; use --rollback for the previous version")
            candidate = prefix / "releases" / version
            if candidate.exists() or candidate.is_symlink():
                if candidate.is_symlink() or (candidate / ".archive-sha256").read_text() != digest:
                    raise InstallError("This version already exists with different content")
            else:
                probe(extracted, version)
                (extracted / ".archive-sha256").write_text(digest)
                # The updater is the same executable, selected by argv[0].
                (extracted / "pr-rerun-update").symlink_to("pr-rerun")
                os.replace(extracted, candidate)
            probe(candidate, version)
            legacy = bin_dir / "pr-rerun"
            if args.migrate_existing and legacy.is_file() and not legacy.is_symlink():
                shutil.copy2(legacy, prefix / "legacy-pr-rerun")
            activate(prefix, bin_dir, candidate, old)
            print("Installed pr-rerun", version, "at", bin_dir / "pr-rerun")
            print("Existing per-user logins and aliases were preserved. Log in without sudo.")


def main():
    parser = argparse.ArgumentParser(
        description="Install, update or roll back the shared PR rerun client"
    )
    parser.add_argument("--version", help="Install an exact release, e.g. 0.4.0 (default: latest)")
    parser.add_argument("--bundle", help="Install a local release archive without network access")
    parser.add_argument("--checksums", help="Trusted SHA256SUMS file for an offline archive")
    parser.add_argument("--proxy", help="HTTP proxy, e.g. http://127.0.0.1:7892")
    parser.add_argument(
        "--rollback", action="store_true", help="Switch to the previous installed release"
    )
    parser.add_argument(
        "--migrate-existing", action="store_true", help="Replace the known legacy shared launcher"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show paths without downloading or writing"
    )
    parser.add_argument("--prefix", default="/opt/pr-rerun", help="Managed release directory")
    parser.add_argument("--bin-dir", default="/usr/local/bin", help="Directory for public commands")
    args = parser.parse_args()
    os.umask(0o022)
    try:
        install(args)
    except (
        InstallError,
        OSError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        # Only our own messages are safe to display; arbitrary OS/network errors can leak data.
        message = str(error) if isinstance(error, InstallError) else type(error).__name__
        print("Error:", message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
