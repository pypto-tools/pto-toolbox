"""One-shot collector for a systemd timer; never writes the service database."""

import fcntl
import os
import sqlite3
import time
from contextlib import closing

from pr_rerun.app_auth import AppAuth
from pr_rerun.models import ServiceError, Settings, positive_id
from pr_rerun.policy import RepositoryPolicy, repository_name
from pr_rerun.stats import StatsStore, stats_path
from pr_rerun.stats_github import StatsGitHub, read_run, run_signature


def enrollment(database, name):
    with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM repositories WHERE name=?", (repository_name(name),)
        ).fetchone()
    if row is None:
        raise ValueError("Repository is not enrolled")
    record = dict(row)
    policy = RepositoryPolicy.model_validate_json(record["policy"])
    if not policy.enabled or not policy.analytics_enabled:
        raise ValueError("Enable analytics_enabled in the repository policy before collecting")
    return record


def sync(remote: StatsGitHub, store: StatsStore, record: dict, *, days=7, now=None):
    if type(days) is not int or not 1 <= days <= 30:
        raise ValueError("Statistics days must be between 1 and 30")
    until = int(time.time() if now is None else now)
    since = until - days * 86400
    state = {
        "since": since,
        "until": until,
        "complete": False,
        "finished": None,
        "runs_seen": 0,
        "runs_fetched": 0,
        "errors": [],
        "error_count": 0,
    }
    store.sync_state(record, state)
    seen = set()

    def failure(message):
        state["error_count"] += 1
        if len(state["errors"]) < 20:
            state["errors"].append(message)

    try:
        remote.authorize()
        for run in remote.runs(since, until):
            identity = positive_id(run["id"])
            if identity in seen:
                raise ValueError("Duplicate run across collection windows")
            seen.add(identity)
            state["runs_seen"] += 1
            cached = store.cached(record, identity)
            if (
                cached is not None
                and run["status"] == "completed"
                and cached["signature"] == run_signature(run)
            ):
                continue
            store.remove(record, identity)
            try:
                store.save(record, read_run(remote, run))
                state["runs_fetched"] += 1
            except (ServiceError, KeyError, ValueError, TypeError) as error:
                failure(
                    f"Run {identity}: "
                    + (str(error) if isinstance(error, ServiceError) else "Invalid metadata.")
                )
                if remote.requests >= remote.budget or getattr(remote, "blocked", False):
                    break
        else:
            # Only a fully enumerated run list can prove that a cached run was removed.
            store.reconcile(record, since, until, seen)
            state["complete"] = state["error_count"] == 0
    except (ServiceError, KeyError, ValueError, TypeError) as error:
        failure(str(error) if isinstance(error, ServiceError) else "Invalid collection metadata.")
    state["finished"] = int(time.time() if now is None else now)
    state["requests"] = remote.requests
    store.sync_state(record, state)
    return state


def collect(settings: Settings, name: str, *, days=7, budget=1500):
    if settings.app_id is None or settings.app_key_file is None:
        raise ValueError("CI statistics require App mode")
    path = stats_path(settings.database, settings.stats_database)
    record = enrollment(settings.database, name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another statistics collector is running") from None
        auth = AppAuth(settings.app_id, settings.app_key_file)
        remote = StatsGitHub(auth, record, budget=budget)
        try:
            result = sync(remote, StatsStore(path), record, days=days)
            # Policy changes or revocation during a long sync must not mark it complete.
            current = enrollment(settings.database, name)
            remote.authorize()
            if current["revision"] != record["revision"]:
                raise ValueError("Repository policy changed during statistics collection")
            return result
        except (ServiceError, ValueError):
            StatsStore(path).sync_state(
                record,
                {
                    "since": 0,
                    "until": int(time.time()),
                    "complete": False,
                    "finished": int(time.time()),
                    "error_count": 1,
                    "errors": ["Repository authorization or policy changed; sync again."],
                },
            )
            raise
        finally:
            remote.close()
            auth.close()
    finally:
        os.close(descriptor)
