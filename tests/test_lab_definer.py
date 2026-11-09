"""Hosted PostgreSQL qualification for the lab grading definer (a0001).

Genesis: migrate a fresh database with this assembly's Alembic
environment (kernel + learning + assembly lineages), compose the
EXECUTE grants, seed one claimed lab check, and prove the worker path:
`academy_lab_worker` reaches the learning ledger ONLY through
`record_claimed_lab_check` — a passing check lands submission/score/
completion/certificate plus the private delivery intent atomically,
while direct capability/table access stays refused (42501).

Requires TEST_DATABASE_URL (superuser) like the Starter hosted suites;
skips without it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import _scoped, _url_for

ROOT = Path(__file__).resolve().parents[1]


def test_worker_reaches_ledger_only_through_definer(genesis_database):
    admin, superuser, name = genesis_database
    tenant, person = uuid.uuid4(), uuid.uuid4()
    course, activity = uuid.uuid4(), uuid.uuid4()
    template, instance, operation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with admin.begin() as conn:
        _scoped(conn, tenant)
        conn.execute(
            text("INSERT INTO public.tenants (id, slug, name) VALUES (:id, :slug, 'Academy')"),
            {"id": tenant, "slug": tenant.hex},
        )
        conn.execute(
            text(
                "INSERT INTO public.parties (id, tenant_id, party_type, display_name) "
                "VALUES (:id, :tenant, 'person', 'Learner')"
            ),
            {"id": person, "tenant": tenant},
        )
        conn.execute(
            text(
                "INSERT INTO public.party_persons (party_id, first_name, last_name) VALUES (:id, 'Learning', 'Person')"
            ),
            {"id": person},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.courses (id, tenant_id, slug, title, "
                "discipline, source_ref, status, listed) VALUES (:id, :tenant, "
                ":slug, 'Course', 'engineering', 'source', 'published', false)"
            ),
            {"id": course, "tenant": tenant, "slug": course.hex},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.activities (id, tenant_id, course_id, "
                "title, type, assessment_mode, grading, pass_threshold, weight) "
                "VALUES (:id, :tenant, :course, 'Lab', 'lab', 'graded', 'auto', "
                "0.7, 1.0)"
            ),
            {"id": activity, "tenant": tenant, "course": course},
        )
        conn.execute(
            text(
                "INSERT INTO public.lab_templates (id, tenant_id, course_id, "
                "activity_id, slug, title, topology, checks) VALUES (:id, :tenant, "
                ":course, :activity, 'lab-1', 'Template', '', "
                '\'[{"id": "c1", "label": "Check 1", "weight": 1}]\'::jsonb)'
            ),
            {"id": template, "tenant": tenant, "course": course, "activity": activity},
        )
        conn.execute(
            text(
                "INSERT INTO public.lab_instances (id, tenant_id, activity_id, "
                "person_id, instance_name, seed, status) VALUES (:id, :tenant, "
                ":activity, :person, :name, '{}'::jsonb, 'active')"
            ),
            {
                "id": instance,
                "tenant": tenant,
                "activity": activity,
                "person": person,
                "name": f"lab-{instance.hex[:12]}",
            },
        )
        conn.execute(
            text(
                "INSERT INTO public.lab_operations (id, tenant_id, instance_id, "
                "kind, state, claimed_by, claim_lease_seconds, heartbeat_at) "
                "VALUES (:id, :tenant, :instance, 'check', 'claimed', "
                "'worker-1', 300, clock_timestamp())"
            ),
            {"id": operation, "tenant": tenant, "instance": instance},
        )

    worker = create_engine(_url_for(superuser, name, user="academy_lab_worker"))
    app_user = create_engine(_url_for(superuser, name, user="app_user"))
    try:
        with worker.begin() as conn:
            _scoped(conn, tenant)
            row = conn.execute(
                text(
                    "SELECT submission_id, score_id, score, max_score, passed "
                    "FROM public.record_claimed_lab_check(:op, 'worker-1', "
                    '\'[{"id": "c1", "label": "Check 1", "weight": 1, '
                    '"pass": true}]\'::jsonb)'
                ),
                {"op": operation},
            ).one()
        assert row.passed is True
        assert row.score == row.max_score == 1.0

        with admin.connect() as conn:
            _scoped(conn, tenant)
            assert conn.scalar(text("SELECT count(*) FROM mod_learning.scores WHERE passed")) == 1
            delivery = conn.execute(
                text("SELECT first_pass, status FROM public.lab_result_deliveries WHERE operation_id = :op"),
                {"op": operation},
            ).one()
            assert delivery.first_pass is True
            assert delivery.status == "pending"
            assert conn.scalar(text("SELECT count(*) FROM public.audit_events WHERE action = 'lab_result.record'")) == 1

        # The ledger stays fenced: no direct capability or table access.
        with worker.begin() as conn:
            _scoped(conn, tenant)
            with pytest.raises(DBAPIError) as direct:
                conn.execute(
                    text(
                        "SELECT * FROM mod_learning.record_lab_result_v1("
                        ":t, :p, :a, :s, '{}'::jsonb, 1, 1, 1, true, '[]'::jsonb)"
                    ),
                    {"t": tenant, "p": person, "a": activity, "s": instance},
                )
        assert getattr(direct.value.orig, "sqlstate", None) == "42501"
        with app_user.begin() as conn:
            _scoped(conn, tenant)
            with pytest.raises(DBAPIError) as web:
                conn.execute(
                    text("SELECT * FROM public.record_claimed_lab_check(:op, 'worker-1', '[]'::jsonb)"),
                    {"op": operation},
                )
        assert getattr(web.value.orig, "sqlstate", None) == "42501"
    finally:
        worker.dispose()
        app_user.dispose()


def test_lab_result_delivery_model_matches_a0001_ddl(genesis_database):
    from sqlalchemy import inspect

    from app.models.lab_result_delivery import LabResultDelivery

    admin, _superuser, _name = genesis_database
    reflected = {c["name"] for c in inspect(admin).get_columns("lab_result_deliveries")}
    assert reflected == {c.name for c in LabResultDelivery.__table__.columns}
