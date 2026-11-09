"""Bounded adapter checks; database capability coverage runs on hosted PostgreSQL."""

import json
import math
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from app.services.exceptions import ConflictError
from app.services.lab_result_capability import LabGradeReceipt, record_claimed_lab_check
from legacy_probe.services.test_lab_operations_worker import policy  # noqa: F401 - shared hosted lease fixture


def _call(db, items):
    return record_claimed_lab_check(
        db, tenant_id=uuid4(), operation_id=uuid4(), claimed_by="worker", per_item=items
    )


def test_adapter_normalizes_scalars_and_binds_qualified_call():
    db = MagicMock()
    sid, score_id = uuid4(), uuid4()
    db.execute.return_value.mappings.return_value.one.return_value = {
        "submission_id": sid, "score_id": score_id, "score": 1.0, "max_score": 1.0, "passed": True
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


@pytest.fixture
def claimed_lab(admin_session, lab_worker_session, tenant_a, policy):  # noqa: F811 - imported pytest fixture
    from sqlalchemy import select

    from app.models.lab import LabTemplate
    from app.services import lab_operations
    from legacy_probe.services.test_lab_operations_worker import _seed

    instance, person = _seed(admin_session, tenant_a.id, name="capability", status="active")
    template = admin_session.scalars(select(LabTemplate).where(
        LabTemplate.activity_id == instance.activity_id
    )).one()
    template.checks = [{"id": "one", "type": "command", "label": "One", "weight": 2}]
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
    admin_session.commit()
    claimed = lab_operations.claim_next(lab_worker_session, claimed_by="capability-worker", policy=policy)
    assert claimed.id == operation.id
    lab_worker_session.commit()
    return instance, operation, [{"id": "one", "label": "One", "weight": 2, "pass": True}]


def _raw_call(db, tenant, operation, items, claim="capability-worker"):
    from sqlalchemy import text

    db.execute(text("SELECT pg_catalog.set_config('app.current_tenant', :tenant, true)"),
               {"tenant": str(tenant)})
    return db.execute(text(
        "SELECT * FROM public.record_claimed_lab_check(CAST(:op AS pg_catalog.uuid), "
        "CAST(:claim AS pg_catalog.text), CAST(:items AS pg_catalog.jsonb))"
    ), {"op": str(operation), "claim": claim, "items": json.dumps(items)}).mappings().one()


def _state(exc):
    return getattr(exc.value.orig, "sqlstate", None) or getattr(exc.value.orig, "pgcode", None)


def test_worker_capability_writes_one_atomic_receipt_intent_and_fixed_audit(
    admin_session, lab_worker_session, claimed_lab
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    row = _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert set(row) == {"submission_id", "score_id", "score", "max_score", "passed"}
    assert (row["score"], row["max_score"], row["passed"]) == (2, 2, True)
    # Nothing is visible from another transaction before the owner commits.
    assert admin_session.scalar(text("SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"),
                                {"op": op.id}) == 0
    lab_worker_session.commit()
    intent = admin_session.execute(text(
        "SELECT * FROM public.lab_result_deliveries WHERE operation_id=:op"
    ), {"op": op.id}).mappings().one()
    assert intent["submission_id"] == row["submission_id"]
    assert intent["score_id"] == row["score_id"]
    assert intent["first_pass"] is True
    assert intent["status"] == "pending"
    assert (intent["score"], intent["max_score"], intent["fraction"], intent["passed"]) == (2, 2, 1, True)
    assert not {"email", "first_name", "last_name", "course_title", "activity_title"} & set(intent)
    audit = admin_session.execute(text(
        "SELECT actor_type,actor_id,actor_party_id,source_application,action,entity_type,details "
        "FROM public.audit_events WHERE entity_id=:op AND action='lab_result.record'"
    ), {"op": str(op.id)}).mappings().one()
    assert tuple(audit[k] for k in ("actor_type", "actor_id", "actor_party_id", "source_application",
                                    "action", "entity_type")) == (
        "system", "academy_lab_worker", None, "dotmac_academy_app", "lab_result.record", "lab_operation")
    assert set(audit["details"]) == {"instance_id", "submission_id", "score_id", "score", "max_score", "passed"}


@pytest.mark.parametrize("claim", ["wrong-worker", ""])
def test_claim_fence_refuses_another_owner(lab_worker_session, claimed_lab, claim):
    instance, op, items = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, items, claim)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_cross_tenant_and_expired_claim_refused(admin_session, lab_worker_session, claimed_lab, tenant_b):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, tenant_b.id, op.id, items)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()
    admin_session.execute(text("UPDATE public.lab_operations SET heartbeat_at=pg_catalog.clock_timestamp() "
                               "- interval '1 day' WHERE id=:op"), {"op": op.id})
    admin_session.commit()
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_same_claim_replay_is_55000_and_settled_replay_is_42501(
    admin_session, lab_worker_session, claimed_lab
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    with lab_worker_session.begin_nested():
        with pytest.raises(DBAPIError) as exc:
            with lab_worker_session.begin_nested():
                _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
        assert _state(exc) == "55000"
    lab_worker_session.commit()
    admin_session.execute(text("UPDATE public.lab_operations SET state='succeeded' WHERE id=:op"), {"op": op.id})
    admin_session.commit()
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


@pytest.mark.parametrize("change", [
    {"id": "wrong"}, {"label": "wrong"}, {"weight": 3}, {"pass": "true"}, {"pass": 1},
    {"extra": "forbidden"}, {"actual": []}, {"expected": {}}, {"detail": "x" * 4097},
    {"hint": 1},
])
def test_per_item_validation_refuses_forged_or_unbounded_results(lab_worker_session, claimed_lab, change):
    instance, op, items = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, [{**items[0], **change}])
    assert _state(exc) == "22023"
    lab_worker_session.rollback()


