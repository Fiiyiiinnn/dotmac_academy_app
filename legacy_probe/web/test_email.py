"""Tests for the on-demand email routes in the instructor Reports portal."""

from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant
from sqlalchemy import select

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import make_person_credential, make_person_party, web_login


def _seed_login(admin_session, tenant, email, role_slug):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Log", last_name="In")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1")))
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role_slug].id))
    admin_session.commit()
    return p

def _seed_cohort(admin_session, tid):
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    a1 = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                  title="Ch1 Test", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(a1)
    admin_session.flush()
    coh = Cohort(tenant_id=tid, name="Abuja 2026", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    stu_a = make_person_party(admin_session, tenant_id=tid, email="a@stu.edu", first_name="Aaa", last_name="Student")
    admin_session.add(stu_a)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=stu_a.id,
                                 role_in_cohort="student", status="active"))
    sub = Submission(tenant_id=tid, activity_id=a1.id, person_id=stu_a.id, answers={}, attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tid, submission_id=sub.id, score=10, max_score=10,
                            fraction=1.0, passed=True, per_item=[], source="auto"))
    admin_session.commit()
    return coh, stu_a

def _login(app_client, email):
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, "password1")
    return h

def test_student_forbidden_on_email_route(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "stud@a.edu", "student")
    coh, stu_a = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "stud@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/reports/student/{stu_a.id}/email",
        headers={**h, "x-csrf-token": csrf},
    )
    assert r.status_code == 403

def test_instructor_email_student_queues_delivery(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst@a.edu", "instructor")
    coh, stu_a = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/reports/student/{stu_a.id}/email",
        headers={**h, "x-csrf-token": csrf},
    )
    assert r.status_code == 200
    admin_session.rollback()
    queued = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "transcript")
    ).one()
    assert queued.recipient == "a@stu.edu"
    assert queued.status == "pending"
    assert "Email queued" in r.text

def test_instructor_email_cohort_to_self(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst@a.edu", "instructor")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/reports/cohort/{coh.id}/email",
        headers={**h, "x-csrf-token": csrf},
    )
    assert r.status_code == 200
    admin_session.rollback()
    queued = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "cohort_report")
    ).one()
    assert queued.recipient == "inst@a.edu"
