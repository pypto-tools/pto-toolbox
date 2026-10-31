"""Local administrator commands. User provisioning performs only a GitHub read."""

import argparse
import json
import os
from pathlib import Path

from pydantic import ValidationError

from pr_rerun.app_auth import AppAuth
from pr_rerun.github import GitHub
from pr_rerun.models import ServiceError, Settings, positive_id
from pr_rerun.policy import PolicyStore, RepositoryPolicy, repository_name
from pr_rerun.store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local PR rerun service identities")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser(
        "issue", help="Issue or rotate a token bound to a verified GitHub user"
    )
    issue.add_argument("login")
    issue.add_argument(
        "--github-auth",
        choices=("token", "gh", "app"),
        help="Credential source (or PR_RERUN_GITHUB_AUTH)",
    )
    issue.add_argument("--repo", help="App-authorized repository for identity lookup")
    enroll = commands.add_parser("repo-add", help="Enroll an explicitly installed repository")
    enroll.add_argument("repository", type=repository_name)
    enroll.add_argument("--policy-file", type=Path, required=True)
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
        elif args.command == "repo-add":
            settings = Settings.from_env(github_auth="app")
            assert settings.app_id is not None and settings.app_key_file is not None
            policy = RepositoryPolicy.model_validate_json(args.policy_file.read_text())
            enrolled = {user["github_id"] for user in store.users() if user["enabled"]}
            if not set(policy.users + policy.maintainers) <= enrolled:
                raise ValueError("Issue service identities for every policy user first")
            auth = AppAuth(settings.app_id, settings.app_key_file)
            try:
                _, repo_id, installation_id = auth.credentials(args.repository)
                auth.validate_workflows(args.repository, repo_id, installation_id, policy.workflows)
                PolicyStore(store).enroll(args.repository, repo_id, installation_id, policy)
            finally:
                auth.close()
            print("Repository enrolled at policy revision 1.")
        else:
            settings = Settings.from_env(github_auth=args.github_auth)
            auth = None
            if settings.app_id is not None:
                if not args.repo or settings.app_key_file is None:
                    raise ValueError("App identity enrollment requires --repo owner/repository")
                auth = AppAuth(settings.app_id, settings.app_key_file)
                try:
                    _, repo_id, installation_id = auth.credentials(args.repo)
                    github = auth.github(args.repo, repo_id, installation_id)
                except Exception:
                    auth.close()
                    raise
            else:
                github = GitHub(settings.repository, settings.github_token.get_secret_value())
            try:
                user = github.user(args.login)
            finally:
                github.close()
                if auth is not None:
                    auth.close()
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
    except ValidationError:
        parser.exit(2, "Error: Invalid configuration or repository policy.\n")
    except (ValueError, OSError, ServiceError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
