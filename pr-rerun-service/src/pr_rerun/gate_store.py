"""Durable, bounded webhook inbox; a single worker owns reconciliation."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class GateStore:
    def __init__(self, path: Path):
        self.path = path
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "delivery TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, "
                "due REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ready_events ON events(state, due)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS approved ("
                "repository INTEGER, run INTEGER, attempt INTEGER, updated REAL NOT NULL, "
                "PRIMARY KEY(repository, run, attempt))"
            )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, delivery: str, payload: dict) -> bool:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT state FROM events WHERE delivery=?", (delivery,)
            ).fetchone()
            if existing and existing["state"] != "failed":
                return True
            if (
                db.execute("SELECT count(*) FROM events WHERE state='waiting'").fetchone()[0]
                >= 10000
            ):
                return False
            if existing:
                db.execute(
                    "UPDATE events SET state='waiting', due=?, failures=0 WHERE delivery=?",
                    (now, delivery),
                )
                return True
            db.execute(
                "INSERT INTO events VALUES (?, ?, 'waiting', ?, 0, ?)",
                (delivery, json.dumps(payload), now, now),
            )
        return True

    def next(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM events WHERE state='waiting' AND due<=? ORDER BY due LIMIT 1",
                (time.time(),),
            ).fetchone()
            return dict(row) if row else None

    def update(self, delivery: str, payload: dict, state: str, delay: int = 0, failures: int = 0):
        now = time.time()
        with self.connection() as db:
            db.execute(
                "UPDATE events SET payload=?, state=?, due=?, failures=?, updated=? "
                "WHERE delivery=?",
                (json.dumps(payload), state, now + delay, failures, now, delivery),
            )

    def wake(self, repository_id: int):
        # Index the small active queue, not GitHub comment history. Events contain no secrets.
        with self.connection() as db:
            for row in db.execute("SELECT delivery,payload FROM events WHERE state='waiting'"):
                event = json.loads(row["payload"])
                if event["kind"] == "gate" and event["repository_id"] == repository_id:
                    db.execute(
                        "UPDATE events SET due=? WHERE delivery=?", (time.time(), row["delivery"])
                    )

    def was_approved(self, repository_id: int, run_id: int, attempt: int) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM approved WHERE repository=? AND run=? AND attempt=?",
                    (repository_id, run_id, attempt),
                ).fetchone()
                is not None
            )

    def approve(self, repository_id: int, run_id: int, attempt: int):
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO approved VALUES (?, ?, ?, ?)",
                (repository_id, run_id, attempt, time.time()),
            )

    def prune(self):
        with self.connection() as db:
            db.execute(
                "DELETE FROM events WHERE state!='waiting' AND updated<?",
                (time.time() - 31 * 86400,),
            )
            db.execute("DELETE FROM approved WHERE updated<?", (time.time() - 36 * 86400,))
