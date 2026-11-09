from unittest.mock import MagicMock

from app.services.checks.engine import run_checks
from app.services.labengine.interface import ExecResult


def _engine(stdout="", code=0):
    e = MagicMock()
    e.exec.return_value = ExecResult(stdout, "", code)
    e.ssh_exec.return_value = ExecResult(stdout, "", code)
    return e


def test_probe_ping_pass():
    eng = _engine(stdout="2 packets transmitted, 2 received", code=0)
    checks = [
        {
            "id": "c1",
            "type": "probe",
            "node": "client",
            "probe": {"kind": "ping", "target": "10.9.0.{{o}}", "count": 2, "min_success": 1},
            "weight": 2,
        }
    ]
    out = run_checks(checks, eng, MagicMock(nodes={"client": "c"}), {"o": 1})
    assert out["score"] == 2
    assert out["max_score"] == 2
    assert out["per_check"][0]["pass"] is True


def test_run_checks_refreshes_the_lease_before_every_external_check():
    eng = _engine(stdout="1 packets transmitted, 1 received", code=0)
    checks = [
        {
            "id": f"ping-{index}",
            "type": "probe",
            "node": "client",
            "probe": {"kind": "ping", "target": "127.0.0.1", "count": 1},
        }
        for index in range(2)
    ]
    refresh = MagicMock()

    run_checks(
        checks,
        eng,
        MagicMock(nodes={"client": "c"}),
        {},
        before_each=refresh,
    )

    assert refresh.call_count == 2


def test_command_jsonpath_fail_reports_actual():
    eng = _engine(stdout='{"state":"Idle"}', code=0)
    checks = [
        {
            "id": "bgp",
            "type": "command",
            "node": "r1",
            "command": "vtysh -c 'show ip bgp json'",
            "assert": {"jsonpath": "$.state", "equals": "Established"},
            "weight": 1,
        }
    ]
    out = run_checks(checks, eng, MagicMock(nodes={"r1": "r1c"}), {})
    assert out["score"] == 0
    assert out["per_check"][0]["pass"] is False
    assert out["per_check"][0]["actual"] == "Idle"


def test_command_jsonpath_pass_established():
    eng = _engine(stdout='{"state":"Established"}', code=0)
    checks = [
        {
            "id": "bgp",
            "type": "command",
            "node": "r1",
            "command": "vtysh -c 'show ip bgp json'",
            "assert": {"jsonpath": "$.state", "equals": "Established"},
            "weight": 3,
        }
    ]
    out = run_checks(checks, eng, MagicMock(nodes={"r1": "r1c"}), {})
    assert out["score"] == 3
    assert out["per_check"][0]["pass"] is True
    assert out["per_check"][0]["actual"] == "Established"


def test_command_transport_ssh_routes_to_ssh_exec():
    eng = _engine(stdout="anything", code=0)
    checks = [
        {
            "id": "ros",
            "type": "command",
            "node": "r1",
            "transport": "ssh",
            "user": "admin",
            "password": "{{pw}}",
            "command": "/interface print",
            "assert": {"regex": "ether1"},
            "weight": 1,
        }
    ]
    eng.ssh_exec.return_value = ExecResult("ether1: up", "", 0)
    out = run_checks(checks, eng, MagicMock(nodes={"r1": "r1c"}), {"pw": "secret"})
    eng.ssh_exec.assert_called_once()
    eng.exec.assert_not_called()
    _args, kwargs = eng.ssh_exec.call_args
    assert kwargs.get("user") == "admin"
    assert kwargs.get("password") == "secret"
    assert out["score"] == 1
    assert out["per_check"][0]["pass"] is True


def test_command_exit_code_default():
    eng = _engine(stdout="", code=0)
    checks = [{"id": "x", "type": "command", "node": "c", "command": "true", "weight": 1}]
    out = run_checks(checks, eng, MagicMock(nodes={"c": "cc"}), {})
    assert out["score"] == 1
    assert out["per_check"][0]["actual"] == 0
    assert out["per_check"][0]["expected"] == 0


def test_config_grep():
    eng = _engine(stdout="ip address 10.0.5.1/24\n", code=0)
    checks = [
        {
            "id": "cfg",
            "type": "config_grep",
            "node": "r1",
            "file": "/etc/frr/frr.conf",
            "contains": "10.0.{{o}}.1/24",
            "weight": 1,
        }
    ]
    out = run_checks(checks, eng, MagicMock(nodes={"r1": "r1c"}), {"o": 5})
    assert out["score"] == 1
    assert out["per_check"][0]["pass"] is True
    assert out["per_check"][0]["expected"] == "10.0.5.1/24"


def test_eval_check_shape():
    from app.services.checks.engine import eval_check

    eng = _engine(stdout="1 packets transmitted, 1 received", code=0)
    check = {
        "id": "p",
        "type": "probe",
        "node": "c",
        "probe": {"kind": "ping", "target": "10.0.0.1", "count": 1, "min_success": 1},
        "weight": 4,
    }
    res = eval_check(check, eng, MagicMock(nodes={"c": "cc"}), {})
    # ``label`` and ``detail`` are always present; ``hint`` only on a failure,
    # and this check passes.
    assert set(res) == {"id", "label", "weight", "pass", "actual", "expected", "detail"}
    assert res["id"] == "p"
    assert res["weight"] == 4


