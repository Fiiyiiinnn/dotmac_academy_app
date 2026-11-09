"""Hosted-only readiness regressions using fake sockets and a monotonic clock."""

import ipaddress
import json
import socket
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.config import LabExecutionPolicy, Settings
from app.services import lab_lifecycle, lab_operations
from app.services.labengine import containerlab
from app.services.labengine.interface import LabHandle
from tests.issue2_wave1_fixtures import (
    admin_engine,
    admin_session,
    seed_lab,
    tenant_a,
)

assert admin_engine and admin_session and tenant_a


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        assert duration >= 0
        self.now += duration


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(containerlab.time, "monotonic", value.monotonic)
    monkeypatch.setattr(containerlab.time, "sleep", value.sleep)
    return value


@pytest.fixture
def engine(tmp_path):
    policy = replace(
        LabExecutionPolicy.from_settings(Settings.model_construct()),
        lab_command_timeout_seconds_by_action={"inspect": 6, "deploy": 10, "destroy": 3, "check": 4},
    )
    return containerlab.ContainerlabEngine(str(tmp_path), execution_policy=policy, lab_host_role="lab")


class Socket:
    def __init__(self, clock, response=b"HTTP/1.1 200 OK\r\nbody", *, delay=0, cancel=False):
        self.clock, self.response, self.delay, self.cancel = clock, response, delay, cancel
        self.closed = False
        self.timeouts = []
        self.sent = b""
        self.address = None
        self.reads = 0

    def settimeout(self, value):
        assert 0 < value <= 2
        self.timeouts.append(value)

    def connect(self, address):
        self.address = address

    def sendall(self, value):
        self.sent = value

    def recv(self, length):
        assert length == 1  # Only the status line, never a body fetch.
        if self.cancel:
            raise KeyboardInterrupt
        self.reads += 1
        if self.delay > self.timeouts[-1]:
            self.clock.sleep(self.timeouts[-1])
            raise TimeoutError
        self.clock.sleep(self.delay)
        value, self.response = self.response[:1], self.response[1:]
        return value

    def close(self):
        self.closed = True


def install_socket(monkeypatch, connection):
    families = []

    def create(family, kind):
        assert kind == socket.SOCK_STREAM
        families.append(family)
        return connection

    monkeypatch.setattr(containerlab.socket, "socket", create)
    return families


@pytest.mark.parametrize("status", [100, 200, 302, 401, 499, 500, 999])
def test_probe_reads_only_accepted_status_line(monkeypatch, clock, status):
    connection = Socket(clock, f"HTTP/1.1 {status} Status\r\nsecret-body".encode())
    families = install_socket(monkeypatch, connection)
    assert containerlab._webfig_responding(ipaddress.ip_address("192.0.2.4"), 20) is (status < 500)
    assert connection.closed and connection.response == b"secret-body"
    assert connection.address == ("192.0.2.4", 80) and families == [socket.AF_INET]
    assert connection.sent == b"GET / HTTP/1.1\r\nHost: 192.0.2.4\r\nConnection: close\r\n\r\n"


def test_probe_trickle_cannot_restart_absolute_deadline(monkeypatch, clock):
    connection = Socket(clock, delay=0.2)
    install_socket(monkeypatch, connection)
    assert not containerlab._webfig_responding(ipaddress.ip_address("192.0.2.4"), 30)
    assert clock.now == pytest.approx(2) and connection.closed
    assert connection.timeouts[-1] < connection.timeouts[0]


def test_probe_shares_budget_across_connect_send_and_read(monkeypatch, clock):
    connection = Socket(clock, delay=0.2)

    def connect(address):
        clock.sleep(0.8)

    def send(value):
        assert connection.timeouts[-1] == pytest.approx(1.2)
        clock.sleep(0.8)

    connection.connect, connection.sendall = connect, send
    install_socket(monkeypatch, connection)
    assert not containerlab._webfig_responding(ipaddress.ip_address("192.0.2.4"), 30)
    assert clock.now == pytest.approx(2) and connection.closed


def test_probe_status_line_size_is_bounded(monkeypatch, clock):
    connection = Socket(clock, b"x" * 9000)
    install_socket(monkeypatch, connection)
    assert not containerlab._webfig_responding(ipaddress.ip_address("192.0.2.4"), 30)
    assert connection.closed and connection.reads == 8192


def test_ipv6_numeric_connection_and_host_header(monkeypatch, clock):
    connection = Socket(clock)
    families = install_socket(monkeypatch, connection)
    assert containerlab._webfig_responding(ipaddress.ip_address("2001:db8::4"), 20)
    assert families == [socket.AF_INET6]
    assert connection.address == ("2001:db8::4", 80)
    assert b"Host: [2001:db8::4]\r\n" in connection.sent


