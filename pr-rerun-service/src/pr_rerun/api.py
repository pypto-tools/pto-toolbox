"""A small HTTP API. Production configuration is loaded only by the app factory."""

import argparse
import fcntl
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pr_rerun.github import GitHub
from pr_rerun.listener import discover_netbird, listeners
from pr_rerun.models import Principal, ServiceError, Settings, StrictModel
from pr_rerun.planner import Planner
from pr_rerun.service import Service
from pr_rerun.store import Store

BODY_LIMIT = 4096


class PreviewInput(StrictModel):
    pr: int = Field(gt=0, lt=2**31)


class ConfirmInput(StrictModel):
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    confirmation: str = Field(min_length=1, max_length=200)


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
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > BODY_LIMIT:
                    await JSONResponse(
                        {"error": "Request body exceeds 4096 bytes."}, status_code=413
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


def create_app(settings: Settings | None = None, github: GitHub | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    github = github or GitHub(settings.repository, settings.github_token.get_secret_value())
    service = Service(Planner(github, settings), store, settings)

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
            github.close()

    app = FastAPI(
        title="PR Rerun Service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestBoundary)
    app.state.service = service

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

    @app.post("/api/reruns/preview")
    def preview(body: PreviewInput, actor: Annotated[Principal, Depends(principal)]):
        return service.preview(actor, body.pr)

    @app.post("/api/reruns/confirm")
    async def confirm(body: ConfirmInput, actor: Annotated[Principal, Depends(principal)]):
        result = await run_in_threadpool(service.confirm, actor, body.ticket, body.confirmation)
        status = 202 if result["state"] in {"accepted", "submitting", "validating"} else 200
        return JSONResponse(result, status_code=status)

    @app.get("/api/reruns/{request_id}")
    def status(request_id: str, actor: Annotated[Principal, Depends(principal)]):
        return service.status(actor, request_id)

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
        "--github-auth", choices=("token", "gh"), help="Credential source (or PR_RERUN_GITHUB_AUTH)"
    )
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
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
            app.state.service.planner.github.close()
    except (ValueError, OSError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
