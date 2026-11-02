"""Independent resumable log collection; no workflow writes or service DB mutations."""

import fcntl
import json
import os
import sqlite3
import time
from contextlib import closing

from pr_rerun.app_auth import AppAuth
from pr_rerun.error_logs import LogGitHub
from pr_rerun.errors import ErrorStore, candidates, read
from pr_rerun.models import ServiceError, Settings
from pr_rerun.policy import RepositoryPolicy
from pr_rerun.stats import key, stats_path
from pr_rerun.stats_collect import enrollment


def enabled(settings, name):
    record = enrollment(settings.database, name)
    if not RepositoryPolicy.model_validate_json(record["policy"]).error_logs_enabled:
        raise ValueError("Enable error_logs_enabled before collecting error excerpts")
    return record


def sync(remote, store, record, *, days=30, max_logs=100, authorize=lambda: None, now=None):
    now = int(time.time() if now is None else now)
    previous = read(store.path, record, days=days, now=now)["sync"]
    if previous.get("pause_until", 0) > now:
        return previous
    queued, metadata = candidates(store.path, record, days, now)
    until = metadata.get("until", now)
    store.enqueue(record, queued, now)
    with closing(sqlite3.connect(store.path)) as db:
        jobs = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM ci_errors WHERE repository=? AND created BETWEEN ? AND ? "
                "ORDER BY created DESC,id",
                (key(record), until - days * 86400, until),
            )
        ]
    state = {
        "started": now,
        "finished": None,
        "days": days,
        "processed": 0,
        "error_count": 0,
        "requests": 0,
        "pause_until": 0,
        "metadata_until": metadata.get("until", 0),
        "metadata_complete": bool(metadata.get("complete")),
        "queued_candidates": len(queued),
        "downloaded_bytes": 0,
    }
    store.state(record, state)
    try:
        remote.authorize()
        for item in jobs:
            if state["processed"] >= max_logs or state["downloaded_bytes"] >= 64 * 1024 * 1024:
                break
            if item["status"] not in {"pending", "retry"} or item.get("retry_at", 0) > now:
                continue
            if remote.requests >= remote.budget:
                break
            authorize()
            try:
                outcome = remote.download(
                    item["job_id"],
                    item["step_number"],
                    max_bytes=64 * 1024 * 1024 - remote.downloaded_bytes,
                )
                authorize()
                item.update(outcome, collected=int(time.time()), tries=item["tries"] + 1)
            except ServiceError:
                if remote.blocked:
                    state["pause_until"] = remote.pause_until
                    state["error_count"] += 1
                    break
                item["tries"] += 1
                item.update(
                    status="retry" if item["tries"] < 3 else "failed",
                    retry_at=now + 600,
                    reason="Log retrieval failed; details withheld.",
                )
                state["error_count"] += 1
            state["downloaded_bytes"] = remote.downloaded_bytes
            store.save(record, item)
            state["processed"] += 1
            state["requests"] = remote.requests
            store.state(record, state)
        authorize()
        remote.authorize()
    except Exception:
        state["error_count"] += 1
        raise
    finally:
        state["finished"] = int(time.time())
        state["requests"] = remote.requests
        store.state(record, state)
    report = read(store.path, record, days=days, now=now)
    return {
        **state,
        "coverage": report["coverage"],
        "complete": report["metadata_complete"] and report["logs_complete"],
    }


def collect(settings: Settings, name: str, *, days=30, budget=100, max_logs=100):
    if not 1 <= days <= 30 or not 1 <= budget <= 500 or not 1 <= max_logs <= 500:
        raise ValueError("Use days 1..30 and log budget/count 1..500")
    if settings.app_id is None or settings.app_key_file is None:
        raise ValueError("Error collection requires App mode")
    record = enabled(settings, name)
    path = stats_path(settings.database, settings.stats_database)
    if not path.is_file():
        raise ValueError("Run ci-sync before error-sync")
    descriptor = os.open(str(path) + ".errors.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another error collector is running") from None
        auth = AppAuth(settings.app_id, settings.app_key_file)
        remote = LogGitHub(auth, record, budget=budget)

        def authorize():
            current = enabled(settings, name)
            if current["revision"] != record["revision"]:
                raise ValueError("Repository policy changed; retry collection")

        try:
            return sync(
                remote, ErrorStore(path), record, days=days, max_logs=max_logs, authorize=authorize
            )
        finally:
            remote.close()
            auth.close()
    finally:
        os.close(descriptor)
