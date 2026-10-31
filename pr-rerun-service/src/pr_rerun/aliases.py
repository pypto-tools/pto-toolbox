"""Local repository shorthand; aliases never change server-side authorization."""

import argparse
import json
import os
import re
from collections.abc import Callable

from pr_rerun.client_config import _directory, _open_login, _save_file

FILENAME = "aliases.json"
ALIAS = r"[A-Za-z][A-Za-z0-9_-]{0,31}"
REPOSITORY = r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+"


class UnknownAlias(ValueError):
    pass


def load_aliases() -> dict[str, str]:
    with _directory() as directory:
        if directory is None:
            return {}
        descriptor = _open_login(directory, FILENAME)
        if descriptor is None:
            return {}
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                value = json.loads(source.read(65537))
            if not isinstance(value, dict) or len(value) > 100:
                raise ValueError
            for alias, repository in value.items():
                if (
                    not re.fullmatch(ALIAS, alias)
                    or not isinstance(repository, str)
                    or len(repository) > 200
                    or not re.fullmatch(REPOSITORY, repository)
                ):
                    raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise ValueError("Invalid repository alias file; inspect aliases.json.") from None


def resolve_repository(value: str, official: Callable[[str], str] | None = None) -> str:
    if re.fullmatch(REPOSITORY, value):
        return value
    if not re.fullmatch(ALIAS, value):
        raise ValueError("Repository must be owner/repository or a configured alias")
    aliases = load_aliases()
    if value not in aliases:
        if official is None:
            raise UnknownAlias(
                "Unknown repository alias; log in to use official aliases "
                "or configure a local alias"
            )
        repository = official(value)
        if not isinstance(repository, str) or not re.fullmatch(REPOSITORY, repository):
            raise ValueError("Invalid official alias response")
        return repository
    return aliases[value]


def main(arguments: list[str], remote: Callable | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pr-rerun alias", description="Manage local aliases or authenticated official aliases"
    )
    commands = parser.add_subparsers(dest="command")
    listing = commands.add_parser("list", help="List aliases")
    setting = commands.add_parser("set", help="Create or replace an alias")
    setting.add_argument("name")
    setting.add_argument("repository")
    remove = commands.add_parser("remove", help="Remove an alias")
    remove.add_argument("name")
    remove.add_argument("--repo", help="Full repository name, required for official removal")
    for command in (listing, setting, remove):
        command.add_argument(
            "--official",
            action="store_true",
            help="Use server aliases (maintainer required for changes)",
        )
        endpoint = command.add_mutually_exclusive_group()
        endpoint.add_argument("--server")
        endpoint.add_argument("--netbird-server")
    args = parser.parse_args(arguments)
    try:
        if getattr(args, "official", False):
            if remote is None:
                raise ValueError("Official alias client is unavailable")
            return remote(args)
        if (
            getattr(args, "server", None)
            or getattr(args, "netbird_server", None)
            or getattr(args, "repo", None)
        ):
            raise ValueError("Server and repository options require --official")
        aliases = load_aliases()
        if args.command in {None, "list"}:
            for alias, repository in sorted(aliases.items()):
                print(f"{alias} -> {repository}")
            if not aliases:
                print("No repository aliases configured.")
            return 0
        if not re.fullmatch(ALIAS, args.name):
            raise ValueError(
                "Alias must start with a letter and contain up to 32 letters, "
                "digits, underscores or hyphens"
            )
        if args.command == "set":
            if len(args.repository) > 200 or not re.fullmatch(REPOSITORY, args.repository):
                raise ValueError("Alias target must be a full owner/repository name")
            aliases[args.name] = args.repository
            if len(aliases) > 100:
                raise ValueError("At most 100 aliases are supported")
        else:
            if args.name not in aliases:
                raise ValueError("Alias does not exist")
            del aliases[args.name]
        _save_file(FILENAME, json.dumps(aliases, sort_keys=True))
        print(
            f"{args.name} -> {args.repository}"
            if args.command == "set"
            else f"Removed alias: {args.name}"
        )
        return 0
    except (OSError, ValueError) as error:
        parser.error(str(error))