@pytest.mark.parametrize("items", [None, {}, [], [True], [{"id": "one", "pass": True}]])
def test_per_item_shape_and_required_fields(lab_worker_session, claimed_lab, items):
    instance, op, _ = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert _state(exc) == "22023"
    lab_worker_session.rollback()


@pytest.mark.parametrize("target", [
    "public.parties", "public.party_persons", "public.lab_result_deliveries", "public.email_outbox",
    "public.notifications", "mod_learning.activities", "mod_learning.scores", "mod_learning.submissions",
])
def test_worker_cannot_read_pii_delivery_or_learning_tables(lab_worker_session, target):
    from sqlalchemy import text

    with pytest.raises(DBAPIError) as exc:
        lab_worker_session.execute(text(f"SELECT * FROM {target} LIMIT 0"))  # noqa: S608 - fixed target cases
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_function_security_and_worker_column_update_map(admin_session, lab_worker_session, claimed_lab):
    from sqlalchemy import text

    outer = "public.record_claimed_lab_check(uuid,text,jsonb)"
    inner = ("mod_learning.record_lab_result_v1(uuid,uuid,uuid,uuid,jsonb,"
             "double precision,double precision,double precision,boolean,jsonb)")
    activation = "public.activate_eligible_facilitator_nominations(uuid,uuid,uuid,uuid,integer,uuid)"
    for signature, definer, roles in (
        (outer, True, {"app_admin", "academy_lab_worker"}),
        (inner, False, {"app_admin"}),
        (activation, True, {"app_admin", "app_user", "platform_api"}),
        ("public._academy_py_float_add_v1(float8,float8)", False, {"app_admin"}),
        ("public._academy_py_json_float_v1(numeric)", False, {"app_admin"}),
    ):
        row = admin_session.execute(text(
            "SELECT p.prosecdef,p.proconfig,p.provolatile,p.proisstrict,p.proparallel,"
            "pg_catalog.pg_get_userbyid(p.proowner) AS owner "
            "FROM pg_catalog.pg_proc p WHERE p.oid=pg_catalog.to_regprocedure(:sig)"
        ), {"sig": signature}).mappings().one()
        assert row["prosecdef"] is definer
        assert row["owner"] == "app_admin"
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        if "_academy_py_" in signature:
            assert (row["provolatile"], row["proisstrict"], row["proparallel"]) == ("i", True, "u")
        grantees = admin_session.scalars(text(
            "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(acl.grantee) END "
            "FROM pg_catalog.pg_proc p, LATERAL pg_catalog.aclexplode(p.proacl) acl "
            "WHERE p.oid=pg_catalog.to_regprocedure(:sig) AND acl.privilege_type='EXECUTE'"
        ), {"sig": signature}).all()
        assert set(grantees) == roles
    assert not lab_worker_session.scalar(text(
        "SELECT pg_catalog.has_schema_privilege(current_user,'mod_learning','USAGE')"
    ))
    columns = set(admin_session.scalars(text(
        "SELECT column_name FROM information_schema.column_privileges "
        "WHERE table_schema='public' AND table_name='lab_instances' "
        "AND grantee='academy_lab_worker' AND privilege_type='UPDATE'"
    )))
    assert columns == {"status", "consoles", "error", "started_at", "last_active_at", "runtime_presence", "updated_at"}
    instance, _, _ = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        lab_worker_session.execute(text("UPDATE public.lab_instances SET person_id=person_id WHERE id=:id"),
                                   {"id": instance.id})
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_pg_temp_domain_and_relation_traps_do_not_capture_outer_types(lab_worker_session, claimed_lab):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    # Unqualified type resolution searches pg_temp before implicit pg_catalog.
    for name, base in (("uuid", "pg_catalog.uuid"), ("text", "pg_catalog.text"),
                       ("bool", "pg_catalog.bool"), ("float8", "pg_catalog.float8"),
                       ("jsonb", "pg_catalog.jsonb")):
        lab_worker_session.execute(text(f"CREATE DOMAIN pg_temp.{name} AS {base} CHECK (false)"))
    lab_worker_session.execute(text("CREATE TEMP TABLE lab_operations (id pg_catalog.uuid)"))
    try:
        receipt = _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
        assert receipt["passed"] is True
    finally:
        lab_worker_session.rollback()


