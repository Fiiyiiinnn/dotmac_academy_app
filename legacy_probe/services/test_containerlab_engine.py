import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.cli import _sigterm_raises_shutdown_requested, _WorkerShutdownRequested
from app.config import LabExecutionPolicy, settings
from app.services.labengine import containerlab
from app.services.labengine.containerlab import ContainerlabEngine
from app.services.labengine.interface import ExecResult, LabHandle, WrongLabHostError


def _fake_popen(returncode: int = 0, stdout: str = "", stderr: str = "", pid: int = 4321):
    """A Popen-shaped double: ``.communicate()`` then ``.returncode``.

    Mirrors the real ``Popen`` contract that ``_run_contained`` relies on —
    ``communicate()`` returns the (stdout, stderr) pair, and ``.returncode``
    is only meaningful (and read) after that call.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


def test_exec_runs_docker_exec(
    policy,
):
    eng = ContainerlabEngine(
        workdir="/tmp/labs",
        lab_host_role="lab",
        execution_policy=policy,
    )
    handle = MagicMock(nodes={"r1": "clab-dal-u1-linux-1-r1"})
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="ok\n")
        res = eng.exec(handle, "r1", ["echo", "ok"])
        assert isinstance(res, ExecResult)
        assert res.stdout.strip() == "ok"
        assert res.exit_code == 0
        args = popen.call_args[0][0]
        assert args == ["docker", "exec", "clab-dal-u1-linux-1-r1", "echo", "ok"]
        assert args[:3] == ["docker", "exec", "clab-dal-u1-linux-1-r1"]
        assert popen.call_args.kwargs["start_new_session"] is True
        assert popen.return_value.communicate.call_args.kwargs["timeout"] == 480


def test_external_command_timeouts_are_strictly_below_their_leases(
    policy,
):
    assert policy.lab_command_timeout_seconds_by_action["deploy"] < policy.lab_operation_lease_seconds_by_kind["deploy"]
    assert (
        policy.lab_command_timeout_seconds_by_action["destroy"] < policy.lab_operation_lease_seconds_by_kind["destroy"]
    )
    assert policy.lab_command_timeout_seconds_by_action["check"] < policy.lab_operation_lease_seconds_by_kind["check"]


def test_deploy_writes_topology_and_calls_containerlab(
    tmp_path,
    monkeypatch,
    policy,
):
    monkeypatch.setattr(containerlab, "_webfig_responding", lambda *args: True)
    real_exists = os.path.exists
    monkeypatch.setattr(
        "app.services.labengine.containerlab.os.path.exists",
        lambda path: True if path == "/dev/kvm" else real_exists(path),
    )
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout='[{"name":"clab-i-r1","ipv4_address":"172.20.20.3/24","kind":"vr-ros"}]',
        )
        h = eng.deploy(
            "name: x\ntopology:\n  nodes:\n    r1: {kind: vr-ros, image: vrnetlab/vr-routeros}",
            "i",
        )
        assert h.nodes["r1"].endswith("-r1")
        assert h.mgmt["r1"] == "172.20.20.3"
        assert h.kinds["r1"] == "vr-ros"


def test_ssh_exec_uses_mgmt_ip(
    policy,
):
    eng = ContainerlabEngine(
        workdir="/tmp/labs",
        lab_host_role="lab",
        execution_policy=policy,
    )
    handle = LabHandle(
        instance_name="i",
        nodes={"r1": "c"},
        mgmt={"r1": "172.20.20.3"},
        kinds={"r1": "vr-ros"},
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="address\n")
        res = eng.ssh_exec(handle, "r1", "/ip address print", user="admin", password="pw")
        assert res.exit_code == 0
        assert "admin@172.20.20.3" in popen.call_args[0][0]


@pytest.mark.parametrize(
    "method", ["inventory", "deploy", "ssh_exec", "destroy", "reset", "exec", "status", "inspect_running"]
)
def test_operations_refuse_on_web_host_without_side_effects(
    tmp_path,
    method,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="web",
        execution_policy=policy,
    )
    handle = LabHandle(instance_name="i", nodes={"r1": "c"}, mgmt={"r1": "127.0.0.1"}, kinds={"r1": "linux"})
    with patch("subprocess.Popen") as popen:
        with pytest.raises(WrongLabHostError):
            {
                "inventory": eng.inventory,
                "deploy": lambda: eng.deploy("name: x", "i"),
                "ssh_exec": lambda: eng.ssh_exec(handle, "r1", "true"),
                "destroy": lambda: eng.destroy("i"),
                "reset": lambda: eng.reset("name: x", "i"),
                "exec": lambda: eng.exec(handle, "r1", ["true"]),
                "status": lambda: eng.status("i"),
                "inspect_running": lambda: eng.inspect_running("i"),
            }[method]()
        popen.assert_not_called()
    assert not (tmp_path / "i").exists()


def test_destroy_raises_when_containerlab_fails(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    topo = tmp_path / "i" / "topo.clab.yml"
    topo.parent.mkdir()
    topo.write_text("name: i")
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(returncode=1, stderr="permission denied")
        with pytest.raises(RuntimeError, match="destroy failed: permission denied"):
            eng.destroy("i")


def test_destroy_treats_an_absent_topology_as_already_destroyed(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="{}")
        eng.destroy("missing")
    popen.assert_called_once_with(
        ["sudo", "-n", "containerlab", "inspect", "--all", "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=containerlab._unblock_worker_signals_in_child,
    )
    assert not (tmp_path / "missing").exists()


def test_destroy_refuses_when_the_inspected_path_does_not_match_the_expected_topology(
    tmp_path,
    policy,
):
    """An exact-name collision must not get destroyed at an unverified path.

    ``inspect`` reporting a different path for this exact instance_name would
    be anomalous for a lab this engine actually deployed (it always writes to
    ``self._topo_path(instance_name)``) — but nothing ruled that out before,
    so a same-named lab from elsewhere would have been destroyed wherever
    ``inspect`` said it lived. It must be refused instead.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=('[{"lab_name":"missing","absLabPath":"/srv/labs/missing.clab.yml",' '"state":"running"}]'),
        )
        with pytest.raises(RuntimeError, match="destroy refused"):
            eng.destroy("missing")
    popen.assert_called_once()  # only the inspect call; never proceeds to destroy


