"""Small stdio-only MCP tools server. No HTTP listener or backend imports."""

import argparse
import json
import sys
from typing import BinaryIO

from pr_rerun.mcp_queries import INSTRUCTIONS, TOOLS, QueryError, query, validate_arguments
from pr_rerun.version import VERSION

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_MESSAGE_BYTES = 65536


def rpc_error(identifier, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


class Session:
    def __init__(self, repository: str | None = None):
        self.repository = repository
        self.protocol: str | None = None
        self.ready = False

    def handle(self, message: object) -> dict | None:
        if not isinstance(message, dict):
            return rpc_error(None, -32600, "Expected one JSON-RPC request, not a batch.")
        identifier = message.get("id")
        if (
            message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and type(identifier) not in (str, int))
        ):
            return rpc_error(None, -32600, "Invalid JSON-RPC request.")
        method = message["method"]
        params = message.get("params", {})
        if "id" not in message:
            if method == "notifications/initialized" and self.protocol and isinstance(params, dict):
                self.ready = True
            # Unknown notifications, including cancellation, never execute tools or get replies.
            return None
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, "Expected object parameters.")
        if method == "ping":
            result = {}
        elif method == "initialize":
            info = params.get("clientInfo")
            if (
                self.protocol is not None
                or not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(info, dict)
                or not isinstance(info.get("name"), str)
                or not isinstance(info.get("version"), str)
            ):
                return rpc_error(identifier, -32602, "Invalid or repeated initialization.")
            requested = params["protocolVersion"]
            self.protocol = requested if requested in PROTOCOLS else PROTOCOLS[0]
            result = {
                "protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pr-rerun-ci", "version": VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif not self.ready:
            return rpc_error(identifier, -32000, "Initialize the MCP session first.")
        elif method == "tools/list":
            if params.keys() - {"_meta"}:
                return rpc_error(identifier, -32602, "This fixed tool list has no cursor.")
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if (
                params.keys() - {"name", "arguments", "_meta"}
                or not isinstance(name, str)
                or name not in {tool["name"] for tool in TOOLS}
            ):
                return rpc_error(identifier, -32602, "Unknown tool or invalid call parameters.")
            try:
                arguments = validate_arguments(name, params.get("arguments", {}))
                data = query(name, arguments, self.repository)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(data, ensure_ascii=True, allow_nan=False),
                        }
                    ],
                    "isError": False,
                }
                if self.protocol in PROTOCOLS[:2]:
                    result["structuredContent"] = data
            except QueryError as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
            except Exception:
                # Do not print exception bodies: transport errors may embed credentials or URLs.
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": "CI query failed unexpectedly; check service health.",
                        }
                    ],
                    "isError": True,
                }
        else:
            return rpc_error(identifier, -32601, "Method not supported by this read-only MCP.")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def reject_constant(value: str):
    raise ValueError("Non-JSON numeric constant")


def serve(source: BinaryIO, target: BinaryIO, repository: str | None = None) -> int:
    session = Session(repository)
    while line := source.readline(MAX_MESSAGE_BYTES + 1):
        if len(line) > MAX_MESSAGE_BYTES:
            reply = rpc_error(None, -32700, "MCP message exceeds 64 KiB; closing session.")
            target.write(json.dumps(reply).encode() + b"\n")
            target.flush()
            return 2
        try:
            message = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            reply = rpc_error(None, -32700, "Invalid JSON message.")
        else:
            reply = session.handle(message)
        if reply is not None:
            target.write(json.dumps(reply, ensure_ascii=True, allow_nan=False).encode() + b"\n")
            target.flush()
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="pr-rerun mcp",
        description="Read-only CI tools over MCP stdio; uses this OS user's saved login.",
    )
    parser.add_argument(
        "--repo", help="Default repository or alias (otherwise saved login default)"
    )
    args = parser.parse_args(arguments)
    if args.repo is not None:
        try:
            validate_arguments("get_ci_stats", {"repository": args.repo})
        except QueryError as error:
            parser.error(str(error))
    try:
        return serve(sys.stdin.buffer, sys.stdout.buffer, args.repo)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except OSError:
        print("MCP stdio transport failed.", file=sys.stderr)
        return 2
