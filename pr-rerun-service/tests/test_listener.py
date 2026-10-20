import json
import socket
import subprocess
import sys

import pytest

from pr_rerun import api
from pr_rerun.listener import NetBirdBinding, command_output, discover_netbird, listeners

PEER = "100.64.0.10"


@pytest.fixture
def network(monkeypatch):
    state = {
        "peer": PEER,
        "interfaces": [
            {
                "ifname": "wt0",
                "flags": ["UP"],
                "addr_info": [{"local": PEER}],
            }
        ],
    }

    def output(arguments):
        if arguments == ["netbird", "status", "--ipv4"]:
            return state["peer"]
        assert arguments == ["ip", "-j", "-4", "address", "show"]
        return json.dumps(state["interfaces"])

    monkeypatch.setattr("pr_rerun.listener.command_output", output)
    return state


@pytest.mark.skipif(sys.platform != "linux", reason="NetBird listeners use Linux interfaces")
def test_netbird_is_discovered_and_explicit_host_is_verified(network):
    assert discover_netbird() == NetBirdBinding(PEER, "wt0")
    assert discover_netbird(PEER) == NetBirdBinding(PEER, "wt0")
    network["interfaces"][0]["ifname"] = "custom-vpn"
    assert discover_netbird().interface == "custom-vpn"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "", "127.0.0.1", "192.168.1.10", "8.8.8.8", "localhost", "100.64.0.11"],
)
def test_other_host_cannot_start_or_read_credentials(monkeypatch, network, host):
    monkeypatch.setattr("sys.argv", ["pr-rerun-server", "--host", host])
    monkeypatch.setattr(
        "pr_rerun.api.create_app", lambda: pytest.fail("Credentials must not be read")
    )
    with pytest.raises(SystemExit) as result:
        api.main()
    assert result.value.code == 2


@pytest.mark.parametrize(
    "peer", ["", "Disconnected", "0.0.0.0", "127.0.0.1", "224.0.0.1", "169.254.1.2"]
)
def test_missing_or_unusable_netbird_address_fails_closed(network, peer):
    network["peer"] = peer
    with pytest.raises(ValueError):
        discover_netbird()


@pytest.mark.parametrize(
    "problem", ["absent", "duplicate", "down", "loopback", "bad-name", "malformed"]
)
def test_interface_must_be_unique_and_up(network, problem):
    if problem == "absent":
        network["interfaces"] = []
    elif problem == "duplicate":
        network["interfaces"] *= 2
    elif problem == "down":
        network["interfaces"][0]["flags"] = []
    elif problem == "loopback":
        network["interfaces"][0]["flags"] = ["UP", "LOOPBACK"]
    elif problem == "bad-name":
        network["interfaces"][0]["ifname"] = ""
    else:
        network["interfaces"] = {"unexpected": "structure"}
    with pytest.raises(ValueError):
        discover_netbird()


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("no netbird"),
        subprocess.TimeoutExpired("netbird", 10),
        subprocess.CalledProcessError(1, "netbird", stderr="private daemon diagnostics"),
    ],
)
def test_failed_probe_does_not_fall_back_or_echo_daemon_output(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("subprocess.run", fail)
    with pytest.raises(ValueError, match="no listener was started") as raised:
        command_output(["netbird", "status", "--ipv4"])
    assert "private daemon diagnostics" not in str(raised.value)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket interface binding")
def test_real_sockets_are_bound_to_device_and_local_addresses():
    # A second loopback address stands in for the VPN address without using NetBird.
    with listeners(NetBirdBinding("127.0.0.2", "lo"), 0) as opened:
        assert len(opened) == 2
        assert [value.getsockname()[0] for value in opened] == ["127.0.0.2", "127.0.0.1"]
        assert opened[0].getsockname()[1] == opened[1].getsockname()[1]
        for value in opened:
            assert value.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 16) == b"lo\0"
            assert value.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    assert all(value.fileno() == -1 for value in opened)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket interface binding")
def test_interface_binding_failure_closes_all_sockets(monkeypatch):
    created = []
    socket_type = socket.socket

    def tracked_socket(*args, **kwargs):
        value = socket_type(*args, **kwargs)
        created.append(value)
        return value

    monkeypatch.setattr("pr_rerun.listener.socket.socket", tracked_socket)
    with pytest.raises(OSError):
        with listeners(NetBirdBinding("127.0.0.2", "no-such-iface"), 0):
            pytest.fail("Must not fall back to another listener")
    assert created
    assert all(value.fileno() == -1 for value in created)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket interface binding")
def test_loopback_port_conflict_closes_vpn_socket(monkeypatch):
    created = []
    socket_type = socket.socket
    with socket_type() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()

        def tracked_socket(*args, **kwargs):
            value = socket_type(*args, **kwargs)
            created.append(value)
            return value

        monkeypatch.setattr("pr_rerun.listener.socket.socket", tracked_socket)
        with pytest.raises(OSError):
            with listeners(NetBirdBinding("127.0.0.2", "lo"), occupied.getsockname()[1]):
                pytest.fail("Must not partially start")
        assert len(created) == 2
        assert all(value.fileno() == -1 for value in created)
