"""Per-OS-user client login, stored atomically with owner-only POSIX permissions."""

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

FILENAME = "client.json"
TOKEN_PATTERN = r"prr_[A-Za-z0-9_-]{43}"


@dataclass(frozen=True)
class SavedLogin:
    server: str
    netbird: bool
    access_token: str = field(repr=False)


def config_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    if not base.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path")
    return base / "pr-rerun" / FILENAME


def _check_owner(info: os.stat_result, *, directory: bool = False) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not correct_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and info.st_nlink != 1)
    ):
        raise ValueError(
            "Saved login must be owned by you, with directory mode 0700 and file mode 0600; "
            "links and special files are not accepted."
        )


@contextmanager
def _directory(*, create: bool = False) -> Iterator[int | None]:
    if os.name != "posix":
        raise ValueError("Persistent login requires POSIX file permissions")
    path = config_path().parent
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        try:
            _check_owner(os.fstat(descriptor), directory=True)
            yield descriptor
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError("Cannot access the private pr-rerun login directory") from error


def _open_login(directory: int) -> int | None:
    try:
        descriptor = os.open(
            FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("Cannot open the saved login; symbolic links are not accepted") from error
    try:
        _check_owner(os.fstat(descriptor))
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def load_login() -> SavedLogin | None:
    with _directory() as directory:
        if directory is None:
            return None
        descriptor = _open_login(directory)
        if descriptor is None:
            return None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                value = json.loads(source.read(8193))
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "server", "netbird", "access_token"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or not isinstance(value["server"], str)
                or not value["server"]
                or type(value["netbird"]) is not bool
                or not isinstance(value["access_token"], str)
                or not re.fullmatch(TOKEN_PATTERN, value["access_token"])
            ):
                raise ValueError
            return SavedLogin(value["server"], value["netbird"], value["access_token"])
        except (ValueError, UnicodeError) as error:
            raise ValueError(
                "Saved login is invalid; run 'pr-rerun logout', then log in again."
            ) from error


def save_login(login: SavedLogin) -> None:
    # Do not create a credential file until the caller has verified authentication.
    payload = json.dumps(
        {
            "version": 1,
            "server": login.server,
            "netbird": login.netbird,
            "access_token": login.access_token,
        }
    )
    with _directory(create=True) as directory:
        assert directory is not None
        existing = _open_login(directory)
        if existing is not None:
            os.close(existing)
        temporary = f".client-{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(payload + "\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def clear_login() -> bool:
    with _directory() as directory:
        if directory is None:
            return False
        existing = _open_login(directory)
        if existing is None:
            return False
        os.close(existing)
        os.unlink(FILENAME, dir_fd=directory)
        os.fsync(directory)
        return True