def _assert_safe_function_source(source):
    import re

    # Strip comments, then require every RAISE to use a literal message and code.
    source = re.sub(r"--[^\n]*|/\*.*?\*/", "", source, flags=re.S)
    assert not re.search(r"\bEXECUTE\b|\bformat\s*\(", source, re.I)
    for match in re.finditer(r"\bRAISE\b(.*?);", source, re.I | re.S):
        clause = match.group(1)
        assert re.fullmatch(
            r"\s*EXCEPTION\s+USING\s+ERRCODE\s*=\s*'[A-Z0-9]{5}'\s*,"
            r"\s*MESSAGE\s*=\s*'[^'%]*'\s*", clause, re.I | re.S
        ), clause


def test_raise_lint_reads_all_installed_capability_function_bodies(admin_session):
    from sqlalchemy import text

    sources = admin_session.scalars(text(
        "SELECT p.prosrc FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
        "WHERE (n.nspname='public' AND p.proname IN ('record_claimed_lab_check','_academy_py_float_add_v1','_academy_py_json_float_v1')) "
        "OR (n.nspname='mod_learning' AND p.proname='record_lab_result_v1')"
    )).all()
    assert len(sources) == 4
    for source in sources:
        _assert_safe_function_source(source)


@pytest.mark.parametrize("source", [
    "RAISE EXCEPTION 'learner %', private_email;", "EXECUTE private_sql;",
    "PERFORM format('secret %s', private_email);",
    "RAISE EXCEPTION USING ERRCODE='22023', MESSAGE=private_email;",
    "RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixed', DETAIL=private_email;",
])
def test_raise_lint_negative_canaries(source):
    with pytest.raises(AssertionError):
        _assert_safe_function_source(source)


@pytest.mark.parametrize("sqlstate,category", [("42501", "42501"), ("23505", "23505"),
                                             ("secret", "unknown"), ("P0001", "other")])
def test_refusal_metric_has_bounded_labels_and_safe_log(monkeypatch, caplog, sqlstate, category):
    from app.services import lab_result_capability as capability

    counter = MagicMock()
    monkeypatch.setattr(capability, "LAB_RESULT_REFUSALS", counter)
    original = Exception("private learner address")
    original.sqlstate = sqlstate
    db = MagicMock()
    db.execute.side_effect = DBAPIError("private statement", {"private": "parameters"}, original)
    with pytest.raises(ConflictError) as exc:
        _call(db, [])
    assert str(exc.value) == "lab check could not be recorded"
    assert exc.value.__suppress_context__ is True
    counter.labels.assert_called_once_with(sqlstate=category)
    counter.labels.return_value.inc.assert_called_once_with()
    assert "class=DBAPIError" in caplog.text
    assert "category=" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("mode,expected_state", [("intent", "55000"), ("ledger", "23505")])
