"""Public, signature-authenticated CI gate listener, separate from the private rerun API."""

import argparse
import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from pr_rerun.app_auth import AppAuth
from pr_rerun.gate_github import GateGitHub
from pr_rerun.gate_policy import GateConfig, GatePolicy, command, parse_event, timestamp
from pr_rerun.gate_store import GateStore

LOG = logging.getLogger(__name__)
MAX_BODY = 1024 * 1024
TITLE = re.compile(r"CI gate PR #([1-9][0-9]*) @([0-9a-f]{40}) base:([0-9a-f]{40})")


def reconcile(policy: GatePolicy, event: dict, github: GateGitHub, store: GateStore) -> str:
    """Approve only the current PR version and attempt; return waiting/done/obsolete."""
    run = github.run(event["run"])
    match = TITLE.fullmatch(run["display_title"])
    if (
        match is None
        or run["event"] != "pull_request"
        or run["repository"]["id"] != policy.repository_id
        or run["path"] != policy.workflow
        or run["head_sha"] != match[2]
        or run["status"] == "completed"
    ):
        return "obsolete"
    number = int(match[1])
    attempt = run["run_attempt"]
    if (
        ("attempt" in event and event["attempt"] != attempt)
        or timestamp(event["created_at"]) < timestamp(run["run_started_at"])
        or time.time() - timestamp(event["created_at"]) > 30 * 86400
    ):
        return "obsolete"
    event["attempt"] = attempt
    if store.was_approved(policy.repository_id, event["run"], attempt):
        return "done"
    workflow = github.workflow(run["workflow_id"])
    if workflow["path"] != policy.workflow or workflow["state"] != "active":
        return "obsolete"

    def current(pr):
        return (
            pr["state"] == "open"
            and not pr.get("merged", False)
            and pr["head"]["sha"] == match[2]
            and pr["base"]["sha"] == match[3]
            and pr["base"]["ref"] == policy.base_branch
            and pr["base"]["repo"]["id"] == policy.repository_id
            and pr["head"]["repo"]["id"] == run["head_repository"]["id"]
            and pr["head"]["ref"] == run["head_branch"]
        )

    if not current(github.pull(number)):
        return "obsolete"
    # Deployments may refer to the PR head or GitHub's test merge. In the latter
    # case check the actual parents; never guess PR identity from a shared SHA.
    if event["sha"] != match[2]:
        commit = github.commit(event["sha"])
        if [parent["sha"] for parent in commit["parents"]] != [match[3], match[2]]:
            return "obsolete"
    jobs = github.jobs(event["run"], attempt)
    for name in policy.lint_jobs:
        matching = [job for job in jobs if job["name"] == name]
        if len(matching) != 1 or matching[0]["conclusion"] != "success":
            return "waiting"
    since = max(timestamp(run["created_at"]), timestamp(run["run_started_at"]))
    if not any(command(comment, since) for comment in github.comments(number)):
        return "waiting"
    # GitHub has no compare-and-swap approval API. Recheck immediately before
    # writing; workflow concurrency cancels obsolete runs after a concurrent push.
    latest = github.run(event["run"])
    if (
        latest["run_attempt"] != attempt
        or latest["head_sha"] != match[2]
        or latest["status"] == "completed"
        or not current(github.pull(number))
    ):
        return "obsolete"
    github.approve(event["run"], number)
    store.approve(policy.repository_id, event["run"], attempt)
    LOG.info(
        "Approved CI gate repository=%s run=%s attempt=%s",
        policy.repository_id,
        event["run"],
        attempt,
    )
    return "done"


