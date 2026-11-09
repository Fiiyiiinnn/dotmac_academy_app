"""ERP ACK conservation hosted proof (issue #2 ruling 9).

Sender behavior + imported-key membership conservation against a
genesis database with the ERP HTTP boundary stubbed: imported
completions never replay silently, raw ACK facts (including NULLs)
are preserved, and only a recorded reply stamps the checkpoint.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

RAW_ACK = datetime(2026, 5, 1, 9, 30, tzinfo=UTC)


def _seed(admin):
    tenant, person = uuid.uuid4(), uuid.uuid4()
    course, cohort = uuid.uuid4(), uuid.uuid4()
    offering, enrollment, completion = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'ERP')"),
            {"id": tenant, "s": tenant.hex},
        )
        conn.execute(
            text(
                "INSERT INTO parties (id, tenant_id, party_type, display_name) "
                "VALUES (:id, :t, 'person', 'Staff Person')"
            ),
            {"id": person, "t": tenant},
        )
        conn.execute(
            text("INSERT INTO party_persons (party_id, first_name, last_name) VALUES (:id, 'Staff', 'Person')"),
            {"id": person},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.courses (id, tenant_id, slug, title, discipline, source_ref, status, listed) "
                "VALUES (:id, :t, :s, 'Fiber', 'networking', 'seed:erp', 'published', true)"
            ),
            {"id": course, "t": tenant, "s": course.hex},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.cohorts (id, tenant_id, name, discipline, status, delivery_mode, "
                "auto_accept_below_action) VALUES (:id, :t, 'C1', 'networking', 'active', 'online', 'none')"
            ),
            {"id": cohort, "t": tenant},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.course_offerings (id, tenant_id, cohort_id, course_id, status, "
                "pacing_default_open) VALUES (:id, :t, :cohort, :course, 'active', false)"
            ),
            {"id": offering, "t": tenant, "cohort": cohort, "course": course},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.enrollments (id, tenant_id, cohort_id, person_id, role_in_cohort, "
                "status, audience, employee_ref) VALUES (:id, :t, :cohort, :p, 'learner', 'active', 'staff', 'EMP-7')"
            ),
            {"id": enrollment, "t": tenant, "cohort": cohort, "p": person},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.course_completions (id, tenant_id, person_id, course_id, status, pct) "
                "VALUES (:id, :t, :p, :course, 'completed', 100.0)"
            ),
            {"id": completion, "t": tenant, "p": person, "course": course},
        )
    return tenant, person, course, completion


def _completion(db, completion_id):
    from dotmac_learning.models import CourseCompletion

    return db.get(CourseCompletion, completion_id)


def _checkpoint(db, tenant, completion):
    return db.execute(
        text(
            "SELECT erp_synced_at, erp_synced_pct FROM academy_erp_completion_checkpoints "
            "WHERE tenant_id = :t AND completion_id = :c"
        ),
        {"t": tenant, "c": completion},
    ).one_or_none()


def _reply(status_code, status):
    return httpx.Response(status_code, json={"status": status}, request=httpx.Request("POST", "https://erp.invalid"))


@pytest.fixture
def erp_env(genesis_database, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "erp_webhook_url", "https://erp.invalid/hook")
    monkeypatch.setattr(settings, "erp_webhook_secret", "test-secret")
    admin, _superuser, _name = genesis_database
    return admin, _seed(admin)


def test_imported_completion_without_checkpoint_refuses(erp_env):
    from app.services.erp_sync import ERPCheckpointInvariantError, push_completion

    admin, (tenant, _person, _course, completion) = erp_env
    with admin.begin() as conn:
        conn.execute(
            text("INSERT INTO academy_erp_imported_completion_keys (tenant_id, completion_id) VALUES (:t, :c)"),
            {"t": tenant, "c": completion},
        )
    with Session(admin) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        with pytest.raises(ERPCheckpointInvariantError, match="missing checkpoint for imported"):
            push_completion(db, tenant_id=tenant, completion=_completion(db, completion))


def test_imported_null_ack_membership_and_recorded_stamp(erp_env, monkeypatch):
    from app.services import erp_sync

    admin, (tenant, _person, _course, completion) = erp_env
    with admin.begin() as conn:
        conn.execute(
            text("INSERT INTO academy_erp_imported_completion_keys (tenant_id, completion_id) VALUES (:t, :c)"),
            {"t": tenant, "c": completion},
        )
        conn.execute(
            text(
                "INSERT INTO academy_erp_completion_checkpoints "
                "(tenant_id, completion_id, erp_synced_at, erp_synced_pct) VALUES (:t, :c, NULL, NULL)"
            ),
            {"t": tenant, "c": completion},
        )
    calls = []
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: calls.append(1) or _reply(200, "recorded"))
    with Session(admin) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        assert erp_sync.push_completion(db, tenant_id=tenant, completion=_completion(db, completion)) == erp_sync.SYNCED
    assert calls == [1]
    with Session(admin) as db:
        row = _checkpoint(db, tenant, completion)
        assert row is not None and row[0] is not None and row[1] == 100.0
        member = db.execute(
            text(
                "SELECT count(*) FROM academy_erp_imported_completion_keys WHERE tenant_id = :t AND completion_id = :c"
            ),
            {"t": tenant, "c": completion},
        ).scalar_one()
        assert member == 1


def test_imported_raw_ack_suppresses_resend_without_new_receipt(erp_env, monkeypatch):
    from app.services import erp_sync

    admin, (tenant, _person, _course, completion) = erp_env
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO academy_erp_completion_checkpoints "
                "(tenant_id, completion_id, erp_synced_at, erp_synced_pct) VALUES (:t, :c, :at, 87.5)"
            ),
            {"t": tenant, "c": completion, "at": RAW_ACK},
        )

    def forbidden(*a, **k):
        raise AssertionError("imported ACK must suppress outbound ERP traffic")

    monkeypatch.setattr(erp_sync.httpx, "post", forbidden)
    with Session(admin) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        assert erp_sync.push_completion(db, tenant_id=tenant, completion=_completion(db, completion)) == erp_sync.SYNCED
    with Session(admin) as db:
        row = _checkpoint(db, tenant, completion)
        assert row[0] == RAW_ACK  # raw imported ACK preserved, not re-stamped
        assert row[1] == 100.0  # pct aligned as suppression bookkeeping


@pytest.mark.parametrize("code,status", [(200, "ignored"), (422, "ignored")])
def test_unmatched_reply_never_stamps_checkpoint(erp_env, monkeypatch, code, status):
    from app.services import erp_sync

    admin, (tenant, _person, _course, completion) = erp_env
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _reply(code, status))
    with Session(admin) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        assert (
            erp_sync.push_completion(db, tenant_id=tenant, completion=_completion(db, completion)) == erp_sync.UNMATCHED
        )
    with Session(admin) as db:
        row = _checkpoint(db, tenant, completion)
        assert row is not None and row[0] is None and row[1] is None


def test_cross_tenant_completion_refused(erp_env):
    from app.services.erp_sync import ERPCheckpointInvariantError, push_completion

    admin, (tenant, _person, _course, completion) = erp_env
    with Session(admin) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
        with pytest.raises(ERPCheckpointInvariantError, match="another tenant"):
            push_completion(db, tenant_id=uuid.uuid4(), completion=_completion(db, completion))
