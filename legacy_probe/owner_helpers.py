"""Shared helpers moved out of retired test modules (test_reports_owner, test_settings_route_restored).

Importers: web/test_reports_owner, services/test_{reports_csv_insights,analytics,dashboards}_owner,
services/test_instructor_read_facades.
"""

from __future__ import annotations

from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role
from sqlalchemy import select, update

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering


def person(db, tid, email, first_name, last_name):
    p = Party(
        tenant_id=tid, party_type=PartyType.person, display_name=f"{first_name} {last_name}".strip(),
        email=email, is_active=True,
    )
    db.add(p)
    db.flush()
    db.add(PartyPerson(party_id=p.id, first_name=first_name, last_name=last_name))
    db.flush()
    return p


def seed_report_cohort(db, tid):
    """Course (networking) + 2 activities + cohort with 2 enrolled students + scores.

    Student A: passes ch1 (1.0), fails ch2-lab (0.4)  -> completion 1/2 = 0.5
    Student B: passes both (1.0, 0.8)                  -> completion 2/2 = 1.0
    """
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    db.add(c)
    db.flush()

    a1 = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                  title="Ch1 Test", pass_threshold=0.6, grading="auto", weight=1.0)
    a2 = Activity(tenant_id=tid, course_id=c.id, chapter_number=2, type="lab",
                  title="Ch2 Lab", pass_threshold=0.6, grading="auto", weight=1.0)
    db.add_all([a1, a2])
    db.flush()

    coh = Cohort(tenant_id=tid, name="Abuja 2026", discipline="networking", status="active")
    db.add(coh)
    db.flush()

    stu_a = person(db, tid, email="a@stu.edu", first_name="Aaa", last_name="Student")
    stu_b = person(db, tid, email="b@stu.edu", first_name="Bbb", last_name="Student")
    db.add_all([stu_a, stu_b])
    db.flush()

    for p in (stu_a, stu_b):
        db.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=p.id,
                          role_in_cohort="student", status="active"))
    db.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=c.id, status="active"))
    db.flush()

    def _score(person, activity, frac, passed):
        sub = Submission(tenant_id=tid, activity_id=activity.id, person_id=person.id,
                         answers={}, attempt_no=1)
        db.add(sub)
        db.flush()
        db.add(Score(tenant_id=tid, submission_id=sub.id, score=frac * 10, max_score=10,
                     fraction=frac, passed=passed, per_item=[], source="auto"))
        db.flush()

    _score(stu_a, a1, 1.0, True)
    _score(stu_a, a2, 0.4, False)
    _score(stu_b, a1, 1.0, True)
    _score(stu_b, a2, 0.8, True)
    return c, coh, a1, a2, stu_a, stu_b


def seed_login_with_account(app_client, admin_session, tenant, email, role_slug, account):
    """Use the genuine canonical hosted account/session fixture, never legacy identity."""
    admin_session.execute(update(Party).where(Party.id == account.pid).values(email=email))
    role = admin_session.scalars(select(Role).where(Role.tenant_id == tenant.id, Role.slug == role_slug)).first()
    if role is None:
        role = Role(tenant_id=tenant.id, slug=role_slug, name=role_slug)
        admin_session.add(role)
        admin_session.flush()
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=account.pid, role_id=role.id))
    admin_session.commit()
    app_client.cookies.clear()
    app_client.cookies.set("session", account.token)
    return {"Host": "alpha.localhost"}