def test_destroy_uses_the_inspected_path_when_it_matches_the_expected_topology(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    expected_path = str(tmp_path / "missing" / "topo.clab.yml")
    with patch("subprocess.Popen") as popen:
        popen.side_effect = [
            _fake_popen(
                stdout=(f'[{{"lab_name":"missing","absLabPath":"{expected_path}",' f'"state":"running"}}]'),
            ),
            _fake_popen(stdout=""),
        ]
        eng.destroy("missing")
    assert popen.call_count == 2
    assert popen.call_args_list[1].args[0] == [
        "sudo",
        "-n",
        "containerlab",
        "destroy",
        "-t",
        expected_path,
        "--cleanup",
    ]
    assert not (tmp_path / "missing").exists()


def _counting_host_lock(monkeypatch):
    """Wrap the real ``host_lock`` context manager with a call counter,
    delegating to the genuine implementation (a real, tmp_path-scoped flock)
    so the underlying mutual-exclusion behavior is unaffected — only the
    number of times a caller *enters* the context manager is observed.
    """
    calls: list[None] = []
    real_host_lock = containerlab.host_lock

    @contextmanager
    def _wrapped(label, *, directory=None):
        calls.append(None)
        with real_host_lock(label, directory=directory):
            yield

    monkeypatch.setattr(containerlab, "host_lock", _wrapped)
    return calls


def test_deploy_if_absent_acquires_host_lock_exactly_once_when_deploying(
    tmp_path,
    monkeypatch,
    policy,
):
    """Observation + mutation happen inside exactly ONE host_lock acquisition
    — not two separate ones the way composing the public inventory()+deploy()
    would (each of those independently acquires and releases its own lock)."""
    calls = _counting_host_lock(monkeypatch)
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.side_effect = [
            _fake_popen(stdout="{}"),  # inspect --all: nothing running (absent)
            _fake_popen(
                stdout='[{"name":"clab-i-r1","ipv4_address":"172.20.20.3/24","kind":"linux"}]',
            ),  # the actual deploy
        ]
        handle = eng.deploy_if_absent("name: x", "i")
    assert handle is not None
    assert handle.nodes["r1"].endswith("-r1")
    assert popen.call_count == 2  # inspect, then deploy — both inside the one lock
    assert len(calls) == 1


def test_deploy_if_absent_acquires_host_lock_exactly_once_and_noops_when_present(
    tmp_path,
    monkeypatch,
    policy,
):
    calls = _counting_host_lock(monkeypatch)
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=f'[{{"lab_name":"i","absLabPath":"{eng._topo_path("i")}"}}]',
        )
        handle = eng.deploy_if_absent("name: x", "i")
    assert handle is None  # precondition mismatch: genuinely present already
    popen.assert_called_once()  # only the inspect — no deploy call at all
    assert len(calls) == 1
    assert not (tmp_path / "i").exists()  # no topology file written on the no-op path


def test_destroy_if_present_acquires_host_lock_exactly_once_when_destroying(
    tmp_path,
    monkeypatch,
    policy,
):
    calls = _counting_host_lock(monkeypatch)
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    topo = tmp_path / "i" / "topo.clab.yml"
    topo.parent.mkdir()
    topo.write_text("name: i")
    with patch("subprocess.Popen") as popen:
        popen.side_effect = [
            _fake_popen(
                stdout=f'[{{"lab_name":"i","absLabPath":"{eng._topo_path("i")}"}}]',
            ),  # inspect --all: present
            _fake_popen(stdout=""),  # the actual destroy
        ]
        destroyed = eng.destroy_if_present("i")
    assert destroyed is True
    assert popen.call_count == 2  # inspect, then destroy — both inside the one lock
    assert len(calls) == 1


def test_destroy_if_present_acquires_host_lock_exactly_once_and_noops_when_absent(
    tmp_path,
    monkeypatch,
    policy,
):
    calls = _counting_host_lock(monkeypatch)
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="{}")
        destroyed = eng.destroy_if_present("i")
    assert destroyed is False  # precondition mismatch: already absent
    popen.assert_called_once()  # only the inspect — no destroy call at all
    assert len(calls) == 1


def test_inspect_running_reconstructs_a_handle_for_an_already_running_instance(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    expected_path = eng._topo_path("i")
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=(
                f'[{{"lab_name":"i","name":"clab-i-r1","absLabPath":"{expected_path}",'
                '"ipv4_address":"172.20.20.5/24","kind":"linux"}]'
            ),
        )
        handle = eng.inspect_running("i")
    assert handle is not None
    assert handle.nodes["r1"] == "clab-i-r1"
    assert handle.mgmt["r1"] == "172.20.20.5"
    assert handle.kinds["r1"] == "linux"
    popen.assert_called_once()  # only the inspect — never a redeploy


def test_inspect_running_returns_none_when_not_actually_running(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="{}")
        handle = eng.inspect_running("i")
    assert handle is None


def test_inspect_running_refuses_a_lab_name_match_at_the_wrong_topology_path(
    tmp_path,
    policy,
):
    """Mirrors ``_destroy_unlocked``'s own ownership-verification test
    (``test_destroy_refuses_when_the_inspected_path_does_not_match_the_expected_topology``):
    an exact-name collision from outside Academy's own workdir must not
    contribute untrusted node/mgmt data to a resync — this is a read-only
    helper, so it returns ``None`` (behaves as "not found") rather than
    raising, unlike the destructive ``_destroy_unlocked`` path.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=(
                '[{"lab_name":"i","name":"clab-i-r1",'
                '"absLabPath":"/some/other/operators/i.clab.yml",'
                '"ipv4_address":"172.20.20.5/24","kind":"linux"}]'
            ),
        )
        handle = eng.inspect_running("i")
    assert handle is None


def test_inspect_running_never_merges_nodes_across_a_same_named_path_collision(
    tmp_path,
    policy,
):
    """A same-``lab_name`` collision reported at an unowned path must never
    contribute even ONE node to the accepted result, regardless of which
    record ``containerlab inspect --all`` happened to report last.

    Regression test for a bug where per-node data was accumulated into flat
    ``nodes``/``mgmt``/``kinds`` dicts for EVERY record matching ``lab_name``,
    while only a single, separately-tracked ``discovered_path`` variable was
    checked against the expected path at the end — so an untrusted record's
    node could survive in the accepted result even though the final path
    check "passed" because a later, legitimately-pathed record was also
    present. The fix groups discovered node data by the path it was reported
    under and only ever reads from the one group whose path matches exactly.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    expected_path = eng._topo_path("i")
    good_record = (
        '{"lab_name":"i","name":"clab-i-r1","absLabPath":"' + expected_path + '",'
        '"ipv4_address":"172.20.20.5/24","kind":"linux"}'
    )
    bad_record = (
        '{"lab_name":"i","name":"clab-i-r2",'
        '"absLabPath":"/some/other/operators/i.clab.yml",'
        '"ipv4_address":"172.20.20.9/24","kind":"linux"}'
    )

    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout=f"[{good_record},{bad_record}]")
        handle = eng.inspect_running("i")
    assert handle is not None
    assert handle.nodes == {"r1": "clab-i-r1"}
    assert "r2" not in handle.nodes
    assert "r2" not in handle.mgmt
    assert "r2" not in handle.kinds

    # Reversed order — the original bug's exact manifestation depended on
    # which record was walked LAST, since only a single mutable
    # ``discovered_path`` variable was checked at the end.
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout=f"[{bad_record},{good_record}]")
        handle = eng.inspect_running("i")
    assert handle is not None
    assert handle.nodes == {"r1": "clab-i-r1"}
    assert "r2" not in handle.nodes
    assert "r2" not in handle.mgmt
    assert "r2" not in handle.kinds