def test_mid_call_intent_conflict_and_genuine_learning_unique_violation(
    admin_session, lab_worker_session, claimed_lab, mode, expected_state
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    # The test-only trigger plants a conflict after the outer replay precheck.
    # Its DDL is rolled back with the call; it never leaves a persistent fixture.
    lab_worker_session.rollback()
    admin_session.rollback()
    with admin_session.begin_nested():
        admin_session.execute(text("""
            CREATE FUNCTION pg_temp.lab_result_conflict_canary() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_ARGV[0] = 'ledger' THEN
                    RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='planted learning unique violation';
                END IF;
                INSERT INTO public.lab_result_deliveries (
                    tenant_id,operation_id,instance_id,person_id,activity_id,course_id,
                    submission_id,score_id,first_completion,certificate_created,first_pass,
                    score,max_score,fraction,passed
                ) SELECT operation.tenant_id,operation.id,instance.id,instance.person_id,
                         instance.activity_id,activity.course_id,NEW.submission_id,NEW.id,
                         false,false,false,NEW.score,NEW.max_score,NEW.fraction,NEW.passed
                  FROM public.lab_operations operation
                  JOIN public.lab_instances instance ON instance.id=operation.instance_id
                  JOIN mod_learning.activities activity ON activity.id=instance.activity_id
                 WHERE operation.id=TG_ARGV[1]::pg_catalog.uuid;
                RETURN NEW;
            END;
            $$
        """))
        # Values are fixed mode cases and a generated UUID, never learner data.
        admin_session.execute(text(
            "CREATE TRIGGER lab_result_conflict_canary BEFORE INSERT ON mod_learning.scores "
            "FOR EACH ROW EXECUTE FUNCTION pg_temp.lab_result_conflict_canary('" + mode + "','" + str(op.id) + "')"
        ))
        with pytest.raises(DBAPIError) as exc:
            with admin_session.begin_nested():
                _raw_call(admin_session, instance.tenant_id, op.id, items)
        assert _state(exc) == expected_state
        assert admin_session.scalar(text(
            "SELECT count(*) FROM mod_learning.submissions WHERE tenant_id=:tenant"
        ), {"tenant": instance.tenant_id}) == 0
        assert admin_session.scalar(text(
            "SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"
        ), {"op": op.id}) == 0
        admin_session.execute(text("DROP TRIGGER lab_result_conflict_canary ON mod_learning.scores"))
        admin_session.execute(text("DROP FUNCTION pg_temp.lab_result_conflict_canary()"))
    admin_session.rollback()


@pytest.mark.parametrize("template_threshold,activity_threshold,expected", [
    (None, 0, True), (None, 0.5, False), (0.5, 0, False), (0, 0.5, True),
])
def test_sql_uses_template_threshold_then_activity_fallback(
    admin_session, lab_worker_session, claimed_lab, template_threshold, activity_threshold, expected
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    admin_session.execute(text("UPDATE mod_learning.activities SET pass_threshold=:threshold WHERE id=:id"),
                          {"threshold": activity_threshold, "id": instance.activity_id})
    admin_session.execute(text("UPDATE public.lab_templates SET limits=CAST(:limits AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"limits": json.dumps({"pass_threshold": template_threshold}), "id": instance.activity_id})
    admin_session.commit()
    row = _raw_call(lab_worker_session, instance.tenant_id, op.id, [{**items[0], "pass": False}])
    assert row["passed"] is expected
    lab_worker_session.rollback()
    assert admin_session.scalar(text(
        "SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"
    ), {"op": op.id}) == 0


def test_per_item_ordinal_order_cannot_be_reordered(admin_session, lab_worker_session, claimed_lab):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    checks = [{"id": "one", "label": "One", "weight": 2}, {"id": "two", "label": "Two", "weight": 3}]
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"checks": json.dumps(checks), "id": instance.activity_id})
    admin_session.commit()
    reversed_items = [{**checks[1], "pass": True}, items[0]]
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, reversed_items)
    assert _state(exc) == "22023"
    lab_worker_session.rollback()


def test_adapter_payload_byte_limit_is_checked_before_database():
    db = MagicMock()
    with pytest.raises(ConflictError, match="invalid lab check payload"):
        _call(db, [{"actual": "x" * 4096}] * 65)
    db.execute.assert_not_called()


def test_sql_payload_byte_limit_is_refused(admin_session, lab_worker_session, claimed_lab):
    from sqlalchemy import text

    instance, op, _ = claimed_lab
    checks = [{"id": str(i), "label": str(i), "weight": 1} for i in range(17)]
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"checks": json.dumps(checks), "id": instance.activity_id})
    admin_session.commit()
    # Every observation meets the individual cap; only the combined bytes exceed 256KiB.
    oversized = [{**check, "pass": True, **dict.fromkeys(("actual", "expected", "detail", "hint"), "x" * 4096)}
                 for check in checks]
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, oversized)
    assert _state(exc) == "22023"
    lab_worker_session.rollback()


