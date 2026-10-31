"""Local repository shorthand; aliases never change server-side authorization."""

import argparse
import json
import os
import re

from pr_rerun.client_config import _directory, _open_login, _save_file

FILENAME = "aliases.json"
ALIAS = r"[A-Za-z][A-Za-z0-9_-]{0,31}"
REPOSITORY = r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+"


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


def resolve_repository(value: str) -> str:
    if re.fullmatch(REPOSITORY, value):
        return value
    if not re.fullmatch(ALIAS, value):
        raise ValueError("Repository must be owner/repository or a configured alias")
    aliases = load_aliases()
    if value not in aliases:
        raise ValueError("Unknown repository alias; use pr-rerun alias set NAME OWNER/REPO")
    return aliases[value]


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pr-rerun alias", description="Manage per-user repository aliases (no network access)"
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="List aliases")
    setting = commands.add_parser("set", help="Create or replace an alias")
    setting.add_argument("name")
    setting.add_argument("repository")
    remove = commands.add_parser("remove", help="Remove an alias")
    remove.add_argument("name")
    args = parser.parse_args(arguments)
    try:
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
