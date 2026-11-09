"""Every audit action the product writes is declared by the assembled registry.

The kernel refuses an undeclared action at write time, which rolls back the whole
operation (a production account reset or password change would fail). This walks
the product's call sites statically and checks the assembled registry.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_learning.manifest import module as learning_module

from app.academy_declarations import CREDENTIAL_LIFECYCLE_AUDIT_ACTIONS
from app.owner_composition import compose_migration_owner_declarations

APP = Path(__file__).resolve().parents[1] / "app"
ACTION = re.compile(r"^[a-z][a-z_]*(\.[a-z_]+)+$")
# Callables whose first positional argument is an audit action.
FIRST_ARGUMENT = {"_audit", "_stage_audit", "_write_transition_audit"}
# _Operation(name, surface, roles, action, entity_type) in services/audit_context.py.
OPERATION_ACTION_INDEX = 3


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and ACTION.match(node.value):
        return node.value
    return None


def _written_actions() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def add(value, path, line):
        if value is not None:
            found.setdefault(value, set()).add(f"{path.relative_to(APP.parent)}:{line}")

    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                for keyword in node.keywords:
                    if keyword.arg == "action":
                        add(_literal(keyword.value), path, keyword.value.lineno)
                if name in FIRST_ARGUMENT and node.args:
                    add(_literal(node.args[0]), path, node.lineno)
                if name == "_Operation" and len(node.args) > OPERATION_ACTION_INDEX:
                    add(_literal(node.args[OPERATION_ACTION_INDEX]), path, node.lineno)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "action":
                        add(_literal(value), path, value.lineno)
            elif isinstance(node, ast.Compare) and ast.unparse(node.left).endswith("action"):
                # e.g. `entry.action != "credential.reset.completed"` in the reset adapter.
                for comparator in node.comparators:
                    add(_literal(comparator), path, comparator.lineno)
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.endswith("_ACTION") for t in node.targets
            ):
                add(_literal(node.value), path, node.lineno)
    return found


def test_scan_finds_the_known_writers():
    # Guards the scan itself: a refactor that hides call sites must not pass silently.
    found = _written_actions()
    assert {
        "credential.password.assigned",
        "credential.password.changed",
        "credential.sessions.revoked",
        "credential.reset.completed",
        "credential.reset_tokens.invalidated",
        "facilitator_nomination.create",
        "applicant.transition",
        "reminder.resend",
        "account.invitation.completed",
        "scheduling.update_session",
        "success_queue.acknowledge",
    } <= set(found)


def test_every_written_audit_action_is_declared_by_the_assembled_registry():
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    undeclared = {a: sorted(w) for a, w in _written_actions().items() if not registry.is_declared(a)}
    assert undeclared == {}


def test_credential_lifecycle_actions_are_declared_by_academy_not_learning():
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    assert len(set(CREDENTIAL_LIFECYCLE_AUDIT_ACTIONS)) == 4
    for action in CREDENTIAL_LIFECYCLE_AUDIT_ACTIONS:
        assert registry.owner(action) == "academy"
        assert action not in learning_module.audit_actions


def test_fenced_lab_result_audit_is_owned_by_academy():
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    assert registry.owner("lab_result.record") == "academy"
    assert "lab_result.record" not in learning_module.audit_actions