@pytest.mark.parametrize("weights,passes,threshold", [
    ([0.1, 0.2, 0.3], [True, True, False], 0.5),
    ([2, 1, 0.1, 0.2, 0.3], [True, False, True, True, True], 0.7),
    ([2, 0.1, 0.2], [True, True, False], 2.1 / 2.3),
    ([10**16, 1, 1], [True, True, True], 1.0),
    ([2**53, 1], [True, False], 1.0),
    ([2**53, 1], [True, True], 1.0),
    ([2**63 - 1, 1, -(2**63), 0.1, 0.2, 0.3], [True] * 6, 1.0),
    ([5e-324, 1.0], [True, False], 5e-324),
    ([5e-324, 2.0], [True, False], 0.0),
    ([-0.1], [False], 0.0),
    ([1e16, 1.0, 1.0], [True, True, True], 1.0),
    ([0.1, 0.2, 0.3], [False, False, False], 0.0),
    ([0.1, 0.2, 2**63, -(2**63), 0.3, 0.4], [True] * 6, 1.0),
    ([2**1023, -(2**1023), 1e-308], [True, False, False], 1.0),
    ([2**1023, 0.1, 2**1023], [True, True, True], 1.0),
    ([], [], 0.0),
])
def test_hosted_numeric_receipt_matches_actual_python_check_engine(
    admin_session, lab_worker_session, claimed_lab, weights, passes, threshold
):
    from sqlalchemy import select, text

    from app.models.lab import LabTemplate
    from app.services import lab_lifecycle
    from app.services.checks.engine import run_checks
    from app.services.labengine.interface import ExecResult, LabHandle

    instance, op, _ = claimed_lab
    checks = [{"id": str(i), "label": str(i), "weight": weight, "type": "command",
               "node": "client", "command": str(i)} for i, weight in enumerate(weights)]
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb), "
                               "limits=CAST(:limits AS pg_catalog.jsonb) WHERE activity_id=:id"),
                          {"checks": json.dumps(checks), "limits": json.dumps({"pass_threshold": threshold}),
                           "id": instance.activity_id})
    admin_session.commit()
    engine = MagicMock()
    engine.exec.side_effect = lambda _handle, _node, args: ExecResult("", "", 0 if passes[int(args[-1])] else 1)
    handle = LabHandle(instance.instance_name, {}, {}, {})
    template = lab_worker_session.scalars(select(LabTemplate).where(
        LabTemplate.activity_id == instance.activity_id
    )).one()
    expected = run_checks(template.checks, engine, handle, instance.seed)
    receipt = lab_lifecycle.grade(
        lab_worker_session, instance, engine, template,
        handle, operation_id=op.id, claimed_by="capability-worker",
    )
    assert receipt.score == float(expected["score"])
    assert receipt.max_score == float(expected["max_score"])
    fraction = expected["score"] / expected["max_score"] if expected["max_score"] else 0
    assert receipt.passed == (expected["max_score"] > 0 and fraction >= threshold)
    # Only the migration-authority fixture inspects private persisted facts.
    # Committing this isolated tenant fixture does not widen worker privileges.
    lab_worker_session.commit()
    persisted = admin_session.execute(text(
        "SELECT intent.score,intent.max_score,intent.fraction AS intent_fraction,intent.passed,"
        "ledger.score AS ledger_score,ledger.max_score AS ledger_max_score,"
        "ledger.fraction AS ledger_fraction,ledger.passed AS ledger_passed "
        "FROM public.lab_result_deliveries intent JOIN mod_learning.scores ledger "
        "ON ledger.tenant_id=intent.tenant_id AND ledger.id=intent.score_id "
        "WHERE intent.tenant_id=:tenant AND intent.operation_id=:operation "
        "AND intent.score_id=:score AND intent.submission_id=:submission"
    ), {"tenant": instance.tenant_id, "operation": op.id, "score": receipt.score_id,
        "submission": receipt.submission_id}).mappings().one()

    def assert_exact_float(actual, oracle):
        assert actual == oracle or (math.isnan(actual) and math.isnan(oracle))
        if oracle == 0:
            assert math.copysign(1.0, actual) == math.copysign(1.0, oracle)

    for key in ("score", "ledger_score"):
        assert_exact_float(persisted[key], float(expected["score"]))
    for key in ("max_score", "ledger_max_score"):
        assert_exact_float(persisted[key], float(expected["max_score"]))
    for key in ("intent_fraction", "ledger_fraction"):
        assert_exact_float(persisted[key], float(fraction))
    assert persisted["passed"] == persisted["ledger_passed"] == receipt.passed



