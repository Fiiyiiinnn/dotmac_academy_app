# ruff: noqa: F811  # imported pytest fixtures are redefined as test parameters
"""Issue #2 port of legacy test_lab_result_capability.py.

Disposition: test_lab_result_capability_claim_fence_and_atomic_receipt
against app/services/lab_result_capability.py + the a0001 definer.
Adapter unit tests are copied verbatim; the hosted tests port the
atomic receipt/fixed audit, claim-fence 42501 refusals, replay
55000/42501, per-item 22023 validation, and the worker PII-read
refusals. Remaining numeric-parity/trap cases stay in legacy_probe.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.lab import LabTemplate
from app.services import lab_operations
from app.services.exceptions import ConflictError
from app.services.lab_result_capability import LabGradeReceipt, record_claimed_lab_check
from tests.issue2_wave1_fixtures import (  # noqa: F401 - pytest fixtures
    admin_engine,
    admin_session,
    app_client,
    app_user_session,
    canonical_account,
    current_account,
    invitation_account,
    lab_policy_rows,
    lab_worker_engine,
    lab_worker_session,
    no_real_console_spawn,
    policy,
    seed_lab,
    sign_in_account,
    tenant_a,
    tenant_b,
)


def _call(db, items):
    return record_claimed_lab_check(db, tenant_id=uuid4(), operation_id=uuid4(), claimed_by="worker", per_item=items)


def test_adapter_normalizes_scalars_and_binds_qualified_call():
    db = MagicMock()
    sid, score_id = uuid4(), uuid4()
    db.execute.return_value.mappings.return_value.one.return_value = {
        "submission_id": sid,
        "score_id": score_id,
        "score": 1.0,
        "max_score": 1.0,
        "passed": True,
    }
    receipt = _call(db, [{"actual": 12, "expected": False, "detail": None}])
    assert receipt == LabGradeReceipt(sid, score_id, 1.0, 1.0, True)
    with pytest.raises(FrozenInstanceError):
        receipt.passed = False
    statement, params = db.execute.call_args.args
    assert "public.record_claimed_lab_check" in str(statement)
    assert "pg_catalog.uuid" in str(statement)
    assert json.loads(params["items"]) == [{"actual": "12", "expected": "false", "detail": None}]


@pytest.mark.parametrize("value", [{"secret": "x"}, [1], "x" * 4097])
def test_adapter_rejects_unbounded_or_container_observations(value):
    db = MagicMock()
    with pytest.raises(ConflictError, match="invalid lab check payload"):
        _call(db, [{"actual": value}])
    db.execute.assert_not_called()


def test_database_detail_is_never_logged_or_raised(caplog):
    db = MagicMock()
    db.execute.side_effect = DBAPIError("sensitive query", {}, Exception("private learner email"))
    with pytest.raises(ConflictError, match="lab check could not be recorded") as caught:
        _call(db, [])
    assert caught.value.__cause__ is None
    assert "private learner" not in caplog.text
    assert "sensitive query" not in caplog.text


@pytest.mark.parametrize(
    "sqlstate,category", [("42501", "42501"), ("23505", "23505"), ("secret", "unknown"), ("P0001", "other")]
)
def test_refusal_metric_has_bounded_labels_and_safe_log(monkeypatch, caplog, sqlstate, category):
    from app.services import lab_result_capability as capability

    counter = MagicMock()
    monkeypatch.setattr(capability, "LAB_RESULT_REFUSALS", counter)
    # Alembic's logging fileConfig (genesis fixture) disables existing
    # loggers in-process; re-enable the capability logger for capture.
    import logging

    monkeypatch.setattr(logging.getLogger("app.services.lab_result_capability"), "disabled", False)
    original = Exception("private learner address")
    original.sqlstate = sqlstate
    db = MagicMock()
    db.execute.side_effect = DBAPIError("private statement", {"private": "parameters"}, original)
    with (
        caplog.at_level(logging.WARNING, logger="app.services.lab_result_capability"),
        pytest.raises(ConflictError) as exc,
    ):
        _call(db, [])
    assert str(exc.value) == "lab check could not be recorded"
    assert exc.value.__suppress_context__ is True
    counter.labels.assert_called_once_with(sqlstate=category)
    counter.labels.return_value.inc.assert_called_once_with()
    assert "class=DBAPIError" in caplog.text
    assert "category=" in caplog.text
    assert "private" not in caplog.text


def test_adapter_payload_byte_limit_is_checked_before_database():
    db = MagicMock()
    with pytest.raises(ConflictError, match="invalid lab check payload"):
        _call(db, [{"actual": "x" * 4096}] * 65)
    db.execute.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["actual", "expected"])
def test_nonfinite_observations_are_refused_before_string_conversion(value, field):
    db = MagicMock()
    with pytest.raises(ConflictError, match="invalid lab check payload"):
        _call(db, [{field: value}])
    db.execute.assert_not_called()


def test_jsonpath_engine_float_expectation_is_normalized_without_losing_result():
    from app.services.checks.engine import run_checks
    from app.services.labengine.interface import ExecResult, LabHandle

    engine = MagicMock()
    engine.exec.return_value = ExecResult('{"value":1.5}', "", 0)
    check = {
        "id": "number",
        "type": "command",
        "node": "client",
        "command": "read",
        "assert": {"jsonpath": "$.value", "equals": 1.5},
    }
    result = run_checks([check], engine, LabHandle("float-check", {}, {}, {}), {})
    assert result["per_check"][0]["pass"] is True
    db = MagicMock()
    db.execute.return_value.mappings.return_value.one.return_value = {
        "submission_id": uuid4(),
        "score_id": uuid4(),
        "score": 1.0,
        "max_score": 1.0,
        "passed": True,
    }
    _call(db, result["per_check"])
    payload = json.loads(db.execute.call_args.args[1]["items"])
    assert payload[0]["actual"] == "1.5"
    assert payload[0]["expected"] == "1.5"
    assert payload[0]["pass"] is True


@pytest.fixture
def claimed_lab(admin_session, lab_worker_session, tenant_a, policy):
    instance, person = seed_lab(admin_session, tenant_a.id, name="capability", status="active")
    template = admin_session.scalars(select(LabTemplate).where(LabTemplate.activity_id == instance.activity_id)).one()
    template.checks = [{"id": "one", "type": "command", "label": "One", "weight": 2}]
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
    admin_session.commit()
    claimed = lab_operations.claim_next(lab_worker_session, claimed_by="capability-worker", policy=policy)
    assert claimed.id == operation.id
    lab_worker_session.commit()
    return instance, operation, [{"id": "one", "label": "One", "weight": 2, "pass": True}]


def _raw_call(db, tenant, operation, items, claim="capability-worker"):
    db.execute(text("SELECT pg_catalog.set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant)})
    return (
        db.execute(
            text(
                "SELECT * FROM public.record_claimed_lab_check(CAST(:op AS pg_catalog.uuid), "
                "CAST(:claim AS pg_catalog.text), CAST(:items AS pg_catalog.jsonb))"
            ),
            {"op": str(operation), "claim": claim, "items": json.dumps(items)},
        )
        .mappings()
        .one()
    )


def _state(exc):
    return getattr(exc.value.orig, "sqlstate", None) or getattr(exc.value.orig, "pgcode", None)


def test_lab_result_capability_claim_fence_and_atomic_receipt(admin_session, lab_worker_session, claimed_lab, tenant_b):
    instance, op, items = claimed_lab

    # Claim fence: another owner, empty claim, and cross-tenant all 42501.
    for claim in ("wrong-worker", ""):
        with pytest.raises(DBAPIError) as exc:
            _raw_call(lab_worker_session, instance.tenant_id, op.id, items, claim)
        assert _state(exc) == "42501"
        lab_worker_session.rollback()
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, tenant_b.id, op.id, items)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()

    # Forged or unbounded per-item results refuse 22023 before any write.
    for change in (
        {"id": "wrong"},
        {"label": "wrong"},
        {"weight": 3},
        {"pass": "true"},
        {"pass": 1},
        {"extra": "forbidden"},
        {"actual": []},
        {"expected": {}},
        {"detail": "x" * 4097},
        {"hint": 1},
    ):
        with pytest.raises(DBAPIError) as exc:
            _raw_call(lab_worker_session, instance.tenant_id, op.id, [{**items[0], **change}])
        assert _state(exc) == "22023", change
        lab_worker_session.rollback()
    for bad_items in (None, {}, [], [True], [{"id": "one", "pass": True}]):
        with pytest.raises(DBAPIError) as exc:
            _raw_call(lab_worker_session, instance.tenant_id, op.id, bad_items)
        assert _state(exc) == "22023", bad_items
        lab_worker_session.rollback()

    # Atomic receipt: one intent + fixed audit, invisible before commit.
    row = _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert set(row) == {"submission_id", "score_id", "score", "max_score", "passed"}
    assert (row["score"], row["max_score"], row["passed"]) == (2, 2, True)
    assert (
        admin_session.scalar(
            text("SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"), {"op": op.id}
        )
        == 0
    )

    # Same-claim replay inside the receipt transaction is 55000.
    with lab_worker_session.begin_nested():
        with pytest.raises(DBAPIError) as exc:
            with lab_worker_session.begin_nested():
                _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
        assert _state(exc) == "55000"
    lab_worker_session.commit()

    intent = (
        admin_session.execute(text("SELECT * FROM public.lab_result_deliveries WHERE operation_id=:op"), {"op": op.id})
        .mappings()
        .one()
    )
    assert intent["submission_id"] == row["submission_id"]
    assert intent["score_id"] == row["score_id"]
    assert intent["first_pass"] is True
    assert intent["status"] == "pending"
    assert (intent["score"], intent["max_score"], intent["fraction"], intent["passed"]) == (2, 2, 1, True)
    assert not {"email", "first_name", "last_name", "course_title", "activity_title"} & set(intent)
    audit = (
        admin_session.execute(
            text(
                "SELECT actor_type,actor_id,actor_party_id,source_application,action,entity_type,details "
                "FROM public.audit_events WHERE entity_id=:op AND action='lab_result.record'"
            ),
            {"op": str(op.id)},
        )
        .mappings()
        .one()
    )
    assert tuple(
        audit[k]
        for k in ("actor_type", "actor_id", "actor_party_id", "source_application", "action", "entity_type")
        # source_application keeps the legacy product convention
        # (a0010 aligns the definer; legacy suite is the spec).
    ) == ("system", "academy_lab_worker", None, "dotmac_academy_app", "lab_result.record", "lab_operation")
    assert set(audit["details"]) == {"instance_id", "submission_id", "score_id", "score", "max_score", "passed"}

    # Settled replay is 42501.
    admin_session.execute(text("UPDATE public.lab_operations SET state='succeeded' WHERE id=:op"), {"op": op.id})
    admin_session.commit()
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_worker_cannot_read_pii_delivery_or_learning_tables(lab_worker_session):
    for target in (
        "public.parties",
        "public.party_persons",
        "public.lab_result_deliveries",
        "public.email_outbox",
        "public.notifications",
        "mod_learning.activities",
        "mod_learning.scores",
        "mod_learning.submissions",
    ):
        with pytest.raises(DBAPIError) as exc:
            lab_worker_session.execute(text(f"SELECT * FROM {target} LIMIT 0"))  # noqa: S608 - fixed targets
        assert _state(exc) == "42501", target
        lab_worker_session.rollback()
