"""Local administrator commands. User provisioning performs only a GitHub read."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from pr_rerun.app_auth import AppAuth
from pr_rerun.cli import safe_text
from pr_rerun.github import GitHub
from pr_rerun.inventory import inventory, read_enrollments
from pr_rerun.models import ServiceError, Settings, positive_id
from pr_rerun.policy import PolicyStore, RepositoryPolicy, repository_name
from pr_rerun.store import Store


def show_inventory(report: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2))
        return
    print("GitHub App installations and service enrollment (authorization snapshot):")
    if not report["installations"]:
        print("No GitHub App installations found.")
    for row in report["installations"]:
        flags = [
            "suspended" if row["suspended"] else "active",
            "selection=" + str(row["selection"]),
        ]
        if row["selection"] != "selected":
            flags.append("unsupported selection for reruns")
        if not row["permissions_ok"]:
            flags.append("missing rerun permissions")
        print(
            f"Installation {row['installation_id']}  {safe_text(row['account'])}  "
            + safe_text(", ".join(flags))
        )
        if row["error"]:
            print("  WARNING: " + safe_text(row["error"]))
        if row["repositories_checked"] and not row["repositories"]:
            print("  No repositories authorized.")
        for repository in row["repositories"]:
            status = repository["enrollment"]
            if repository["service"]:
                service = repository["service"]
                status += ", " + ("enabled" if service["enabled"] else "disabled")
                status += ", aliases=" + (",".join(service["aliases"]) or "-")
            print("  " + safe_text(repository["repository"]) + "  " + safe_text(status))
    for row in report["service_only"]:
        print("Service-only: " + safe_text(row["repository"]) + "  " + row["github_status"])
    print("No repository enrollment, rules, or workflow runs were changed.")


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
    collector = commands.add_parser("ci-sync", help="Collect read-only CI statistics")
    collector.add_argument("--repo", required=True, type=repository_name)
    collector.add_argument("--days", type=int, choices=range(1, 31), default=7)
    collector.add_argument("--request-budget", type=int, default=1500)
    collector.add_argument("--json", action="store_true", help="Output collection status as JSON")
    errors = commands.add_parser("error-sync", help="Collect redacted CI error excerpts")
    errors.add_argument("--repo", required=True, type=repository_name)
    errors.add_argument("--days", type=int, choices=range(1, 31), default=30)
    errors.add_argument("--request-budget", type=int, default=100)
    errors.add_argument("--max-logs", type=int, default=100)
    errors.add_argument("--json", action="store_true")
    revoke = commands.add_parser("revoke", help="Revoke a user's token by immutable GitHub user ID")
    revoke.add_argument("github_id", type=int)
    commands.add_parser("users", help="List identities without token hashes or credentials")
    installations = commands.add_parser(
        "installations", help="Compare GitHub App repository access with service enrollment"
    )
    installations.add_argument("--json", action="store_true", help="Output structured JSON")
    administrators = commands.add_parser(
        "service-admin", help="Manage service-wide policy administrators locally"
    )
    administrator_commands = administrators.add_subparsers(dest="action", required=True)
    administrator_commands.add_parser("list", help="List service administrators")
    for action in ("grant", "revoke"):
        role = administrator_commands.add_parser(action)
        role.add_argument("github_id", type=int, help="Verified immutable GitHub user ID")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        database = os.environ.get("PR_RERUN_DATABASE")
        if not database:
            raise ValueError("PR_RERUN_DATABASE is required")
        if args.command == "error-sync":
            from pr_rerun.error_collect import collect as collect_errors

            result = collect_errors(
                Settings.from_env(github_auth="app"),
                args.repo,
                days=args.days,
                budget=args.request_budget,
                max_logs=args.max_logs,
            )
            print(json.dumps(result, indent=2))
            if not result.get("complete"):
                parser.exit(1, "Error collection is partial; progress is retained.\n")
            return
        if args.command == "ci-sync":
            from pr_rerun.stats_collect import collect

            if not 1 <= args.request_budget <= 5000:
                raise ValueError("Request budget must be between 1 and 5000")
            result = collect(
                Settings.from_env(github_auth="app"),
                args.repo,
                days=args.days,
                budget=args.request_budget,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"CI statistics: {args.repo}")
                print(
                    f"Complete: {result['complete']}; runs seen: {result['runs_seen']}; "
                    f"runs fetched: {result['runs_fetched']}; requests: {result['requests']}"
                )
                for error in result["errors"]:
                    print("Warning: " + safe_text(error))
            if not result["complete"]:
                parser.exit(1, "Statistics sync is incomplete; cached progress is retained.\n")
            return
        if args.command == "installations":
            records = read_enrollments(Path(database))
            settings = Settings.from_env(github_auth="app")
            assert settings.app_id is not None and settings.app_key_file is not None
            auth = AppAuth(settings.app_id, settings.app_key_file)
            try:
                report = inventory(auth, records)
            finally:
                auth.close()
            show_inventory(report, as_json=args.json)
            if not report["complete"]:
                parser.exit(
                    1, "Warning: inventory is incomplete; unchecked repositories are unknown.\n"
                )
            return
        store = Store(Path(database))
        if args.command == "users":
            print(json.dumps(store.users(), indent=2))
        elif args.command == "service-admin":
            if args.action == "list":
                print(json.dumps(store.service_admins(), indent=2))
            else:
                store.set_service_admin(args.github_id, enabled=args.action == "grant")
                print("Service administrator role updated.")
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
    except sqlite3.Error:
        parser.exit(2, "Error: Cannot access the configured statistics or service database.\n")
    except ValidationError:
        parser.exit(2, "Error: Invalid configuration or repository policy.\n")
    except (ValueError, OSError, ServiceError, KeyError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