def test_probe_cancellation_propagates_after_cleanup(monkeypatch, clock):
    connection = Socket(clock, cancel=True)
    install_socket(monkeypatch, connection)
    with pytest.raises(KeyboardInterrupt):
        containerlab._webfig_responding(ipaddress.ip_address("192.0.2.4"), 20)
    assert connection.closed


@pytest.mark.parametrize("address", [None, "", "router.example.test", "192.0.2.4/not-an-ip", "fe80::1%eth0"])
def test_missing_or_nonnumeric_management_address_refuses_without_probe(monkeypatch, address):
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: pytest.fail("must not probe"))
    handle = LabHandle("i", {"r1": "clab-i-r1"}, {"r1": address}, {"r1": "vr-ros"})
    with pytest.raises(RuntimeError, match="numeric management address"):
        containerlab._wait_for_routeros(handle, 10)


def test_linux_bypasses_http_even_with_missing_management_address(monkeypatch):
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: pytest.fail("Linux must not probe"))
    containerlab._wait_for_routeros(LabHandle("i", {"n": "clab-i-n"}, {}, {"n": "linux"}), 0)


def test_all_router_nodes_share_action_deadline(monkeypatch, clock):
    calls = []

    def probe(address, deadline):
        calls.append((str(address), deadline))
        clock.sleep(min(1.5, deadline - clock.now))
        return False

    monkeypatch.setattr(containerlab, "_webfig_responding", probe)
    handle = LabHandle(
        "i", {"r1": "a", "r2": "b"}, {"r1": "192.0.2.1", "r2": "192.0.2.2"}, {"r1": "vr-ros", "r2": "vr-ros"}
    )
    with pytest.raises(RuntimeError, match="action deadline"):
        containerlab._wait_for_routeros(handle, 3)
    assert calls == [("192.0.2.1", 3), ("192.0.2.2", 3)] and clock.now == 3


def result(command, rows):
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(rows), stderr="")


def test_deploy_command_and_readiness_share_budget(engine, clock, monkeypatch):
    def run(command, *, timeout, grace_seconds):
        assert timeout == 10
        clock.sleep(9)
        return result(command, [{"name": "clab-i-r1", "kind": "vr-ros", "ipv4_address": "192.0.2.4/24"}])

    monkeypatch.setattr(containerlab, "_run_contained", run)
    connection = Socket(clock, delay=0.2)
    install_socket(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="action deadline"):
        engine.deploy("name: i\ntopology: {nodes: {}}", "i")
    assert clock.now == pytest.approx(10) and connection.closed


def test_inspect_readiness_stays_within_inspect_budget(engine, clock, monkeypatch):
    def run(command, *, timeout, grace_seconds):
        assert timeout == 6 and "inspect" in command
        clock.sleep(5)
        return result(
            command,
            [
                {
                    "lab_name": "i",
                    "absLabPath": engine._topo_path("i"),
                    "name": "clab-i-r1",
                    "kind": "vr-ros",
                    "ipv4_address": "192.0.2.4/24",
                }
            ],
        )

    monkeypatch.setattr(containerlab, "_run_contained", run)
    connection = Socket(clock, delay=0.2)
    install_socket(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="action deadline"):
        engine.inspect_running("i")
    assert clock.now == pytest.approx(6) and connection.closed


@pytest.mark.parametrize("owned", [True, False])
def test_inspect_probes_only_owned_handle(engine, clock, monkeypatch, owned):
    rows = [
        {
            "lab_name": "i",
            "absLabPath": engine._topo_path("i") if owned else "/other/topo.clab.yml",
            "name": "clab-i-r1",
            "kind": "vr-ros",
            "ipv6_address": "2001:db8::4/64",
        }
    ]
    monkeypatch.setattr(containerlab, "_run_contained", lambda command, **kwargs: result(command, rows))
    calls = []
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda address, deadline: calls.append(address) or True)
    handle = engine.inspect_running("i")
    assert (handle is not None) is owned
    assert len(calls) == int(owned)
    if owned:
        assert handle.mgmt["r1"] == "2001:db8::4"


def test_status_and_inventory_are_presence_only(engine, monkeypatch):
    rows = [{"lab_name": "i", "absLabPath": engine._topo_path("i"), "name": "clab-i-r1", "kind": "vr-ros"}]
    monkeypatch.setattr(containerlab, "_run_contained", lambda command, **kwargs: result(command, rows))
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: pytest.fail("presence must not probe"))
    assert engine.status("i") == "running" and "i" in engine.inventory()