def test_inventory_maps_lab_names_to_inspected_topology_paths(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    one_path = str(tmp_path / "dal-one" / "topo.clab.yml")
    two_path = str(tmp_path / "dal-two" / "topo.clab.yml")
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=(
                f'[{{"lab_name":"dal-one","absLabPath":"{one_path}"}},'
                f'{{"lab_name":"dal-two","labPath":"{two_path}"}}]'
            ),
        )
        assert eng.inventory() == {
            "dal-one": one_path,
            "dal-two": two_path,
        }


def test_inventory_excludes_a_lab_whose_path_is_outside_this_engines_workdir(
    tmp_path,
    policy,
):
    """A dal-*-named lab is not automatically Academy's — only its real path is proof.

    An unrelated containerlab lab on the same host could coincidentally share
    the ``dal-`` naming convention (an operator's own test, or plain
    coincidence). ``inventory()`` must exclude it rather than let
    ``reconcile_runtime`` treat it as a rowless orphan eligible for destroy.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(
            stdout=('[{"lab_name":"dal-imposter",' '"absLabPath":"/some/other/operators/lab.clab.yml"}]'),
        )
        assert eng.inventory() == {}


def test_inventory_includes_the_owned_path_despite_a_same_named_collision_at_an_unowned_path(
    tmp_path,
    policy,
):
    """Regression test for the Finding-3 last-write-wins bug: ``inventory()``
    grouped discovered paths by a single mutable "last path" variable, so an
    unowned same-named record walked AFTER the legitimately-owned one would
    silently overwrite it, excluding a genuinely deployed, owned lab from the
    result entirely. The fix groups ALL distinct paths per ``lab_name`` into
    a set and checks membership, so walk order can no longer matter.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    owned_path = eng._topo_path("i")
    owned_record = f'{{"lab_name":"i","absLabPath":"{owned_path}"}}'
    unowned_record = '{"lab_name":"i","absLabPath":"/some/other/operators/i.clab.yml"}'

    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout=f"[{owned_record},{unowned_record}]")
        assert eng.inventory() == {"i": owned_path}

    # Reversed order — the original bug's exact manifestation depended on
    # which record was walked LAST.
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout=f"[{unowned_record},{owned_record}]")
        assert eng.inventory() == {"i": owned_path}


def test_destroy_uses_the_owned_path_despite_a_same_named_collision_at_an_unowned_path(
    tmp_path,
    policy,
):
    """Mirrors the inventory regression test above for ``destroy()``: a
    same-named unowned collision walked LAST must not make destroy wrongly
    refuse a genuinely owned, destroyable lab.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    owned_path = eng._topo_path("i")
    # No topology file on disk (mirrors ``test_destroy_uses_the_inspected_
    # path_when_it_matches_the_expected_topology``'s own setup): this forces
    # ``_destroy_unlocked`` down the "confirm via inspection" branch rather
    # than trusting a locally-present file.
    owned_record = f'{{"lab_name":"i","absLabPath":"{owned_path}"}}'
    unowned_record = '{"lab_name":"i","absLabPath":"/some/other/operators/i.clab.yml"}'

    with patch("subprocess.Popen") as popen:
        popen.side_effect = [
            _fake_popen(stdout=f"[{owned_record},{unowned_record}]"),
            _fake_popen(stdout=""),
        ]
        eng.destroy("i")
    assert popen.call_count == 2
    assert popen.call_args_list[1].args[0] == [
        "sudo",
        "-n",
        "containerlab",
        "destroy",
        "-t",
        owned_path,
        "--cleanup",
    ]

    # Reversed order.
    with patch("subprocess.Popen") as popen:
        popen.side_effect = [
            _fake_popen(stdout=f"[{unowned_record},{owned_record}]"),
            _fake_popen(stdout=""),
        ]
        eng.destroy("i")
    assert popen.call_count == 2
    assert popen.call_args_list[1].args[0] == [
        "sudo",
        "-n",
        "containerlab",
        "destroy",
        "-t",
        owned_path,
        "--cleanup",
    ]


def test_destroy_does_not_hide_an_inspection_failure(
    tmp_path,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(returncode=1, stderr="runtime unavailable")
        with pytest.raises(RuntimeError, match="inspect failed: runtime unavailable"):
            eng.destroy("missing")


@pytest.mark.parametrize(
    "topology",
    [
        "nodes:\n  r1: {kind: vr-ros}",
        'topology:\n  nodes:\n    r1:\n      kind: "vr-ros"',
        "topology:\n  defaults:\n    kind: vr-ros\n  nodes:\n    r1: {}",
    ],
)
def test_vr_deploy_refuses_without_kvm_before_writing_or_shelling_out(
    tmp_path,
    monkeypatch,
    topology,
    policy,
):
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    monkeypatch.setattr("app.services.labengine.containerlab.os.path.exists", lambda _: False)
    with patch("subprocess.Popen") as popen, pytest.raises(RuntimeError, match="requires /dev/kvm"):
        eng.deploy(topology, "i")
    popen.assert_not_called()
    assert not (tmp_path / "i").exists()


@pytest.mark.parametrize(
    "topology",
    [
        "nodes:\n  r1: {kind: vr-ros}",
        'topology:\n  nodes:\n    r1:\n      kind: "vr-ros"',
        "topology:\n  defaults:\n    kind: vr-ros\n  nodes:\n    r1: {}",
    ],
)
def test_vr_reset_refuses_the_redeploy_half_without_kvm(
    tmp_path,
    monkeypatch,
    topology,
    policy,
):
    """reset() calls the unlocked deploy helper directly (to avoid a nested
    lock acquisition) rather than the public deploy() wrapper. The KVM
    fail-closed guard must still apply to it, not only to deploy() — a
    regression this once was, since it lives in ``_deploy_unlocked`` now,
    not only in the ``deploy()`` wrapper.

    destroy() running first (and its own file-missing fallback inspect
    subprocess call) is existing, unchanged behavior — only the doomed
    deploy attempt itself must never be reached.
    """
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    monkeypatch.setattr("app.services.labengine.containerlab.os.path.exists", lambda _: False)
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="{}")
        with pytest.raises(RuntimeError, match="requires /dev/kvm"):
            eng.reset(topology, "i")
        deploy_calls = [c for c in popen.call_args_list if "deploy" in c.args[0]]
        assert deploy_calls == []
    assert not (tmp_path / "i").exists()


# --- Stage 1 process-containment: _run_contained / _terminate_process_group ---


@pytest.mark.parametrize(
    "call_site",
    [
        lambda eng: eng.inventory(),
        lambda eng: eng.deploy("name: x", "i"),
        lambda eng: eng.destroy("i"),
        lambda eng: eng.exec(
            LabHandle(instance_name="i", nodes={"r1": "c"}, mgmt={"r1": "1.2.3.4"}, kinds={"r1": "linux"}),
            "r1",
            ["true"],
        ),
        lambda eng: eng.ssh_exec(
            LabHandle(instance_name="i", nodes={"r1": "c"}, mgmt={"r1": "1.2.3.4"}, kinds={"r1": "linux"}),
            "r1",
            "true",
        ),
    ],
)
def test_every_call_site_runs_in_its_own_session(
    tmp_path,
    call_site,
    policy,
):
    """Every one of the five call sites goes through _run_contained with
    start_new_session=True — the property os.killpg relies on."""
    eng = ContainerlabEngine(
        workdir=str(tmp_path),
        lab_host_role="lab",
        execution_policy=policy,
    )
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="[]")
        call_site(eng)
        assert popen.called
        for call in popen.call_args_list:
            assert call.kwargs["start_new_session"] is True
            assert call.kwargs["stdout"] == subprocess.PIPE
            assert call.kwargs["stderr"] == subprocess.PIPE
            assert call.kwargs["text"] is True


def _spawn_grandchild_tree(marker_dir: str) -> list[str]:
    """A command whose direct child immediately forks a grandchild, both of
    which sleep far longer than any test timeout and write a pidfile so the
    test can check whether they are still alive after cleanup.
    """
    script = f"""
