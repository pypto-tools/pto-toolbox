"""Read-only comparison of live App installations with local enrollment."""

import sqlite3
from contextlib import closing
from pathlib import Path

from pr_rerun.app_auth import AppAuth
from pr_rerun.policy import RepositoryPolicy


def read_enrollments(database: Path) -> list[dict]:
    """Do not initialize a Store or create/migrate a database for a query."""
    try:
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT name, repository_id, installation_id, revision, policy "
                "FROM repositories ORDER BY name"
            ).fetchall()
    except sqlite3.Error:
        raise ValueError(
            "An existing App-mode service database is required for inventory"
        ) from None
    result = []
    for row in rows:
        policy = RepositoryPolicy.model_validate_json(row["policy"])
        result.append(
            {
                "repository": row["name"],
                "repository_id": row["repository_id"],
                "installation_id": row["installation_id"],
                "enabled": policy.enabled,
                "aliases": policy.aliases,
                "revision": row["revision"],
            }
        )
    return result


def inventory(auth: AppAuth, enrolled: list[dict]) -> dict:
    installations = auth.installations()
    by_name = {row["repository"]: row for row in enrolled}
    by_id = {row["repository_id"]: row for row in enrolled}
    observed = set()
    for installation in installations:
        for repository in installation["repositories"]:
            record = by_id.get(repository["repository_id"]) or by_name.get(repository["repository"])
            repository["enrollment"] = "not_enrolled"
            repository["service"] = record
            if record is not None:
                observed.add(record["repository"])
                matches = (
                    record["repository"] == repository["repository"]
                    and record["repository_id"] == repository["repository_id"]
                    and record["installation_id"] == installation["installation_id"]
                )
                repository["enrollment"] = "enrolled" if matches else "identity_changed"
    by_installation = {row["installation_id"]: row for row in installations}
    service_only = []
    for row in enrolled:
        if row["repository"] in observed:
            continue
        installation = by_installation.get(row["installation_id"])
        status = (
            "installation_missing"
            if installation is None
            else ("not_authorized" if installation["repositories_checked"] else "unknown")
        )
        service_only.append({**row, "github_status": status})
    return {
        "complete": all(row["repositories_checked"] for row in installations),
        "installations": installations,
        "service_only": service_only,
    }
