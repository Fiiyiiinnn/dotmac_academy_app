"""Real ``academy_lab_worker`` privilege matrix and worker-consequence integration test.

Skips cleanly when ``TEST_LAB_WORKER_DATABASE_URL`` is unset (see the
``lab_worker_session`` fixture in ``tests/conftest.py``) — this is the one test
in the suite that requires a real, live-verified ``academy_lab_worker`` role rather than
the migration/superuser connection ``admin_session`` provides, because its
whole point is to catch an incomplete or overbroad grant that a superuser
connection would never surface.

Uses a stub (``MagicMock``) containerlab engine only — never a real one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlalchemy import select, text

from app.models.assessment import Score, Submission
from app.models.email_outbox import EmailOutbox
from app.models.lab import LabOperation, LabTemplate
from app.models.learning_event import LearningEvent
from app.models.notification import Notification
from app.services import lab_jobs, lab_operations
from app.services.labengine.interface import ExecResult, LabHandle
from legacy_probe.services.test_lab_operations_worker import _seed

# (table, granted privileges, privileges that must NOT be granted)
SELECT_ONLY_TABLES = ("lab_templates", "platform_settings")
LAB_INSTANCES_PRIVILEGES = ("SELECT",)
LAB_INSTANCE_UPDATE_COLUMNS = {
    "status", "consoles", "error", "started_at", "last_active_at", "runtime_presence", "updated_at",
}


def _stub_engine(instance_name: str) -> MagicMock:
    engine = MagicMock()
    engine.deploy.return_value = LabHandle(
        instance_name=instance_name,
        nodes={"client": f"clab-{instance_name}-client"},
        mgmt={"client": "10.0.0.5"},
        kinds={"client": "linux"},
    )
    engine.destroy.return_value = None  # absent is already destroyed
    engine.exec.return_value = ExecResult("2 packets transmitted, 2 received", "", 0)
    engine.inventory.return_value = set()  # nothing running per the runtime
    return engine


def _has_privilege(session, table: str, privilege: str) -> bool:
    return bool(
        session.scalar(
            text("SELECT has_table_privilege('academy_lab_worker', :table, :priv)").bindparams(
                table=table, priv=privilege
            )
        )
    )


def _has_column_privilege(session, role: str, table: str, column: str, privilege: str) -> bool:
    return bool(
        session.scalar(
            text(
                "SELECT has_column_privilege(:role, :table, :column, :priv)"
            ).bindparams(role=role, table=table, column=column, priv=privilege)
        )
    )


def test_lab_worker_privilege_matrix_and_worker_consequences(
    admin_session, lab_worker_session, tenant_a
):
    instance, person = _seed(admin_session, tenant_a.id, name="privileges")
    template = admin_session.scalars(
        select(LabTemplate)
        .where(LabTemplate.tenant_id == tenant_a.id)
        .where(LabTemplate.activity_id == instance.activity_id)
    ).one()
    template.checks = [
        {
            "id": "reach",
            "type": "probe",
            "node": "client",
            "probe": {"kind": "ping", "target": "10.0.0.1", "count": 2, "min_success": 1},
            "weight": 1,
        }
    ]
    lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.commit()

    # --- explicit ACL matrix -----------------------------------------------
    expected_direct_acl = {
        "lab_operations": {"INSERT", "SELECT", "UPDATE"},
        "lab_instances": {"SELECT"},
        "lab_templates": {"SELECT"},
        "platform_settings": {"SELECT"},
    }
    direct_acl = {
        table: set(privileges)
        for table, privileges in lab_worker_session.execute(
            text(
                """SELECT c.relname, array_agg(DISTINCT p.privilege_type ORDER BY p.privilege_type)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN LATERAL aclexplode(
                    COALESCE(c.relacl, acldefault('r', c.relowner))
                ) p
                WHERE n.nspname = 'public'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND pg_get_userbyid(p.grantee) = 'academy_lab_worker'
                GROUP BY c.relname"""
            )
        ).all()
    }
    assert direct_acl == expected_direct_acl
    assert not lab_worker_session.scalar(
        text("SELECT has_schema_privilege('academy_lab_worker', 'public', 'CREATE')")
    )
    assert lab_worker_session.scalar(
        text("SELECT NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = 'academy_lab_worker'::regrole OR m.roleid = 'academy_lab_worker'::regrole)")
    )

    for table in SELECT_ONLY_TABLES:
        assert _has_privilege(lab_worker_session, table, "SELECT"), f"expected SELECT on {table}"
        assert not _has_privilege(
            lab_worker_session, table, "INSERT"
        ), f"academy_lab_worker should not have INSERT on {table}"
        for privilege in ("UPDATE", "DELETE"):
            assert not _has_privilege(lab_worker_session, table, privilege)
        assert not lab_worker_session.scalar(
            text("SELECT has_any_column_privilege('academy_lab_worker', :table, 'UPDATE')"),
            {"table": table},
        )

    assert not lab_worker_session.scalar(text(
        "SELECT has_schema_privilege('academy_lab_worker', 'mod_learning', 'USAGE')"
    ))
    # Use catalog identities to inspect rights without needing schema USAGE.
    assert lab_worker_session.scalar(text("""
        SELECT NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='mod_learning' AND c.relkind='r'
            AND (has_table_privilege('academy_lab_worker', c.oid, 'SELECT,INSERT,UPDATE,DELETE,REFERENCES,TRIGGER,TRUNCATE')
                 OR has_any_column_privilege('academy_lab_worker', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES'))
        )
    """))
    for table in ("email_outbox", "notifications", "lab_result_deliveries", "party_persons", "parties"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "REFERENCES"):
            assert not _has_privilege(lab_worker_session, table, privilege)
        assert not lab_worker_session.scalar(text(
            "SELECT has_any_column_privilege('academy_lab_worker', :table, 'SELECT,INSERT,UPDATE,REFERENCES')"
        ), {"table": table})
    columns = lab_worker_session.scalars(text("""
        SELECT attname FROM pg_catalog.pg_attribute
        WHERE attrelid='public.lab_instances'::regclass AND attnum>0 AND NOT attisdropped
    """)).all()
    for column in columns:
        assert _has_column_privilege(
            lab_worker_session, "academy_lab_worker", "lab_instances", column, "UPDATE"
        ) == (column in LAB_INSTANCE_UPDATE_COLUMNS)
        assert not _has_column_privilege(
            lab_worker_session, "academy_lab_worker", "lab_instances", column, "INSERT"
        )

    # Facilitator activation is exposed only as a policy-enforcing capability.
    # The worker cannot inspect or mutate nomination/requirement/audit rows.
    for table in ("facilitator_nominations", "facilitator_requirements"):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not _has_privilege(lab_worker_session, table, privilege)
        assert not lab_worker_session.scalar(
            text("SELECT has_any_column_privilege('academy_lab_worker', :table, 'UPDATE')"),
            {"table": table},
        )
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert not _has_privilege(lab_worker_session, "audit_events", privilege)
    assert not lab_worker_session.scalar(
        text("SELECT has_any_column_privilege('academy_lab_worker', 'audit_events', 'INSERT')")
    )

    capability = "public.activate_eligible_facilitator_nominations(uuid,uuid,uuid,uuid,integer,uuid)"
    assert not lab_worker_session.scalar(
        text("SELECT has_function_privilege('academy_lab_worker', :capability, 'EXECUTE')"),
        {"capability": capability},
    )
    capability_security = lab_worker_session.execute(
        text(
            """SELECT p.prosecdef, p.proconfig, p.provolatile, p.proparallel,
                      p.proisstrict, pg_get_userbyid(p.proowner),
                      pg_get_userbyid(c.relowner) AS nominations_owner,
                      NOT EXISTS (
                          SELECT 1 FROM aclexplode(p.proacl) acl WHERE acl.grantee = 0
                      ) AS public_revoked,
                      (
                          SELECT array_agg(pg_get_userbyid(acl.grantee) ORDER BY pg_get_userbyid(acl.grantee))
                            FROM aclexplode(p.proacl) acl
                           WHERE acl.privilege_type = 'EXECUTE'
                             AND acl.grantee <> p.proowner
                      ) AS execute_grantees
                 FROM pg_proc p
                 JOIN pg_namespace n ON n.oid = p.pronamespace
                 JOIN pg_class c ON c.oid = 'public.facilitator_nominations'::regclass
                WHERE n.nspname = 'public'
                  AND p.oid = CAST(:capability AS regprocedure)"""
        ),
        {"capability": capability},
    ).one()
    assert capability_security.prosecdef is True
    assert capability_security.proconfig == ["search_path=pg_catalog, pg_temp"]
    assert capability_security.provolatile == "v"
    assert capability_security.proparallel == "u"
    assert capability_security.proisstrict is False
    assert capability_security.pg_get_userbyid == capability_security.nominations_owner
    assert capability_security.public_revoked is True
    assert capability_security.execute_grantees == ["app_user", "platform_api"]

    for privilege in LAB_INSTANCES_PRIVILEGES:
        assert _has_privilege(
            lab_worker_session, "lab_instances", privilege
        ), f"expected {privilege} on lab_instances"

    # --- runtime_presence column-level ACL ---------------------------------
    # The worker's column UPDATE includes runtime_presence; app_user's
    # INSERT grant is column-scoped and must NOT include it, and platform_api
    # must never get write access to it either.
    for privilege in ("SELECT", "UPDATE"):
        assert _has_column_privilege(
            lab_worker_session, "academy_lab_worker", "lab_instances", "runtime_presence", privilege
        ), f"expected academy_lab_worker to have {privilege} on lab_instances.runtime_presence"
    for role in ("app_user", "platform_api"):
        for privilege in ("INSERT", "UPDATE"):
            assert not _has_column_privilege(
                lab_worker_session, role, "lab_instances", "runtime_presence", privilege
            ), f"{role} must not have {privilege} on lab_instances.runtime_presence"

    # --- claimed_host/claimed_epoch column-level ACL (migration 0059) ------
    # academy_lab_worker's grant is table-wide (see 0055, restated by 0059),
    # so it must cover these two structural claim-ownership columns exactly
    # like every other worker-owned one; app_user's INSERT grant is
    # column-scoped and must NOT include either, and platform_api must never
    # get write access to either one.
    for privilege in ("SELECT", "UPDATE"):
        for column in ("claimed_host", "claimed_epoch"):
            assert _has_column_privilege(
                lab_worker_session, "academy_lab_worker", "lab_operations", column, privilege
            ), f"expected academy_lab_worker to have {privilege} on lab_operations.{column}"
    for role in ("app_user", "platform_api"):
        for privilege in ("INSERT", "UPDATE"):
            for column in ("claimed_host", "claimed_epoch"):
                assert not _has_column_privilege(
                    lab_worker_session, role, "lab_operations", column, privilege
                ), f"{role} must not have {privilege} on lab_operations.{column}"

    # --- origin/runtime_precondition column-level ACL (migration 0060) -----
    # academy_lab_worker's grant is table-wide (see 0055, restated by
    # 0059/0060), so it must cover these two conditional-operation columns
    # exactly like every other worker-owned one; app_user's INSERT grant is
    # column-scoped and must NOT include either, and platform_api must never
    # get write access to either one.
    for privilege in ("SELECT", "UPDATE"):
        for column in ("origin", "runtime_precondition"):
            assert _has_column_privilege(
                lab_worker_session, "academy_lab_worker", "lab_operations", column, privilege
            ), f"expected academy_lab_worker to have {privilege} on lab_operations.{column}"
    for role in ("app_user", "platform_api"):
        for privilege in ("INSERT", "UPDATE"):
            for column in ("origin", "runtime_precondition"):
                assert not _has_column_privilege(
                    lab_worker_session, role, "lab_operations", column, privilege
                ), f"{role} must not have {privilege} on lab_operations.{column}"

    # --- deploy, then a passing check, through the real worker session --
    engine = _stub_engine(instance.instance_name)
    assert lab_jobs.drain_once(lab_worker_session, engine) == 1
    lab_worker_session.commit()

    # The real academy_lab_worker role CAN claim using claimed_host/
    # claimed_epoch: drain_once's default identity path set both on the
    # claim above, through this exact worker-session connection, and a
    # terminal settlement retains them as audit provenance.
    admin_session.rollback()
    deployed_operation = admin_session.scalars(
        select(LabOperation)
        .where(LabOperation.instance_id == instance.id)
        .where(LabOperation.kind == "deploy")
        .where(LabOperation.state == "succeeded")
    ).one()
    worker_identity = lab_operations.worker_identity_parts()
    assert deployed_operation.claimed_by == worker_identity.claimed_by
    assert deployed_operation.claimed_host == worker_identity.host
    assert deployed_operation.claimed_epoch == worker_identity.epoch

    lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
    admin_session.commit()

    assert lab_jobs.drain_once(lab_worker_session, engine) == 1
    lab_worker_session.commit()

    # --- the real academy_lab_worker role CAN reclaim a previous-epoch claim
    reclaim_instance, reclaim_person = _seed(
        admin_session, tenant_a.id, name="privileges-reclaim"
    )
    reclaim_operation = lab_operations.enqueue(
        admin_session, instance=reclaim_instance, kind="deploy", requested_by=reclaim_person.id
    )
    reclaim_operation.state = "claimed"
    reclaim_operation.claimed_by = "some-host:111:oldboot"
    reclaim_operation.claimed_host = "some-host"
    reclaim_operation.claimed_epoch = "111:oldboot"
    reclaim_operation.claimed_at = datetime.now(UTC)
    reclaim_operation.heartbeat_at = datetime.now(UTC)
    admin_session.commit()

    reclaimed = lab_operations.reclaim_previous_epoch(
        lab_worker_session, host="some-host", epoch="222:newboot"
    )
    lab_worker_session.commit()
    assert reclaimed == 1
    admin_session.rollback()
    admin_session.refresh(reclaim_operation)
    assert reclaim_operation.state == "queued"
    assert reclaim_operation.claimed_by is None
    assert reclaim_operation.claimed_host is None
    assert reclaim_operation.claimed_epoch is None

    # --- reconcile_runtime: inventory reports nothing running ---------------
    queued, destroyed = lab_jobs.reconcile_runtime(lab_worker_session, engine)
    lab_worker_session.commit()
    assert destroyed == 0
    assert queued >= 1

    # --- verify consequences were actually persisted, not just "no exception"
    admin_session.rollback()
    submission = admin_session.scalars(
        select(Submission).where(Submission.person_id == person.id)
    ).first()
    assert submission is not None

    score = admin_session.scalars(
        select(Score).where(Score.submission_id == submission.id)
    ).first()
    assert score is not None
    assert score.passed is True

    event_kinds = {
        row
        for row in admin_session.scalars(
            select(LearningEvent.kind).where(LearningEvent.person_id == person.id)
        ).all()
    }
    assert {"submission_made", "work_graded", "lab_check_passed"} <= event_kinds

    # Grading creates only the owner-only intent. Admin delivery produces the
    # notification and outbox rows after the worker transaction commits.
    assert admin_session.scalar(text("""
        SELECT count(*) FROM public.lab_result_deliveries WHERE tenant_id=:tenant
          AND person_id=:person AND status='pending'
    """), {"tenant": tenant_a.id, "person": person.id}) == 1
    from app.services import lab_result_delivery

    lab_result_delivery.materialize_pending(admin_session)
    admin_session.commit()
    notification = admin_session.scalars(
        select(Notification).where(Notification.person_id == person.id)
    ).first()
    assert notification is not None

    email = admin_session.scalars(
        select(EmailOutbox).where(EmailOutbox.idempotency_key == f"score-pass:{score.id}")
    ).first()
    assert email is not None

    repair_operation = admin_session.scalars(
        select(LabOperation)
        .where(LabOperation.instance_id == instance.id)
        .where(LabOperation.state == "queued")
        .where(LabOperation.kind == "deploy")
    ).first()
    assert repair_operation is not None