import os, sys, time
open({os.path.join(marker_dir, "child.pid")!r}, "w").write(str(os.getpid()))
pid = os.fork()
if pid == 0:
    open({os.path.join(marker_dir, "grandchild.pid")!r}, "w").write(str(os.getpid()))
    time.sleep(60)
    sys.exit(0)
time.sleep(60)
"""
    return [sys.executable, "-c", script]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    else:
        return True


def _spy_on_real_popen(monkeypatch) -> list[subprocess.Popen]:
    """Let ``_run_contained`` spawn a genuinely real OS process (through the
    real, unmocked ``subprocess.Popen``) while also capturing a handle to it
    for the test to inspect afterwards — proving OS-level signal delivery
    against a real process, not a ``MagicMock`` standing in for one.
    """
    created: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def _spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _spy)
    return created


def test_timeout_terminates_a_real_spawned_child_process(
    monkeypatch,
    policy,
):
    """A real (non-mock) child process is actually gone after a timeout."""
    created = _spy_on_real_popen(monkeypatch)
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=0.2,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    proc = created[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(proc.pid):
            time.sleep(0.05)
        assert not _pid_alive(proc.pid), "child process survived timeout teardown"
    finally:
        if _pid_alive(proc.pid):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_timeout_terminates_an_orphaned_grandchild_too(
    tmp_path,
    monkeypatch,
    policy,
):
    """The core process-group property: a grandchild the direct child forked
    must be gone too, not just the direct child — proving the teardown
    signals the whole group (os.killpg), not just proc itself.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)
    cmd = _spawn_grandchild_tree(str(marker))
    # Timeout is well above the near-instant fork(), so the grandchild is
    # already alive by the time _run_contained's communicate() times out.
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=1.0,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), "grandchild outlived process-group teardown"
    finally:
        if _pid_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def _spawn_grandchild_that_ignores_sigterm(marker_dir: str) -> list[str]:
    """A command whose direct child dies promptly on SIGTERM (default
    handling) but whose forked grandchild explicitly ignores SIGTERM before
    sleeping — the one scenario ``test_timeout_terminates_an_orphaned_grandchild_too``
    cannot exercise, since there both processes die together from the same
    signal. This proves the SIGKILL escalation is not skipped just because
    the direct child's own ``proc.wait()`` already succeeded.
    """
    script = f"""
import os, signal, sys, time
open({os.path.join(marker_dir, "child.pid")!r}, "w").write(str(os.getpid()))
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open({os.path.join(marker_dir, "grandchild.pid")!r}, "w").write(str(os.getpid()))
    time.sleep(60)
    sys.exit(0)
time.sleep(60)
"""
    return [sys.executable, "-c", script]


def test_sigkill_escalation_is_not_skipped_when_the_direct_child_dies_from_sigterm(
    tmp_path,
    monkeypatch,
    policy,
):
    """The direct child dies promptly from SIGTERM (so its own ``proc.wait()``
    inside ``_terminate_process_group`` succeeds within the grace period),
    but its grandchild ignores SIGTERM outright. The grandchild must still be
    reaped via SIGKILL — proving the escalation step is an unconditional
    backstop, not gated on whether the direct child's own wait succeeded.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)
    cmd = _spawn_grandchild_that_ignores_sigterm(str(marker))
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=1.0,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), (
            "SIGTERM-ignoring grandchild survived teardown — SIGKILL "
            "escalation was skipped because the direct child's own "
            "proc.wait() already succeeded"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def _spawn_grandchild_that_finishes_cleanup_before_exiting(marker_dir: str) -> list[str]:
    """A command whose direct child dies promptly on SIGTERM (default
    handling) but whose forked grandchild installs its own SIGTERM handler
    that does a brief bit of "cleanup" work — well within the grace period —
    before voluntarily exiting itself. Neither process is ever actually
    killed by the parent's signals: this is the scenario
    ``test_sigkill_escalation_is_not_skipped_when_the_direct_child_dies_from_sigterm``
    (which uses ``SIG_IGN``, i.e. a grandchild that never voluntarily exits)
    cannot catch — that test only proves eventual escalation happens, not
    that premature escalation doesn't cut a graceful shutdown short.
    """
    started_marker = os.path.join(marker_dir, "cleanup_started")
    finished_marker = os.path.join(marker_dir, "cleanup_finished")
    script = f"""
import os, signal, sys, time

def _on_sigterm(signum, frame):
    open({started_marker!r}, "w").write("1")
    time.sleep(0.5)
    open({finished_marker!r}, "w").write("1")
    sys.exit(0)

open({os.path.join(marker_dir, "child.pid")!r}, "w").write(str(os.getpid()))
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, _on_sigterm)
    open({os.path.join(marker_dir, "grandchild.pid")!r}, "w").write(str(os.getpid()))
    time.sleep(60)
    sys.exit(0)
