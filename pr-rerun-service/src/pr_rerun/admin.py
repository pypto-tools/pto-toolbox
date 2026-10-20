"""Local administrator commands. User provisioning performs only a GitHub read."""

import argparse
import json
import os
from pathlib import Path

from pr_rerun.github import GitHub
from pr_rerun.models import ServiceError, Settings, positive_id
from pr_rerun.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local PR rerun service identities")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser(
        "issue", help="Issue or rotate a token bound to a verified GitHub user"
    )
    issue.add_argument("login")
    issue.add_argument(
        "--github-auth", choices=("token", "gh"), help="Credential source (or PR_RERUN_GITHUB_AUTH)"
    )
    revoke = commands.add_parser("revoke", help="Revoke a user's token by immutable GitHub user ID")
    revoke.add_argument("github_id", type=int)
    commands.add_parser("users", help="List identities without token hashes or credentials")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        database = os.environ.get("PR_RERUN_DATABASE")
        if not database:
            raise ValueError("PR_RERUN_DATABASE is required")
        store = Store(Path(database))
        if args.command == "users":
            print(json.dumps(store.users(), indent=2))
        elif args.command == "revoke":
            if not store.revoke(positive_id(args.github_id)):
                raise ValueError("GitHub user ID was not enrolled")
            print("Access revoked.")
        else:
            settings = Settings.from_env(github_auth=args.github_auth)
            github = GitHub(settings.repository, settings.github_token.get_secret_value())
            try:
                user = github.user(args.login)
            finally:
                github.close()
            github_id = positive_id(user["id"])
            if user["type"] != "User":
                raise ValueError("Only human GitHub accounts can be enrolled")
            token = store.issue_token(github_id, user["login"])
            print(
                json.dumps(
                    {
                        "github_id": github_id,
                        "login": user["login"],
                        "access_token": token,
                        "notice": "Save securely. Issuing another token revokes this one.",
                    },
                    indent=2,
                )
            )
    except (ValueError, ServiceError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