# ---------------------------------------------------------------------------
# Learner-facing feedback: every check explains itself in words
# ---------------------------------------------------------------------------


def test_unreadable_value_says_so_instead_of_none():
    """Regression: a failed jsonpath read rendered as "actual: None", which tells
    a learner nothing about what to fix. It must distinguish "could not read"
    from "read and wrong", and show what the command actually printed."""
    eng = _engine(stdout="", code=0)
    checks = [
        {
            "id": "client-a-prefix",
            "type": "command",
            "node": "client-a",
            "command": "ip -j addr show eth1",
            "assert": {"jsonpath": "$[0].addr_info[0].prefixlen", "equals": 30},
            "weight": 1,
        }
    ]
    item = run_checks(checks, eng, MagicMock(nodes={"client-a": "c"}), {})["per_check"][0]
    assert item["pass"] is False
    assert item["actual"] is None
    detail = item["detail"]
    assert "could not read" in detail
    assert "client-a" in detail
    assert "no output" in detail  # the command printed nothing


def test_wrong_value_is_reported_as_read_not_unreadable():
    eng = _engine(stdout='{"state":"Idle"}', code=0)
    checks = [
        {
            "id": "bgp",
            "type": "command",
            "node": "r1",
            "command": "vtysh -c 'show ip bgp json'",
            "assert": {"jsonpath": "$.state", "equals": "Established"},
        }
    ]
    detail = run_checks(checks, eng, MagicMock(nodes={"r1": "r1c"}), {})["per_check"][0]["detail"]
    assert "read Idle" in detail
    assert "Established" in detail
    assert "could not read" not in detail


def test_failed_ping_names_the_target_and_the_shortfall():
    eng = _engine(stdout="3 packets transmitted, 0 received", code=1)
    checks = [
        {
            "id": "ping-a-to-b",
            "type": "probe",
            "node": "client-a",
            "probe": {"kind": "ping", "target": "10.{{t}}.0.6", "count": 3, "min_success": 2},
            "weight": 2,
        }
    ]
    item = run_checks(checks, eng, MagicMock(nodes={"client-a": "c"}), {"t": 11})["per_check"][0]
    assert item["pass"] is False
    assert "could not reach 10.11.0.6" in item["detail"]
    assert "0 of 3" in item["detail"]


def test_exit_code_failure_quotes_the_command_and_output():
    eng = _engine(stdout="", code=1)
    checks = [
        {
            "id": "client-a-subnet",
            "type": "command",
            "node": "client-a",
            "command": "ip -4 -o addr show eth1 | grep -qF ' 10.11.0.2/30'",
            "assert": {"exit_code": 0},
        }
    ]
    detail = run_checks(checks, eng, MagicMock(nodes={"client-a": "c"}), {})["per_check"][0]["detail"]
    assert "exited 1" in detail
    assert "grep" in detail


def test_label_and_hint_are_passed_through_on_failure_only():
    eng = _engine(stdout="", code=1)
    base = {
        "id": "client-a-subnet",
        "type": "command",
        "node": "client-a",
        "command": "true",
        "assert": {"exit_code": 0},
        "label": "client-a has a /30 address on eth1",
        "hint": "Assign 10.11.0.2/30 to eth1 on client-a, then run the checks again.",
    }
    failed = run_checks([base], eng, MagicMock(nodes={"client-a": "c"}), {})["per_check"][0]
    assert failed["label"] == "client-a has a /30 address on eth1"
    assert failed["hint"].startswith("Assign 10.11.0.2/30")

    # A passing check needs no corrective action, so the hint is not carried.
    passed = run_checks([base], _engine(stdout="", code=0), MagicMock(nodes={"client-a": "c"}), {})["per_check"][0]
    assert passed["pass"] is True
    assert "hint" not in passed
    assert passed["label"] == "client-a has a /30 address on eth1"


def test_checks_without_label_or_hint_still_evaluate():
    eng = _engine(stdout="", code=0)
    checks = [{"id": "bare", "type": "command", "node": "n", "command": "true", "assert": {"exit_code": 0}}]
    item = run_checks(checks, eng, MagicMock(nodes={"n": "c"}), {})["per_check"][0]
    assert item["pass"] is True
    assert item["label"] == ""
    assert item["detail"]


def test_long_command_output_is_truncated_in_the_detail():
    eng = _engine(stdout="x" * 500, code=1)
    checks = [{"id": "noisy", "type": "command", "node": "n", "command": "dump", "assert": {"exit_code": 0}}]
    detail = run_checks(checks, eng, MagicMock(nodes={"n": "c"}), {})["per_check"][0]["detail"]
    assert len(detail) < 400
    assert "…" in detail