time.sleep(60)
"""
    return [sys.executable, "-c", script]


def test_a_grandchild_gracefully_finishing_within_the_grace_period_is_not_sigkilled(
    tmp_path,
    monkeypatch,
    policy,
):
    """The whole-group grace period must be honoured even though the direct
    child (the process-group leader) exits near-instantly on SIGTERM by
    default. A grandchild that installs its own SIGTERM handler, does brief
    cleanup work well within the grace period, and then exits voluntarily
    must be allowed to finish — it must never be SIGKILLed mid-cleanup just
    because the direct child's own ``proc.wait()`` already returned.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)

    killpg_signals: list[int] = []
    real_killpg = os.killpg

    def _spying_killpg(pgid: int, sig: int) -> None:
        killpg_signals.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(containerlab.os, "killpg", _spying_killpg)

    cmd = _spawn_grandchild_that_finishes_cleanup_before_exiting(str(marker))
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=1.0,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert (marker / "cleanup_finished").exists(), (
            "grandchild's voluntary cleanup was interrupted before it could finish — "
            "the grace period was not honoured for the whole process group"
        )
        assert not _pid_alive(grandchild_pid), "grandchild should have exited voluntarily"
        assert signal.SIGKILL not in killpg_signals, (
            "SIGKILL was sent even though the grandchild exited voluntarily " "within the grace period"
        )
        assert signal.SIGTERM in killpg_signals
    finally:
        if _pid_alive(proc.pid):
            try:
                real_killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def _spawn_direct_child_that_finishes_cleanup_before_exiting(marker_dir: str) -> list[str]:
    """A command with no grandchild at all: the *direct* child spawned by
    ``_run_contained`` itself installs its own SIGTERM handler, does a brief
    bit of "cleanup" work well within the grace period, and then exits
    voluntarily — proving SIGTERM is actually deliverable to the process
    ``subprocess.Popen`` itself forks, not merely to something it later
    forks. This is the regression surface for the "child inherits SIGTERM
    blocked" bug: ``preexec_fn`` unblocking the mask happens in exactly this
    process, right after its own fork and before its own exec — a grandchild
    fixture cannot isolate that from the transitive "does an unblocked mask
    survive across a subsequent fork" question.
    """
    started_marker = os.path.join(marker_dir, "cleanup_started")
    finished_marker = os.path.join(marker_dir, "cleanup_finished")
    script = f"""
import os, signal, sys, time

def _on_sigterm(signum, frame):
    open({started_marker!r}, "w").write("1")
    time.sleep(0.5)
    open({finished_marker!r}, "w").write("1")
    sys.exit(0)

signal.signal(signal.SIGTERM, _on_sigterm)
open({os.path.join(marker_dir, "child.pid")!r}, "w").write(str(os.getpid()))
time.sleep(60)
"""
    return [sys.executable, "-c", script]


