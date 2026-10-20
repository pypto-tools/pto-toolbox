"""Restrict remote listening to the local NetBird address and kernel interface."""

import ipaddress
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class NetBirdBinding:
    address: str
    interface: str


def command_output(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        # Do not expose daemon output or fall back to an unrestricted listener.
        raise ValueError(f"Cannot inspect {arguments[0]}; no listener was started") from error
    return result.stdout.strip()


def discover_netbird(requested_host: str | None = None) -> NetBirdBinding:
    if sys.platform != "linux" or not hasattr(socket, "SO_BINDTODEVICE"):
        raise ValueError(
            "NetBird serving requires Linux interface binding; use --local-only locally"
        )
    address_text = command_output(["netbird", "status", "--ipv4"])
    try:
        address = ipaddress.IPv4Address(address_text)
    except ValueError as error:
        raise ValueError("NetBird did not report a valid IPv4 address") from error
    if (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
    ):
        raise ValueError("NetBird did not report a usable peer IPv4 address")
    if requested_host is not None and requested_host != str(address):
        raise ValueError("--host must exactly match this machine's NetBird IPv4 address")
    try:
        interfaces = json.loads(command_output(["ip", "-j", "-4", "address", "show"]))
        matches = [
            entry
            for entry in interfaces
            if any(item.get("local") == str(address) for item in entry.get("addr_info", []))
        ]
        if len(matches) != 1:
            raise ValueError("NetBird IP must belong to exactly one local interface")
        entry = matches[0]
        interface = entry["ifname"]
        if (
            not isinstance(interface, str)
            or not interface
            or len(interface.encode()) >= 16
            or "\0" in interface
            or "LOOPBACK" in entry["flags"]
            or "UP" not in entry["flags"]
        ):
            raise ValueError("NetBird interface is unavailable or down")
    except (TypeError, KeyError, AttributeError) as error:
        raise ValueError("Cannot verify the local NetBird interface") from error
    return NetBirdBinding(str(address), interface)


@contextmanager
def listeners(binding: NetBirdBinding | None, port: int) -> Iterator[list[socket.socket]]:
    """Bind before serving; any failure closes every socket without a fallback."""
    opened: list[socket.socket] = []
    try:
        endpoints = [(binding.address, binding.interface)] if binding else []
        # A separate loopback listener gives this server's users a stable local endpoint.
        endpoints.append(("127.0.0.1", "lo" if sys.platform == "linux" else None))
        for address, interface in endpoints:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            opened.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if interface is not None:
                listener.setsockopt(
                    socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
                )
            listener.bind((address, port))
            # Port zero is for integration tests; both listeners use the selected port.
            port = listener.getsockname()[1]
            listener.setblocking(False)
        # Every address and interface is validated and bound before accepting traffic.
        for listener in opened:
            listener.listen(128)
        yield opened
    finally:
        for listener in opened:
            listener.close()