class Worker:
    def __init__(
        self,
        config: GateConfig,
        store: GateStore,
        auth: AppAuth,
        factory: Callable[[AppAuth, GatePolicy], GateGitHub] = GateGitHub,
    ):
        self.policies = {policy.repository_id: policy for policy in config.repositories}
        self.store, self.auth, self.factory = store, auth, factory

    def tick(self) -> bool:
        row = self.store.next()
        if row is None:
            return False
        event = json.loads(row["payload"])
        policy = self.policies.get(event["repository_id"])
        try:
            if policy is None or policy.installation_id != event["installation_id"]:
                state = "obsolete"
            elif event["kind"] == "comment":
                self.store.wake(policy.repository_id)
                state = "done"
            elif event["environment"] != policy.environment:
                state = "obsolete"
            else:
                github = self.factory(self.auth, policy)
                try:
                    state = reconcile(policy, event, github, self.store)
                finally:
                    github.close()
            self.store.update(row["delivery"], event, state, delay=300)
        except Exception as error:
            # Never log exception text, payloads or upstream responses: they may
            # contain credentials. Keep the delivery ID for operator redelivery.
            failures = row["failures"] + 1
            state = "failed" if failures >= 20 else "waiting"
            self.store.update(
                row["delivery"], event, state, min(3600, 2 ** min(failures, 12)), failures
            )
            LOG.error(
                "CI gate delivery=%s state=%s failures=%s error_type=%s",
                row["delivery"],
                state,
                failures,
                type(error).__name__,
            )
        return True


def create_app(
    config: GateConfig, database: Path, secret: bytes, auth: AppAuth, *, worker_enabled=True
):
    if len(secret) < 32:
        raise ValueError("Use a webhook secret of at least 32 bytes")
    store = GateStore(database)
    worker = Worker(config, store, auth)

    @asynccontextmanager
    async def lifespan(app):
        # One worker owns this database, including during rolling deployments.
        with Path(str(database) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stop = asyncio.Event()

            async def consume():
                last_prune = 0.0
                while not stop.is_set():
                    try:
                        if time.monotonic() - last_prune > 3600:
                            await asyncio.to_thread(store.prune)
                            last_prune = time.monotonic()
                        if await asyncio.to_thread(worker.tick):
                            continue
                    except Exception:
                        LOG.error("CI gate worker could not access its inbox; retrying")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass

            task = asyncio.create_task(consume()) if worker_enabled else None
            app.state.consumer = task
            try:
                yield
            finally:
                stop.set()
                if task:
                    await task
                auth.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store, app.state.worker = store, worker

    @app.get("/health")
    async def health():
        task = getattr(app.state, "consumer", None)
        if task is not None and task.done():
            raise HTTPException(503, "Gate worker stopped")
        return {"ok": True}

    @app.post("/github/ci-gate", status_code=202)
    async def webhook(request: Request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "JSON required")
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_BODY:
                raise HTTPException(413, "Webhook too large")
            data.extend(chunk)
        expected = "sha256=" + hmac.new(secret, data, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(401, "Invalid signature")
        delivery = request.headers.get("x-github-delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise HTTPException(400, "Invalid delivery ID")
        try:
            value = json.loads(data)
            parsed = parse_event(config, request.headers.get("x-github-event", ""), value)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(400, "Invalid webhook payload") from None
        if parsed and not store.enqueue(delivery, parsed):
            raise HTTPException(503, "Gate inbox is full; redeliver later")
        return {"accepted": parsed is not None}

    return app


def read_secret(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_nlink != 1
            or info.st_size > 4096
        ):
            raise ValueError("Webhook secret must be an owner-only regular file (0400 or 0600)")
        return os.read(descriptor, 4096).strip()
    finally:
        os.close(descriptor)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--secret-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = GateConfig.model_validate_json(args.config.read_text())
    auth = AppAuth(
        int(os.environ["PR_RERUN_APP_ID"]), Path(os.environ["PR_RERUN_APP_PRIVATE_KEY_FILE"])
    )
    app = create_app(config, args.database, read_secret(args.secret_file), auth)
    # Only this listener goes behind the HTTPS reverse proxy. The rerun API
    # continues to bind privately and is never mounted here.
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