def test_worker_cannot_call_inner_learning_capability(lab_worker_session, claimed_lab):
    from sqlalchemy import text

    instance, _, _ = claimed_lab
    with pytest.raises(DBAPIError) as exc:
        lab_worker_session.execute(text(
            "SELECT * FROM mod_learning.record_lab_result_v1("
            "CAST(:tenant AS pg_catalog.uuid),CAST(:person AS pg_catalog.uuid),"
            "CAST(:activity AS pg_catalog.uuid),CAST(:instance AS pg_catalog.uuid),"
            "'{}'::pg_catalog.jsonb,1::pg_catalog.float8,1::pg_catalog.float8,1::pg_catalog.float8,"
            "true::pg_catalog.bool,'[]'::pg_catalog.jsonb)"
        ), {"tenant": instance.tenant_id, "person": instance.person_id,
            "activity": instance.activity_id, "instance": instance.id})
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


def test_receipt_then_rollback_reveals_no_delivery_or_learning_reads(
    admin_session, lab_worker_session, claimed_lab
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    receipt = _raw_call(lab_worker_session, instance.tenant_id, op.id, items)
    assert len(receipt) == 5
    for statement in (
        "SELECT * FROM public.lab_result_deliveries LIMIT 0",
        "SELECT * FROM mod_learning.scores LIMIT 0",
        "SELECT * FROM public.parties LIMIT 0",
    ):
        with pytest.raises(DBAPIError) as exc:
            with lab_worker_session.begin_nested():
                lab_worker_session.execute(text(statement))
        assert _state(exc) == "42501"
    lab_worker_session.rollback()
    assert admin_session.scalar(text(
        "SELECT count(*) FROM mod_learning.submissions WHERE tenant_id=:tenant"
    ), {"tenant": instance.tenant_id}) == 0
    assert admin_session.scalar(text(
        "SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"
    ), {"op": op.id}) == 0


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
    check = {"id": "number", "type": "command", "node": "client", "command": "read",
             "assert": {"jsonpath": "$.value", "equals": 1.5}}
    result = run_checks([check], engine, LabHandle("float-check", {}, {}, {}), {})
    assert result["per_check"][0]["pass"] is True
    db = MagicMock()
    db.execute.return_value.mappings.return_value.one.return_value = {
        "submission_id": uuid4(), "score_id": uuid4(), "score": 1.0, "max_score": 1.0, "passed": True,
    }
    _call(db, result["per_check"])
    payload = json.loads(db.execute.call_args.args[1]["items"])
    assert payload[0]["actual"] == "1.5"
    assert payload[0]["expected"] == "1.5"
    assert payload[0]["pass"] is True


@pytest.mark.parametrize("signature,args", [
    ("public._academy_py_float_add_v1", "1::pg_catalog.float8,2::pg_catalog.float8"),
    ("public._academy_py_json_float_v1", "1::pg_catalog.numeric"),
])
def test_worker_cannot_execute_private_numeric_helpers(lab_worker_session, signature, args):
    from sqlalchemy import text

    with pytest.raises(DBAPIError) as exc:
        lab_worker_session.execute(text(f"SELECT {signature}({args})"))
    assert _state(exc) == "42501"
    lab_worker_session.rollback()


@pytest.mark.parametrize("weight_text", ["0.1000000000000000000001", "1e-400", "-1e-400"])
def test_hosted_noncanonical_decimal_weight_uses_actual_python_json_decoding(
    admin_session, lab_worker_session, claimed_lab, weight_text
):
    from sqlalchemy import select, text

    from app.models.lab import LabTemplate
    from app.services import lab_lifecycle
    from app.services.checks.engine import run_checks
    from app.services.labengine.interface import ExecResult, LabHandle

    instance, op, _ = claimed_lab
    checks_text = ('[{"id":"one","label":"One","weight":' + weight_text
                   + ',"type":"command","node":"client","command":"0"}]')
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb), "
                               "limits=CAST(:limits AS pg_catalog.jsonb) WHERE activity_id=:id"),
                          {"checks": checks_text, "limits": '{"pass_threshold":0.5}', "id": instance.activity_id})
    admin_session.commit()
    template = lab_worker_session.scalars(select(LabTemplate).where(
        LabTemplate.activity_id == instance.activity_id
    )).one()
    engine = MagicMock()
    engine.exec.return_value = ExecResult("", "", 0)
    handle = LabHandle(instance.instance_name, {}, {}, {})
    expected = run_checks(template.checks, engine, handle, instance.seed)
    receipt = lab_lifecycle.grade(lab_worker_session, instance, engine, template, handle,
                                  operation_id=op.id, claimed_by="capability-worker")
    assert receipt.score == float(expected["score"])
    assert receipt.max_score == float(expected["max_score"])
    assert receipt.passed is (expected["max_score"] > 0)
    lab_worker_session.rollback()


