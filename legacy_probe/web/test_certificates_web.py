"""Certificate download route (Slice 2d)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.completion import CourseCompletion
from app.models.course import Course
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Ada", last_name="Lovelace")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _course(admin_session, tid, slug="net"):
    c = Course(tenant_id=tid, slug=slug, title="Networking 101", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    return c

def test_completed_learner_downloads_pdf(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, "ada@a.edu")
    c = _course(admin_session, tenant_a.id)
    admin_session.add(CourseCompletion(tenant_id=tenant_a.id, person_id=p.id, course_id=c.id,
                                       status="completed", pct=1.0, completed_at=datetime.now(UTC)))
    admin_session.commit()
    try:
        r = app_client.get(f"/certificates/{c.id}", headers=H)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content[:5] == b"%PDF-"
        assert "attachment" in r.headers.get("content-disposition", "")
    finally:
        admin_session.query(Course).filter(Course.tenant_id == tenant_a.id).delete()
        admin_session.commit()

def test_incomplete_learner_forbidden(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, "bob@a.edu")
    c = _course(admin_session, tenant_a.id, slug="net2")
    admin_session.add(CourseCompletion(tenant_id=tenant_a.id, person_id=p.id, course_id=c.id,
                                       status="in_progress", pct=0.5, completed_at=None))
    admin_session.commit()
    try:
        assert app_client.get(f"/certificates/{c.id}", headers=H).status_code == 403
    finally:
        admin_session.query(Course).filter(Course.tenant_id == tenant_a.id).delete()
        admin_session.commit()
