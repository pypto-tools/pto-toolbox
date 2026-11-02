"""A small HTTP API. Production configuration is loaded only by the app factory."""

import argparse
import fcntl
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pr_rerun.app_auth import AppAuth
from pr_rerun.github import GitHub
from pr_rerun.listener import discover_netbird, listeners
from pr_rerun.login import DeviceLogin
from pr_rerun.models import Principal, ServiceError, Settings, StrictModel
from pr_rerun.planner import Planner
from pr_rerun.policy import PolicyUpdate, RepositoryName
from pr_rerun.registry import Registry
from pr_rerun.service import Service
from pr_rerun.store import Store

BODY_LIMIT = 4096


class PreviewInput(StrictModel):
    repository: RepositoryName | None = None
    pr: int = Field(gt=0, lt=2**31)


class ConfirmInput(StrictModel):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    confirmation: str = Field(min_length=1, max_length=200)


class LoginPollInput(StrictModel):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class RequestBoundary:
    """Bound JSON bodies before parsing and keep all responses out of browser caches."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def safe_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        if scope["method"] == "POST":
            headers = dict(scope["headers"])
            if headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
                await JSONResponse({"error": "Expected application/json."}, status_code=415)(
                    scope, receive, safe_send
                )
                return
            limit = 65536 if scope["path"].startswith("/api/repositories/") else BODY_LIMIT
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    await JSONResponse(
                        {"error": f"Request body exceeds {limit} bytes."}, status_code=413
                    )(scope, receive, safe_send)
                    return
                if not message.get("more_body"):
                    break
            consumed = False
            upstream_receive = receive

            async def buffered_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await upstream_receive()

            receive = buffered_receive
        await self.app(scope, receive, safe_send)


def create_app(
    settings: Settings | None = None,
    github: GitHub | None = None,
    *,
    app_auth: AppAuth | None = None,
    login_transport=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    if settings.app_id is not None:
        if settings.app_key_file is None:
            raise ValueError("App private key file is required")
        service = Registry(
            settings, store, app_auth or AppAuth(settings.app_id, settings.app_key_file)
        )
        close = service.close
    else:
        github = github or GitHub(settings.repository, settings.github_token.get_secret_value())
        service = Service(Planner(github, settings), store, settings)
        close = github.close

    login = (
        DeviceLogin(
            settings.app_client_id,
            store,
            login_transport,
            proxy=settings.login_proxy.get_secret_value() if settings.login_proxy else None,
        )
        if settings.app_id and settings.app_client_id
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Recovery is safe only with one server process owning this database.
        # flock also rejects an accidental second deployment using the same file.
        descriptor = os.open(f"{settings.database}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another server is already using this database") from error
            store.recover()
            yield
        finally:
            os.close(descriptor)
            close()
            if login is not None:
                login.close()

    app = FastAPI(
        title="PR Rerun Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestBoundary)
    app.state.service = service
    app.state.close = close

    def principal(authorization: Annotated[str, Header()] = "") -> Principal:
        return store.authenticate(authorization)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, error: ServiceError):
        return JSONResponse({"error": str(error)}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        # FastAPI's default response echoes invalid input, which may contain tokens.
        return JSONResponse({"error": "Invalid request fields."}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        # Expose neither tokens nor credentialed URLs through chained exceptions.
        logging.getLogger("pr_rerun").error("Request failed (%s)", type(error).__name__)
        status = 503 if isinstance(error, sqlite3.Error) else 502
        return JSONResponse(
            {"error": "Request could not be completed. Inspect its status before retrying."},
            status_code=status,
        )

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/auth/config")
    def auth_config():
        return {"github_login": login is not None}

    @app.post("/api/auth/device/start")
    def login_start(request: Request):
        if login is None:
            raise ServiceError(503, "GitHub login is not configured on this server.")
        return login.start(request.client.host if request.client else "unknown")

    @app.post("/api/auth/device/poll")
    def login_poll(body: LoginPollInput):
        if login is None:
            raise ServiceError(503, "GitHub login is not configured on this server.")
        return login.poll(body.ticket)

    @app.post("/api/auth/logout")
    def logout(actor: Annotated[Principal, Depends(principal)]):
        store.logout_session(actor.token_hash)
        return {"logged_out": True}

    @app.post("/api/reruns/preview")
    def preview(body: PreviewInput, actor: Annotated[Principal, Depends(principal)]):
        if isinstance(service, Registry):
            return service.preview(actor, body.pr, body.repository)
        if body.repository is not None and body.repository.lower() != settings.repository.lower():
            raise ServiceError(403, "Repository is not configured on this server.")
        return service.preview(actor, body.pr)

    @app.post("/api/reruns/confirm")
    async def confirm(body: ConfirmInput, actor: Annotated[Principal, Depends(principal)]):
        result = await run_in_threadpool(
            lambda: service.confirm(actor, body.ticket, body.confirmation)
        )
        status = 202 if result["state"] in {"accepted", "submitting", "validating"} else 200
        return JSONResponse(result, status_code=status)

    @app.get("/api/reruns/{request_id}")
    def status(request_id: str, actor: Annotated[Principal, Depends(principal)]):
        return service.status(actor, request_id)

    def registry() -> Registry:
        if not isinstance(service, Registry):
            raise ServiceError(404, "Repository policy management requires App mode.")
        return service

    @app.get("/api/repository-aliases")
    def official_aliases(actor: Annotated[Principal, Depends(principal)]):
        return {"aliases": registry().policies.aliases(actor)}

    @app.get("/api/repositories")
    def repositories(actor: Annotated[Principal, Depends(principal)]):
        return registry().policies.repositories(actor)

    @app.get("/api/repository-aliases/{alias}")
    def resolve_alias(alias: str, actor: Annotated[Principal, Depends(principal)]):
        return {"repository": registry().policies.resolve_alias(alias, actor)}

    @app.get("/api/repositories/{owner}/{repo}/stats")
    def statistics(
        owner: str,
        repo: str,
        actor: Annotated[Principal, Depends(principal)],
        days: Annotated[int, Query(ge=1, le=30)] = 7,
        sort: Literal["failures", "failure-rate"] = "failures",
        event: Annotated[str | None, Query(max_length=100)] = None,
        branch: Annotated[str | None, Query(max_length=255)] = None,
        workflow: Annotated[str | None, Query(max_length=300)] = None,
        job: Annotated[str | None, Query(max_length=500)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ):
        from pr_rerun.stats import report, stats_path

        current = registry()
        record = current.policies.get(f"{owner}/{repo}")
        current.policies.authorize_analytics(record, actor)
        # Fail closed on installation revocation even for cached private metadata.
        current.auth.read_credentials(
            record["name"], record["repository_id"], record["installation_id"]
        )
        result = report(
            stats_path(settings.database, settings.stats_database),
            record,
            days=days,
            sort=sort,
            event=event,
            branch=branch,
            workflow=workflow,
            job=job,
            limit=limit,
        )
        store.authenticate_hash(actor.token_hash)
        current.policies.authorize_analytics(current.policies.get(record["name"]), actor)
        return result

    @app.get("/api/repositories/{owner}/{repo}/policy")
    def policy(owner: str, repo: str, actor: Annotated[Principal, Depends(principal)]):
        return registry().policy(f"{owner}/{repo}", actor)

    @app.post("/api/repositories/{owner}/{repo}/policy")
    def update_policy(
        owner: str, repo: str, body: PolicyUpdate, actor: Annotated[Principal, Depends(principal)]
    ):
        return registry().update_policy(f"{owner}/{repo}", body, actor)

    @app.get("/api/repositories/{owner}/{repo}/history")
    def history(owner: str, repo: str, actor: Annotated[Principal, Depends(principal)]):
        return {"history": registry().policies.history(f"{owner}/{repo}", actor)}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve confirmed, author-only GitHub Actions reruns"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--host", help="Expected local NetBird IPv4; auto-detected and verified by default"
    )
    mode.add_argument("--local-only", action="store_true", help="Listen on loopback only")
    parser.add_argument(
        "--github-auth",
        choices=("token", "gh", "app"),
        help="Credential source (or PR_RERUN_GITHUB_AUTH)",
    )
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # HTTP libraries can include signed artifact URLs in routine request logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        binding = None if args.local_only else discover_netbird(args.host)
        app = create_app(Settings.from_env(github_auth=args.github_auth))
        try:
            with listeners(binding, args.port) as sockets:
                for listener in sockets:
                    logging.getLogger("pr_rerun").info("Listening on %s", listener.getsockname())
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        workers=1,
                        access_log=False,
                        limit_concurrency=32,
                        timeout_keep_alive=5,
                        proxy_headers=False,
                    )
                )
                server.run(sockets=sockets)
                if not server.started:
                    raise ValueError("Server startup did not complete")
        finally:
            app.state.close()
    except (ValueError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