def instance():
    return SimpleNamespace(
        id=uuid4(),
        instance_name="i",
        seed={},
        status="provisioning",
        error=None,
        runtime_presence="unknown",
        consoles={"prior": {"kind": "linux", "port": 9000}},
    )


def test_readiness_failure_never_activates_healthy_or_destroys_runtime(engine, clock, monkeypatch):
    rows = [{"name": "clab-i-r1", "kind": "vr-ros", "ipv4_address": "192.0.2.4/24"}]
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return result(command, rows)

    monkeypatch.setattr(containerlab, "_run_contained", run)
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: False)
    monkeypatch.setattr(lab_lifecycle, "start_console", lambda *args: pytest.fail("must not start consoles"))
    state = instance()
    template = SimpleNamespace(topology="name: i\ntopology: {nodes: {}}")
    lab_lifecycle.provision(MagicMock(), state, engine, template)
    assert state.status == "active" and state.error and state.runtime_presence == "unknown"
    assert state.consoles == {"prior": {"kind": "linux", "port": 9000}}
    assert len(commands) == 1 and "deploy" in commands[0]


@pytest.mark.parametrize("path", ["preliminary", "conditional-race"])
def test_inspect_readiness_failure_preserves_existing_consoles(engine, clock, monkeypatch, path):
    rows = [
        {
            "lab_name": "i",
            "absLabPath": engine._topo_path("i"),
            "name": "clab-i-r1",
            "kind": "vr-ros",
            "ipv4_address": "192.0.2.4/24",
        }
    ]
    monkeypatch.setattr(containerlab, "_run_contained", lambda command, **kwargs: result(command, rows))
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: False)
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda *args: pytest.fail("must not tear down consoles"))
    state = instance()
    with pytest.raises(RuntimeError, match="action deadline"):
        if path == "preliminary":
            lab_lifecycle.resync_present_preliminary(MagicMock(), state, engine)
        else:
            lab_lifecycle.provision_if_absent(
                MagicMock(),
                state,
                engine,
                SimpleNamespace(topology="name: i\ntopology: {nodes: {}}"),
                preserved_status="active",
                preserved_error=None,
            )
    assert state.runtime_presence == "unknown" and state.error is None
    assert state.consoles == {"prior": {"kind": "linux", "port": 9000}}


def test_run_claimed_conditional_readiness_failure_retains_capacity_and_consoles(
    admin_session, tenant_a, engine, clock, monkeypatch
):
    """Real worker fencing/settlement preserves the conditional failure mapping."""
    policy = replace(engine.execution_policy, max_concurrent_labs=1, max_concurrent_labs_per_tenant=1)
    engine = containerlab.ContainerlabEngine(engine.workdir, execution_policy=policy, lab_host_role="lab")
    state, _ = seed_lab(admin_session, tenant_a.id, name="readiness-worker", status="queued", presence="absent")
    prior_consoles = {"prior": {"kind": "linux", "mgmt": "192.0.2.8", "port": 9000}}
    state.consoles = prior_consoles
    operation = lab_operations.enqueue(
        admin_session,
        instance=state,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    operation_id = operation.id
    admin_session.commit()
    claimed = lab_operations.claim_next(admin_session, claimed_by="readiness-worker", policy=policy)
    assert claimed is not None and claimed.id == operation_id
    admin_session.commit()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "inspect" in command:
            return result(command, [])
        assert "deploy" in command
        return result(
            command, [{"name": f"clab-{state.instance_name}-r1", "kind": "vr-ros", "ipv4_address": "192.0.2.4/24"}]
        )

    monkeypatch.setattr(containerlab, "_run_contained", run)
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: False)
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda *args: pytest.fail("must not clear existing consoles"))
    monkeypatch.setattr(lab_lifecycle, "start_console", lambda *args: pytest.fail("must not start consoles"))
    outcome = lab_operations.run_claimed(
        admin_session, operation_id=operation_id, claimed_by="readiness-worker", engine=engine, policy=policy
    )
    admin_session.refresh(state)
    admin_session.refresh(operation)
    assert outcome == "failed" and operation.state == "failed"
    assert "action deadline" in operation.last_error
    assert state.status == "error" and "action deadline" in state.error
    assert state.runtime_presence == "unknown" and state.consoles == prior_consoles
    assert sum("inspect" in command for command in commands) == 2
    assert sum("deploy" in command for command in commands) == 1
    assert all("destroy" not in command for command in commands)
    assert clock.now == 10

    # The failed newly-created runtime occupies the sole slot through genuine
    # admission logic; a second absent instance cannot treat that slot as free.
    contender, _ = seed_lab(admin_session, tenant_a.id, name="readiness-contender", presence="absent")
    admin_session.commit()
    assert not lab_operations._capacity_available(admin_session, contender, policy=policy)
