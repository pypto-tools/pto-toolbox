"""Repository-scoped error excerpts in the existing independent analytics database."""

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from pr_rerun.error_logs import scrub
from pr_rerun.models import ServiceError
from pr_rerun.stats import key
from pr_rerun.stats_github import iso

ERROR_ID = re.compile(r"^err_[a-f0-9]{32}$")
CATEGORIES = ("network", "resource", "timeout", "test", "compile", "exception", "unclassified")


def candidates(path: Path, record: dict, days: int, now: int) -> tuple[list[dict], dict]:
    result = []
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT data FROM ci_sync WHERE repository=?", (key(record),)).fetchone()
        metadata = json.loads(row[0]) if row else {}
        until = metadata.get("until", now)
        for (value,) in db.execute(
            "SELECT data FROM ci_runs WHERE repository=? AND created BETWEEN ? AND ? "
            "ORDER BY created DESC,run_id DESC",
            (key(record), until - days * 86400, until),
        ):
            run = json.loads(value)
            for job in run["jobs"]:
                if job["status"] != "completed" or not (
                    job["conclusion"] in {"failure", "timed_out"} or job["failed_steps"]
                ):
                    continue
                details = job.get("failed_step_details", [])
                steps = (
                    details
                    if details and len(details) == len(job["failed_steps"])
                    else [{"number": None, "name": " / ".join(job["failed_steps"])}]
                )
                for step in steps:
                    identity = (
                        f"{key(record)}:{run['id']}:{job['attempt']}:{job['id']}:{step['number']}"
                    )
                    result.append(
                        {
                            "id": "err_" + hashlib.sha256(identity.encode()).hexdigest()[:32],
                            "run_id": run["id"],
                            "attempt": job["attempt"],
                            "job_id": job["id"],
                            "created": run["created_at"],
                            "workflow": scrub(run["workflow"]),
                            "job": scrub(job["name"]),
                            "event": scrub(run["event"]),
                            "branch": scrub(run["branch"]),
                            "step_number": step["number"],
                            "step": scrub(step["name"]),
                            "conclusion": job["conclusion"],
                            "url": f"https://github.com/{record['name']}/actions/runs/{run['id']}/job/{job['id']}",
                        }
                    )
    return result, metadata


class ErrorStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ci_errors (
                    repository TEXT NOT NULL, id TEXT NOT NULL, created INTEGER NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(repository,id));
                CREATE INDEX IF NOT EXISTS ci_error_time ON ci_errors(repository,created);
                CREATE TABLE IF NOT EXISTS ci_error_sync (
                    repository TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    def enqueue(self, record: dict, items: list[dict], now: int):
        with closing(sqlite3.connect(self.path)) as db, db:
            for item in items:
                value = dict(item, status="pending", tries=0, retry_at=0)
                db.execute(
                    "INSERT OR IGNORE INTO ci_errors VALUES (?,?,?,?)",
                    (key(record), item["id"], item["created"], json.dumps(value)),
                )
            # Retain one day of overlap, as for the statistics cache.
            db.execute(
                "DELETE FROM ci_errors WHERE repository=? AND created<?",
                (key(record), now - 31 * 86400),
            )

    def save(self, record: dict, item: dict):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "UPDATE ci_errors SET data=? WHERE repository=? AND id=?",
                (json.dumps(item), key(record), item["id"]),
            )

    def state(self, record: dict, state: dict):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO ci_error_sync VALUES (?,?)",
                (key(record), json.dumps(state)),
            )


def read(
    path: Path,
    record: dict,
    *,
    days=7,
    limit=20,
    offset=0,
    error_id=None,
    workflow=None,
    job=None,
    category=None,
    event=None,
    branch=None,
    now=None,
) -> dict:
    if not 1 <= days <= 30 or not 1 <= limit <= 200 or not 0 <= offset <= 100000:
        raise ValueError("Invalid error query range or pagination")
    if error_id is not None and not ERROR_ID.fullmatch(error_id):
        raise ServiceError(400, "Invalid error ID.")
    if category is not None and category not in CATEGORIES:
        raise ServiceError(400, "Invalid error category.")
    now = int(time.time() if now is None else now)
    state, metadata, items = {}, {}, []
    until = now
    if path.is_file():
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "ci_sync" in tables:
                row = db.execute(
                    "SELECT data FROM ci_sync WHERE repository=?", (key(record),)
                ).fetchone()
                metadata = json.loads(row[0]) if row else {}
                until = metadata.get("until", now)
            if "ci_error_sync" in tables:
                row = db.execute(
                    "SELECT data FROM ci_error_sync WHERE repository=?", (key(record),)
                ).fetchone()
                state = json.loads(row[0]) if row else {}
            if "ci_errors" in tables:
                if error_id is not None:
                    row = db.execute(
                        "SELECT data FROM ci_errors WHERE repository=? AND id=? AND created>=?",
                        (key(record), error_id, now - 31 * 86400),
                    ).fetchone()
                    if row:
                        return {"repository": record["name"], "error": json.loads(row[0])}
                else:
                    items = [
                        json.loads(r[0])
                        for r in db.execute(
                            "SELECT data FROM ci_errors WHERE repository=? "
                            "AND created BETWEEN ? AND ? "
                            "ORDER BY created DESC,id",
                            (key(record), until - days * 86400, until),
                        )
                    ]
    if error_id is not None:
        raise ServiceError(404, "Error record not found for this repository.")
    filtered = [
        item
        for item in items
        if all(
            value is None or item.get(field) == value
            for field, value in (
                ("workflow", workflow),
                ("job", job),
                ("category", category),
                ("event", event),
                ("branch", branch),
            )
        )
    ]
    counts = Counter(item["status"] for item in items)
    counts["truncated"] = sum(bool(item.get("truncated")) for item in items)
    groups = {}
    for item in filtered:
        if item["status"] != "ready":
            continue
        fingerprint = item["fingerprint"]
        group = groups.setdefault(
            fingerprint,
            {
                "fingerprint": fingerprint,
                "category": item["category"],
                "summary": item["summary"],
                "example_id": item["id"],
                "count": 0,
            },
        )
        group["count"] += 1
    # Use the metadata snapshot cutoff, just as the statistics report does.
    metadata_complete = bool(
        metadata.get("complete")
        and metadata.get("since", now) <= metadata.get("until", now) - days * 86400
    )
    return {
        "repository": record["name"],
        "since": iso(until - days * 86400),
        "until": iso(until),
        "sync": state,
        "metadata_complete": metadata_complete,
        "stale": now - metadata.get("until", 0) > 1800,
        "coverage": dict(counts),
        "logs_complete": bool(
            state.get("finished")
            and state.get("metadata_complete")
            and state.get("days", 0) >= days
            and not state.get("error_count")
            and state.get("metadata_until", 0) >= metadata.get("until", now)
            and not any(
                counts[s] for s in ("pending", "retry", "failed", "unavailable", "truncated")
            )
        ),
        "total": len(filtered),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(filtered) else None,
        "groups": sorted(groups.values(), key=lambda x: (-x["count"], x["fingerprint"]))[:limit],
        "items": [
            {k: v for k, v in item.items() if k != "excerpt"}
            for item in filtered[offset : offset + limit]
        ],
    }
