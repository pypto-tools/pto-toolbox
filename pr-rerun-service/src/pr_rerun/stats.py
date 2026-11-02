"""Separate statistics storage and deterministic first-attempt aggregation."""

import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from pr_rerun.models import ServiceError
from pr_rerun.stats_github import iso


def stats_path(database: Path, configured: Path | None = None) -> Path:
    path = configured or database.with_name(database.name + ".stats.sqlite")
    if path.resolve() == database.resolve() or (
        path.exists() and database.exists() and path.samefile(database)
    ):
        raise ValueError("Statistics must use a separate database")
    return path


def key(record: dict) -> str:
    return f"{record['repository_id']}:{record['installation_id']}"


class StatsStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ci_runs (
                    repository TEXT NOT NULL, run_id INTEGER NOT NULL,
                    created INTEGER NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(repository, run_id));
                CREATE INDEX IF NOT EXISTS ci_created ON ci_runs(repository, created);
                CREATE TABLE IF NOT EXISTS ci_sync (
                    repository TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)

    def cached(self, record: dict, run_id: int) -> dict | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT data FROM ci_runs WHERE repository=? AND run_id=?", (key(record), run_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, record: dict, run: dict):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO ci_runs VALUES (?, ?, ?, ?)",
                (key(record), run["id"], run["created_at"], json.dumps(run)),
            )

    def remove(self, record: dict, run_id: int):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM ci_runs WHERE repository=? AND run_id=?", (key(record), run_id))

    def sync_state(self, record: dict, state: dict):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO ci_sync VALUES (?, ?)", (key(record), json.dumps(state))
            )

    def reconcile(self, record: dict, since: int, until: int, seen: set[int]):
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = db.execute(
                "SELECT run_id FROM ci_runs WHERE repository=? AND created BETWEEN ? AND ?",
                (key(record), since, until),
            )
            missing = [row[0] for row in rows if row[0] not in seen]
            db.executemany(
                "DELETE FROM ci_runs WHERE repository=? AND run_id=?",
                [(key(record), identity) for identity in missing],
            )
            db.execute(
                "DELETE FROM ci_runs WHERE repository=? AND created<?",
                (key(record), until - 31 * 86400),
            )


def report(
    path: Path,
    record: dict,
    *,
    days=7,
    sort="failures",
    event=None,
    branch=None,
    workflow=None,
    job=None,
    limit=20,
    now=None,
) -> dict:
    if (
        days not in range(1, 31)
        or sort not in {"failures", "failure-rate"}
        or not 1 <= limit <= 200
    ):
        raise ValueError("Invalid statistics range, sort or limit")
    if not path.is_file():
        raise ServiceError(503, "No CI statistics collected; ask the operator to run ci-sync.")
    now = int(time.time() if now is None else now)
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        row = db.execute("SELECT data FROM ci_sync WHERE repository=?", (key(record),)).fetchone()
        if row is None:
            raise ServiceError(503, "No CI statistics collected for this repository.")
        sync = json.loads(row[0])
        until = sync["until"]
        since = until - days * 86400
        rows = db.execute(
            "SELECT data FROM ci_runs WHERE repository=? AND created BETWEEN ? AND ?",
            (key(record), since, until),
        )
        groups = {}
        run_count = 0
        for (data,) in rows:
            run = json.loads(data)
            if (
                (event and run["event"] != event)
                or (branch and run["branch"] != branch)
                or (workflow and run["workflow"] != workflow)
            ):
                continue
            run_count += 1
            by_name = defaultdict(list)
            for item in run["jobs"]:
                by_name[item["name"]].append(item)
            for name, jobs in by_name.items():
                if job and name != job:
                    continue
                group_key = run["workflow_id"], run["workflow"], name
                if group_key not in groups:
                    groups[group_key] = {
                        "workflow": run["workflow"],
                        "job": name,
                        "results": Counter(),
                        "rerun_executions": 0,
                        "retried_failures": 0,
                        "recovered": 0,
                        "ambiguous_recovery_runs": 0,
                        "failed_steps": Counter(),
                        "durations": [],
                    }
                group = groups[group_key]
                initial = [item for item in jobs if item["attempt"] == 1]
                reruns = [item for item in jobs if item["attempt"] > 1]
                group["rerun_executions"] += len(reruns)
                for item in initial:
                    result = item["conclusion"] if item["status"] == "completed" else "pending"
                    group["results"][result or "other"] += 1
                    group["failed_steps"].update(item["failed_steps"])
                    if item["duration_seconds"] is not None and item["status"] == "completed":
                        group["durations"].append(item["duration_seconds"])
                attempts = Counter(item["attempt"] for item in jobs)
                if len(initial) != 1 or any(count > 1 for count in attempts.values()):
                    group["ambiguous_recovery_runs"] += 1
                elif initial[0]["conclusion"] in {"failure", "timed_out"} and reruns:
                    group["retried_failures"] += 1
                    if any(
                        item["status"] == "completed" and item["conclusion"] == "success"
                        for item in reruns
                    ):
                        group["recovered"] += 1
        output = []
        for group in groups.values():
            results = group.pop("results")
            durations = sorted(group.pop("durations"))
            samples = sum(results.get(state, 0) for state in ("success", "failure", "timed_out"))
            failures, timeouts = results.get("failure", 0), results.get("timed_out", 0)
            output.append(
                {
                    **group,
                    "results": dict(results),
                    "samples": samples,
                    "failures": failures,
                    "timeouts": timeouts,
                    "failure_rate": (failures + timeouts) / samples if samples else None,
                    "p50_seconds": durations[math.ceil(len(durations) * 0.5) - 1]
                    if durations
                    else None,
                    "p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1]
                    if durations
                    else None,
                    "failed_steps": dict(group["failed_steps"].most_common()),
                }
            )
        output.sort(
            key=lambda row: (
                -(
                    row["failures"] + row["timeouts"]
                    if sort == "failures"
                    else row["failure_rate"]
                    if row["failure_rate"] is not None
                    else -1
                ),
                -row["samples"],
                row["workflow"],
                row["job"],
            )
        )
    return {
        "repository": record["name"],
        "since": iso(since),
        "until": iso(until),
        "time_basis": "workflow run creation; first attempt only",
        "last_sync": iso(sync["finished"]) if sync.get("finished") else None,
        "complete": bool(sync["complete"] and sync["since"] <= since),
        "stale": now - until > 1800,
        "sync": sync,
        "run_count": run_count,
        "filters": {"event": event, "branch": branch, "workflow": workflow, "job": job},
        "total_groups": len(output),
        "jobs": output[:limit],
    }