def test_a_direct_child_actually_receives_sigterm_despite_the_parents_own_mask(
    tmp_path,
    monkeypatch,
    policy,
):
    """The direct child spawned by ``_run_contained`` must NOT inherit this
    process's own SIGTERM-blocked mask (blocked around ``Popen(...)`` itself
    to close the construction-window reentrancy hole). Without
    ``preexec_fn=_unblock_worker_signals_in_child`` unblocking the signal in
    the child after fork but before exec, the child would inherit the block
    permanently (fork() copies the mask; exec() does not clear a blocked
    signal) and could never act on the SIGTERM
    ``_terminate_process_group``'s escalation sends it — forcing every
    cleanup to fall through to SIGKILL regardless of how gracefully the
    child would otherwise have shut down.

    Pre-fix: the child's SIGTERM handler never fires (signal permanently
    blocked in the child), so ``cleanup_finished`` never appears and SIGKILL
    is required. Post-fix: the handler fires, cleanup finishes within the
    grace period, and SIGKILL is never sent.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)

    killpg_signals: list[int] = []
    real_killpg = os.killpg

    def _spying_killpg(pgid: int, sig: int) -> None:
        killpg_signals.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(containerlab.os, "killpg", _spying_killpg)

    cmd = _spawn_direct_child_that_finishes_cleanup_before_exiting(str(marker))
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=1.0,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    proc = created[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(proc.pid):
            time.sleep(0.05)
        assert (marker / "cleanup_finished").exists(), (
            "the direct child's own SIGTERM handler never fired — it never "
            "actually received the signal (still blocked in the child?)"
        )
        assert not _pid_alive(proc.pid), "direct child should have exited voluntarily"
        assert signal.SIGKILL not in killpg_signals, (
            "SIGKILL was sent even though the direct child exited voluntarily "
            "within the grace period — its SIGTERM handler must have been "
            "unreachable"
        )
        assert signal.SIGTERM in killpg_signals
    finally:
        if _pid_alive(proc.pid):
            try:
                real_killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_cancellation_mid_communicate_follows_the_same_group_cleanup_path(
    monkeypatch,
    policy,
):
    """A non-timeout exception raised mid-communicate (e.g. KeyboardInterrupt
    reaching this call) must still tear down the process group before
    propagating, not just a TimeoutExpired. The exception itself is
    simulated via monkeypatch on the real process's ``communicate`` — the
    process it stands in for is a genuinely real, separately spawned OS
    process, so signal delivery is still proven against the real thing.
    """
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    real_proc = subprocess.Popen(cmd, start_new_session=True)  # noqa: S603
    monkeypatch.setattr(real_proc, "communicate", MagicMock(side_effect=KeyboardInterrupt()))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: real_proc)
    try:
        with pytest.raises(KeyboardInterrupt):
            containerlab._run_contained(
                cmd,
                timeout=30,
                grace_seconds=policy.lab_process_termination_grace_seconds,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(real_proc.pid):
            time.sleep(0.05)
        assert not _pid_alive(real_proc.pid), "child survived a cancelled communicate()"
    finally:
        if _pid_alive(real_proc.pid):
            os.killpg(real_proc.pid, signal.SIGKILL)
        real_proc.wait(timeout=5)


def test_real_sigterm_mid_communicate_triggers_group_cleanup(
    tmp_path,
    monkeypatch,
    policy,
):
    """A REAL SIGTERM delivered to this process while it is genuinely
    blocked inside ``_run_contained``'s ``communicate()`` on a real spawned
    process must interrupt that blocking call via the lab worker's own
    SIGTERM handler and tear down the whole process group.

    Unlike ``test_cancellation_mid_communicate_follows_the_same_group_cleanup_path``,
    which simulates the interrupting exception via monkeypatch, this proves
    real OS signal delivery: a background thread sends a genuine
    ``os.kill(os.getpid(), signal.SIGTERM)`` at the current process while the
    main thread is blocked in a real ``communicate()`` call, with
    ``_sigterm_raises_shutdown_requested`` (the exact context manager
    ``_lab_worker`` installs for its own process lifetime) active for the
    duration of the test.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)
    cmd = _spawn_grandchild_tree(str(marker))

    previous_handler = signal.getsignal(signal.SIGTERM)

    def _send_sigterm_shortly() -> None:
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    with _sigterm_raises_shutdown_requested():
        sender = threading.Thread(target=_send_sigterm_shortly, daemon=True)
        sender.start()
        try:
            with pytest.raises(_WorkerShutdownRequested):
                containerlab._run_contained(
                    cmd,
                    timeout=30,
                    grace_seconds=policy.lab_process_termination_grace_seconds,
                )
        finally:
            sender.join(timeout=5)

    assert (
        signal.getsignal(signal.SIGTERM) == previous_handler
    ), "SIGTERM disposition leaked past _sigterm_raises_shutdown_requested"

    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_pid_alive(proc.pid) or _pid_alive(grandchild_pid)):
            time.sleep(0.05)
        assert not _pid_alive(proc.pid), "direct child survived a real SIGTERM's teardown"
        assert not _pid_alive(grandchild_pid), "grandchild survived a real SIGTERM's teardown"
    finally:
        if _pid_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_sigterm_arriving_mid_popen_construction_still_tears_down_the_child(
    tmp_path,
    monkeypatch,
    policy,
):
    """A SIGTERM landing after the real fork+exec but before ``_run_contained``'s
    own ``proc`` local is bound must not leak the freshly-spawned process.

    ``subprocess.Popen`` is wrapped so that, as a side effect of being
    *called* (simulating the exact race: the real fork has already happened
    by the time our wrapper's call to the real ``Popen`` returns), it sends a
    real SIGTERM at this process before handing the constructed ``Popen``
    object back to ``_run_contained`` — with
    ``_sigterm_raises_shutdown_requested`` active for the test's duration,
    exactly like the real-signal test above. Pre-fix, this exact setup leaks
    the process: the exception fires before ``proc`` is ever bound, so
    ``_run_contained``'s ``except BaseException:`` cleanup has no handle to
    call ``_terminate_process_group`` on. Post-fix, the SIGTERM is deferred
    (blocked) until immediately after ``proc`` is bound, so it is delivered
    only once ``communicate()`` has already started — following the existing,
    already-tested cleanup path.

    Deliberately a single non-forking process, not a grandchild tree: the
    injected signal fires essentially instantly after ``Popen()`` returns,
    racing against the freshly-exec'd child's own Python startup — on a
    loaded CI runner there is no guarantee it survives long enough to reach
    its own ``os.fork()`` and write a grandchild marker before being killed.
    That race is orthogonal to what this test proves (construction-window
    signal deferral); whole-group teardown of an already-existing
    grandchild is already covered, without this timing sensitivity, by
    ``test_timeout_terminates_an_orphaned_grandchild_too`` and the
    SIGTERM-ignoring-grandchild tests elsewhere in this file.
    """
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    real_popen = subprocess.Popen
    created: list[subprocess.Popen] = []

    def _popen_that_signals_before_returning(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        # Simulate the fork having already happened (it has — `proc` is a
        # real, live child by this point) while a SIGTERM lands before the
        # caller's own local variable is bound to the return value.
        os.kill(os.getpid(), signal.SIGTERM)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen_that_signals_before_returning)

    previous_handler = signal.getsignal(signal.SIGTERM)
    with _sigterm_raises_shutdown_requested():
        with pytest.raises(_WorkerShutdownRequested):
            containerlab._run_contained(
                cmd,
                timeout=30,
                grace_seconds=policy.lab_process_termination_grace_seconds,
            )
    assert signal.getsignal(signal.SIGTERM) == previous_handler

    assert created, "Popen was never actually called"
    proc = created[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(proc.pid):
            time.sleep(0.05)
        assert not _pid_alive(proc.pid), (
            "child process leaked — SIGTERM during Popen construction was not " "deferred past proc being bound"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_a_second_sigterm_mid_escalation_does_not_abort_teardown(
    tmp_path,
    monkeypatch,
    policy,
):
    """A second SIGTERM arriving while ``_terminate_process_group`` is
    already running (the exact scenario of an operator sending SIGTERM twice
    because the first one doesn't seem to have worked yet) must not abort the
    SIGTERM -> grace-period -> SIGKILL -> reap sequence partway through.

    Reuses the SIGTERM-ignoring-grandchild fixture so the full escalation
    path actually runs (SIGTERM does not kill the grandchild, forcing the
    grace-period poll and eventual SIGKILL). ``os.killpg`` is spied so that on
    its first call (the initial SIGTERM step, genuinely mid-cleanup — after
    ``_terminate_process_group`` has started but well before it has finished)
    it also fires a second real SIGTERM at this process before returning.
    Pre-fix, that second signal raises ``_WorkerShutdownRequested`` out of
    the escalation loop before the grace period or SIGKILL step ever runs,
    leaving the SIGTERM-ignoring grandchild alive. Post-fix, the second
    signal is deferred until the function returns, so escalation completes
    and the grandchild ends up SIGKILLed and dead.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)

    real_killpg = os.killpg
    killpg_calls: list[int] = []

    def _killpg_that_signals_on_first_call(pgid: int, sig: int) -> None:
        killpg_calls.append(sig)
        if len(killpg_calls) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        real_killpg(pgid, sig)

    monkeypatch.setattr(containerlab.os, "killpg", _killpg_that_signals_on_first_call)

    cmd = _spawn_grandchild_that_ignores_sigterm(str(marker))

    previous_handler = signal.getsignal(signal.SIGTERM)
    with _sigterm_raises_shutdown_requested():
        with pytest.raises(_WorkerShutdownRequested):
            containerlab._run_contained(
                cmd,
                timeout=1.0,
                grace_seconds=policy.lab_process_termination_grace_seconds,
            )
    assert signal.getsignal(signal.SIGTERM) == previous_handler

    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), (
            "SIGTERM-ignoring grandchild survived — a second SIGTERM "
            "mid-escalation aborted teardown before SIGKILL could run"
        )
        assert signal.SIGKILL in killpg_calls, (
            "escalation never reached SIGKILL — aborted early by the second " "SIGTERM instead of deferring it"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                real_killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_sigint_arriving_mid_popen_construction_still_tears_down_the_child(
    tmp_path,
    monkeypatch,
    policy,
):
    """The same construction-window leak
    ``test_sigterm_arriving_mid_popen_construction_still_tears_down_the_child``
    closes for SIGTERM must also be closed for a real terminal Ctrl+C
    (SIGINT -> ``KeyboardInterrupt``, Python's own default disposition).
    ``_run_contained``'s own docstring promises cleanup for cancellation
    generally, not only for SIGTERM — the previous round's fix only extended
    ``pthread_sigmask`` to block ``{SIGTERM}``, leaving this identical window
    open for SIGINT.

    No ``_sigterm_raises_shutdown_requested``-style machinery is needed here:
    Python's own default SIGINT handler already converts a delivered SIGINT
    into ``KeyboardInterrupt`` at the point of delivery.

    Deliberately a single non-forking process, not a grandchild tree — see
    ``test_sigterm_arriving_mid_popen_construction_still_tears_down_the_child``'s
    docstring for why a grandchild-tree command races the injected signal
    against the freshly-exec'd child's own startup time.
    """
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    real_popen = subprocess.Popen
    created: list[subprocess.Popen] = []

    def _popen_that_signals_before_returning(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        created.append(proc)
        # Simulate the fork having already happened (it has) while a SIGINT
        # (a real Ctrl+C) lands before the caller's own local variable is
        # bound to the return value.
        os.kill(os.getpid(), signal.SIGINT)
        return proc

    monkeypatch.setattr(subprocess, "Popen", _popen_that_signals_before_returning)

    previous_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        containerlab._run_contained(
            cmd,
            timeout=30,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    assert signal.getsignal(signal.SIGINT) == previous_handler

    assert created, "Popen was never actually called"
    proc = created[0]
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(proc.pid):
            time.sleep(0.05)
        assert not _pid_alive(proc.pid), (
            "child process leaked — SIGINT during Popen construction was not " "deferred past proc being bound"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_a_second_sigint_mid_escalation_does_not_abort_teardown(
    tmp_path,
    monkeypatch,
    policy,
):
    """The SIGINT counterpart of
    ``test_a_second_sigterm_mid_escalation_does_not_abort_teardown``: a real
    Ctrl+C landing while ``_terminate_process_group`` is already running
    (mid-escalation) must not abort the SIGTERM -> grace-period -> SIGKILL ->
    reap sequence partway through via an unhandled ``KeyboardInterrupt``.

    Reuses the SIGTERM-ignoring-grandchild fixture so the full escalation
    path actually runs. ``os.killpg`` is spied so that on its first call (the
    initial SIGTERM step) it also fires a real SIGINT at this process before
    returning. Pre-fix, that SIGINT raises ``KeyboardInterrupt`` out of the
    escalation loop before the grace period or SIGKILL step ever runs,
    leaving the SIGTERM-ignoring grandchild alive. Post-fix, the SIGINT is
    deferred until the function returns, so escalation completes and the
    grandchild ends up SIGKILLed and dead.
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)

    real_killpg = os.killpg
    killpg_calls: list[int] = []

    def _killpg_that_signals_on_first_call(pgid: int, sig: int) -> None:
        killpg_calls.append(sig)
        if len(killpg_calls) == 1:
            os.kill(os.getpid(), signal.SIGINT)
        real_killpg(pgid, sig)

    monkeypatch.setattr(containerlab.os, "killpg", _killpg_that_signals_on_first_call)

    cmd = _spawn_grandchild_that_ignores_sigterm(str(marker))

    previous_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        containerlab._run_contained(
            cmd,
            timeout=1.0,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    assert signal.getsignal(signal.SIGINT) == previous_handler

    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), (
            "SIGTERM-ignoring grandchild survived — a second interrupt "
            "(SIGINT) mid-escalation aborted teardown before SIGKILL could run"
        )
        assert signal.SIGKILL in killpg_calls, (
            "escalation never reached SIGKILL — aborted early by the SIGINT " "instead of deferring it"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                real_killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_run_contained_returns_a_completed_process_shaped_result(
    policy,
):
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(returncode=0, stdout="ok", stderr="")
        result = containerlab._run_contained(
            ["true"],
            timeout=5,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == "ok"
    assert result.stderr == ""


def test_run_contained_nonzero_exit_still_returns_normally(
    policy,
):
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(returncode=1, stdout="", stderr="boom")
        result = containerlab._run_contained(
            ["false"],
            timeout=5,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )
    assert result.returncode == 1
    assert result.stderr == "boom"


def test_grace_period_polling_does_not_busy_spin_after_the_direct_child_is_reaped(
    tmp_path,
    monkeypatch,
    policy,
):
    """The poll loop must not degenerate into a busy-spin once the direct
    child (process-group leader) has already been reaped by ``proc.wait()``.

    Reuses the same shape as
    ``test_sigkill_escalation_is_not_skipped_when_the_direct_child_dies_from_sigterm``:
    the direct child dies from SIGTERM almost immediately (default handling)
    while its SIGTERM-ignoring grandchild keeps the whole process group
    alive for the entire grace period, forcing every iteration after the
    first to go through a ``proc.wait()`` call on an already-reaped child.

    ``Popen.wait()`` caches the child's return code after that first reap,
    so every later call returns instantly regardless of the requested
    ``timeout``. If the loop relied on that call alone to pace itself, it
    would hammer the ``os.killpg(pid, 0)`` liveness probe as fast as the CPU
    allows for the rest of the grace period instead of sleeping between
    polls. This pins the probe-call count to roughly ``grace /
    poll_interval`` (with slack for scheduling jitter), which the pre-fix
    busy-spinning code blew through by orders of magnitude (thousands of
    calls within milliseconds).
    """
    marker = tmp_path / "markers"
    marker.mkdir()
    created = _spy_on_real_popen(monkeypatch)

    monkeypatch.setattr(containerlab, "_GROUP_LIVENESS_POLL_INTERVAL_SECONDS", 0.1)

    probe_calls: list[int] = []
    real_killpg = os.killpg

    def _spying_killpg(pgid: int, sig: int) -> None:
        if sig == 0:
            probe_calls.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(containerlab.os, "killpg", _spying_killpg)

    cmd = _spawn_grandchild_that_ignores_sigterm(str(marker))
    with pytest.raises(subprocess.TimeoutExpired):
        containerlab._run_contained(
            cmd,
            timeout=0.2,
            grace_seconds=1.0,
        )
    proc = created[0]
    try:
        assert (marker / "grandchild.pid").exists(), "grandchild never started"
        grandchild_pid = int((marker / "grandchild.pid").read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.05)
        assert not _pid_alive(grandchild_pid), "grandchild outlived the grace period"
        # grace / poll_interval == 10 iterations at most, generously padded
        # for scheduling jitter. The pre-fix busy-spin produced thousands of
        # probe calls in a fraction of that window.
        assert len(probe_calls) <= 40, (
            f"expected roughly bounded polling, got {len(probe_calls)} probe " "calls — the loop is busy-spinning"
        )
    finally:
        if _pid_alive(proc.pid):
            try:
                real_killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=5)


def test_terminate_process_group_swallows_already_terminated_process(
    policy,
):
    """ProcessLookupError at either signalling step must not mask the caller's
    real exception — it means the group is already gone, not a new failure.
    """
    proc = MagicMock()
    proc.pid = 99999999  # exceedingly unlikely to be a real pid
    with patch(
        "app.services.labengine.containerlab.os.killpg",
        side_effect=ProcessLookupError,
    ):
        containerlab._terminate_process_group(
            proc,
            grace_seconds=policy.lab_process_termination_grace_seconds,
        )  # must not raise
    proc.wait.assert_called_once()


def test_unblock_call_itself_raising_still_reaches_terminate_process_group(
    monkeypatch,
    policy,
):
    """Pins the exact mechanism behind the construction-window fix, independent
    of real OS signal timing: CPython's own
    ``signal.pthread_sigmask(SIG_UNBLOCK, ...)`` can synchronously raise a
    pending signal's handler exception as a side effect of that call
    returning — before any following bytecode runs. If that unblock call
    lived in a bare ``finally`` after ``Popen(...)`` (as it did pre-fix)
    rather than inside the ``try`` guarding ``communicate()``, an exception
    raised there would propagate straight out of ``_run_contained`` without
    ever reaching ``_terminate_process_group`` — leaking the child with no
    cleanup attempt at all.

    ``signal.pthread_sigmask`` is mocked to raise only on its *second* call
    (the ``SIG_UNBLOCK`` immediately before ``communicate()``) — the first
    call (``SIG_BLOCK``, before ``Popen(...)``) must succeed normally so
    ``proc`` gets bound. This proves: (a) the exception surfaces at exactly
    that call, and (b) ``_terminate_process_group`` still runs, with the
    correct ``proc``, despite it.
    """
    real_mask = signal.pthread_sigmask
    calls: list[tuple[int, object]] = []

    class _Boom(BaseException):
        pass

    def _fake_mask(how, mask):
        calls.append((how, mask))
        if len(calls) == 2:
            raise _Boom("simulated pending-signal delivery on unblock")
        return real_mask(how, mask)

    monkeypatch.setattr(signal, "pthread_sigmask", _fake_mask)

    fake_proc = _fake_popen()
    with patch("subprocess.Popen", return_value=fake_proc):
        with patch("app.services.labengine.containerlab._terminate_process_group") as terminate:
            with pytest.raises(_Boom):
                containerlab._run_contained(
                    ["true"],
                    timeout=5,
                    grace_seconds=policy.lab_process_termination_grace_seconds,
                )
            terminate.assert_called_once_with(fake_proc, grace_seconds=policy.lab_process_termination_grace_seconds)
    fake_proc.communicate.assert_not_called()


def test_final_reap_does_not_block_indefinitely_on_a_process_that_never_exits(
    monkeypatch,
    policy,
):
    """The last ``proc.wait()`` in ``_terminate_process_group`` — after the
    SIGTERM/grace/SIGKILL sequence has already run — must be bounded, not an
    unbounded blocking wait. A real process wedged in an uninterruptible
    kernel sleep (D-state) cannot be interrupted even by SIGKILL, so an
    unbounded wait here would hold this worker's locks hostage forever.

    A genuinely unkillable real process is not practically constructible in
    a test, so ``proc.wait`` is monkeypatched to always raise
    ``subprocess.TimeoutExpired`` — simulating a process that never actually
    exits no matter how long is waited. The grace period is shrunk so the
    test itself stays fast. The function must still return (not hang, not
    raise) within a bounded time.
    """

    proc = MagicMock()
    proc.pid = 99999999  # exceedingly unlikely to be a real pid
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=0.05)

    with patch(
        "app.services.labengine.containerlab.os.killpg",
        side_effect=ProcessLookupError,
    ):
        started = time.monotonic()
        containerlab._terminate_process_group(
            proc,
            grace_seconds=0.05,
        )  # must not hang
        elapsed = time.monotonic() - started

    assert elapsed < 5, f"final reap blocked for {elapsed}s instead of returning promptly"
    # Called once for the (skipped, since killpg raised ProcessLookupError)
    # grace-period loop path is never entered here — only the final bounded
    # reap call itself.
    proc.wait.assert_called_once_with(timeout=0.05)


@pytest.fixture
def policy():
    """Explicit validated env/default snapshot; never used as a production fallback."""
    return LabExecutionPolicy.from_settings(settings)


def test_nondefault_policy_controls_every_command_wait_and_cleanup_grace(tmp_path, monkeypatch):
    from dataclasses import replace

    from app.config import Settings

    source = LabExecutionPolicy.from_settings(Settings.model_construct())
    policy = replace(
        source,
        lab_command_timeout_seconds_by_action={"inspect": 2, "deploy": 7, "destroy": 3, "check": 4},
        lab_operation_lease_seconds_by_kind={"deploy": 30, "destroy": 20, "check": 15},
        lab_process_termination_grace_seconds=2,
    )
    engine = ContainerlabEngine(str(tmp_path), lab_host_role="lab", execution_policy=policy)
    calls = []

    def run(command, *, timeout, grace_seconds):
        calls.append((command, timeout, grace_seconds))
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(containerlab, "_run_contained", run)
    monkeypatch.setattr(containerlab.os.path, "exists", lambda path: True)
    engine._inspect_all()
    engine._deploy_unlocked("name: x\ntopology: {nodes: {r1: {kind: linux}}}", "x")
    engine._destroy_unlocked("x")
    handle = LabHandle("x", {"r1": "node"}, {"r1": "10.0.0.1"}, {"r1": "linux"})
    engine._exec_unlocked(handle, "r1", ["true"])
    engine._ssh_exec_unlocked(handle, "r1", "true", "admin", "")
    assert engine.execution_policy is policy
    for command, timeout, _ in calls:
        expected = 7 if "deploy" in command else 2 if "inspect" in command else 3 if "destroy" in command else 4
        assert 0 < timeout <= expected
        assert timeout == pytest.approx(expected, abs=0.1)
    assert all(grace == 2 for _, _, grace in calls)
    assert policy.lease_seconds("deploy") == 30
    assert policy.lease_seconds("destroy") == 20
    assert policy.lease_seconds("check") == 15


def test_default_combined_path_wait_budgets_include_both_cleanup_graces(policy):
    waits = policy.lab_command_timeout_seconds_by_action
    grace = policy.lab_process_termination_grace_seconds
    inspect, deploy, destroy, check = (waits[action] for action in ("inspect", "deploy", "destroy", "check"))
    assert max(inspect + destroy, inspect + deploy, 2 * inspect) + 2 * grace == 970
    assert 2 * inspect + destroy + 2 * grace == 550
    assert check + 2 * grace == 490
    assert policy.lease_seconds("deploy") > 970
    assert policy.lease_seconds("destroy") > 550
    assert policy.lease_seconds("check") > 490
    assert policy.lease_seconds("unknown") > inspect + 2 * grace
    # Direct reset has no lease heartbeat between destroy and deploy; it remains
    # a direct engine operation and does not define the worker's renewal budget.
    assert inspect + destroy + deploy + 2 * grace > policy.lease_seconds("deploy")