def test_payload_int_float_spelling_cannot_choose_aggregation_mode(
    admin_session, lab_worker_session, claimed_lab
):
    from sqlalchemy import select, text

    from app.models.lab import LabTemplate
    from app.services.checks.engine import run_checks
    from app.services.labengine.interface import ExecResult, LabHandle

    instance, op, _ = claimed_lab
    checks = [{"id": str(i), "label": str(i), "weight": weight, "type": "command",
               "node": "client", "command": "0"} for i, weight in enumerate((1, 1e-16, 1e-16))]
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"checks": json.dumps(checks), "id": instance.activity_id})
    admin_session.commit()
    template = lab_worker_session.scalars(select(LabTemplate).where(
        LabTemplate.activity_id == instance.activity_id
    )).one()
    engine = MagicMock()
    engine.exec.return_value = ExecResult("", "", 0)
    result = run_checks(template.checks, engine, LabHandle(instance.instance_name, {}, {}, {}), instance.seed)
    payload = result["per_check"]
    payload[0]["weight"] = 1.0
    # Raw SQL bypasses adapter normalization but cannot select a different sum mode.
    for item in payload:
        for key in ("actual", "expected"):
            item[key] = str(item[key])
    receipt = _raw_call(lab_worker_session, instance.tenant_id, op.id, payload)
    assert receipt["score"] == float(result["score"])
    assert receipt["max_score"] == float(result["max_score"])
    lab_worker_session.rollback()


@pytest.mark.parametrize("stored_weight", [None, "1", True, [], {}])
def test_raw_sql_refuses_non_numeric_stored_template_weight(
    admin_session, lab_worker_session, claimed_lab, stored_weight
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    checks = [{"id": "one", "label": "One", "weight": stored_weight, "type": "command"}]
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"checks": json.dumps(checks), "id": instance.activity_id})
    admin_session.commit()
    # A numeric payload cannot make an ungradable stored template authoritative.
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, [{**items[0], "weight": 1}])
    assert _state(exc) == "22023"
    lab_worker_session.rollback()


@pytest.mark.parametrize("submitted_weight", [10**309, 1])
def test_raw_sql_refuses_stored_decimal_weight_decoding_to_infinity(
    admin_session, lab_worker_session, claimed_lab, submitted_weight
):
    from sqlalchemy import text

    instance, op, items = claimed_lab
    checks_text = ('[{"id":"one","label":"One","weight":' + str(10**309) + '.0,"type":"command"}]')
    admin_session.execute(text("UPDATE public.lab_templates SET checks=CAST(:checks AS pg_catalog.jsonb) "
                               "WHERE activity_id=:id"),
                          {"checks": checks_text, "id": instance.activity_id})
    admin_session.commit()
    with pytest.raises(DBAPIError) as exc:
        _raw_call(lab_worker_session, instance.tenant_id, op.id, [{**items[0], "weight": submitted_weight}])
    assert _state(exc) == "22023"
    lab_worker_session.rollback()
