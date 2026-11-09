"""Web-layer tests for announcements — learner GET /announcements and
instructor GET/POST /instructor/announcements."""
from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant
from sqlalchemy import text

from app.models.cohort import Cohort
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import login_location, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _set_tenant(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})

def _make_user(admin_session, tenant, email="ann_stu@a.edu", role=None):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="A", last_name="B")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("pw1"))
    )
    # The web facet admits only the default roles; a user with no named role is a student.
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role or "student"].id))
    admin_session.commit()
    return p

def _login(app_client, email="ann_stu@a.edu"):
    web_login(app_client, H, email, "pw1")

def _csrf(app_client):
    return app_client.cookies.get("csrf_token", "")

# ── Learner GET /announcements ─────────────────────────────────────────────────

def test_announcements_requires_auth(app_client, tenant_a):
    r = app_client.get("/announcements", headers=H, follow_redirects=False)
    # The kernel facet auth redirects (302) before Academy's own 303 guard.
    assert r.status_code == 302
    assert r.headers["location"] == login_location("/announcements")

def test_announcements_learner_200(app_client, admin_session, tenant_a):
    _make_user(admin_session, tenant_a, "ann_l200@a.edu")
    _login(app_client, "ann_l200@a.edu")
    r = app_client.get("/announcements", headers=H)
    assert r.status_code == 200
    assert "Announcements" in r.text

def test_announcements_shows_tenant_wide(app_client, admin_session, tenant_a):
    """Tenant-wide announcements appear on the learner page."""
    p = _make_user(admin_session, tenant_a, "ann_show@a.edu")
    _set_tenant(admin_session, tenant_a.id)

    from app.services import announcements as ann_svc
    ann_svc.create(
        admin_session,
        tenant_id=tenant_a.id,
        author_person_id=p.id,
        title="Big news for everyone",
        body_md="Hello!",
    )
    admin_session.commit()

    _login(app_client, "ann_show@a.edu")
    r = app_client.get("/announcements", headers=H)
    assert r.status_code == 200
    assert "Big news for everyone" in r.text

# ── Instructor GET /instructor/announcements ───────────────────────────────────

def test_instructor_announcements_requires_role(app_client, admin_session, tenant_a):
    """Student (no instructor role) gets 403 on instructor announcements page."""
    _make_user(admin_session, tenant_a, "ann_stu403@a.edu")
    _login(app_client, "ann_stu403@a.edu")
    r = app_client.get("/instructor/announcements", headers=H)
    assert r.status_code == 403

def test_instructor_announcements_200(app_client, admin_session, tenant_a):
    _make_user(admin_session, tenant_a, "ann_ins@a.edu", role="instructor")
    _login(app_client, "ann_ins@a.edu")
    r = app_client.get("/instructor/announcements", headers=H)
    assert r.status_code == 200
    assert "New announcement" in r.text

def test_instructor_create_announcement(app_client, admin_session, tenant_a):
    """Instructor POST creates announcement and redirects."""
    _make_user(admin_session, tenant_a, "ann_cre@a.edu", role="instructor")
    _login(app_client, "ann_cre@a.edu")
    csrf = _csrf(app_client)

    r = app_client.post(
        "/instructor/announcements",
        headers={**H, "x-csrf-token": csrf},
        data={"title": "New Class Schedule", "body_md": "Check the portal.", "cohort_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Verify the announcement landed in DB
    from sqlalchemy import text as sqlt

    from app.services import announcements as ann_svc
    admin_session.execute(sqlt("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_a.id)})
    items = ann_svc.list_for_tenant(admin_session, tenant_id=tenant_a.id)
    titles = [a.title for a in items]
    assert "New Class Schedule" in titles

def test_student_403_on_instructor_announcements_post(app_client, admin_session, tenant_a):
    """Student cannot POST to instructor announcements."""
    _make_user(admin_session, tenant_a, "ann_stu_post@a.edu")
    _login(app_client, "ann_stu_post@a.edu")
    csrf = _csrf(app_client)

    r = app_client.post(
        "/instructor/announcements",
        headers={**H, "x-csrf-token": csrf},
        data={"title": "Hack", "body_md": "nope", "cohort_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 403

def test_instructor_delete_announcement(app_client, admin_session, tenant_a):
    """Instructor can delete an announcement via POST /{id}/delete."""
    ins = _make_user(admin_session, tenant_a, "ann_del@a.edu", role="instructor")
    _set_tenant(admin_session, tenant_a.id)

    from app.services import announcements as ann_svc
    ann = ann_svc.create(
        admin_session,
        tenant_id=tenant_a.id,
        author_person_id=ins.id,
        title="To be deleted",
        body_md="bye",
    )
    admin_session.commit()

    _login(app_client, "ann_del@a.edu")
    csrf = _csrf(app_client)

    r = app_client.post(
        f"/instructor/announcements/{ann.id}/delete",
        headers={**H, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303

    admin_session.expire_all()
    from sqlalchemy import text as sqlt
    admin_session.execute(sqlt("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_a.id)})
    items = ann_svc.list_for_tenant(admin_session, tenant_id=tenant_a.id)
    assert not any(a.id == ann.id for a in items)

def test_instructor_create_cohort_targeted(app_client, admin_session, tenant_a):
    """Instructor can create a cohort-targeted announcement."""
    _make_user(admin_session, tenant_a, "ann_coh@a.edu", role="instructor")
    _set_tenant(admin_session, tenant_a.id)
    coh = Cohort(tenant_id=tenant_a.id, name="Target Cohort", discipline="net", status="active")
    admin_session.add(coh)
    admin_session.commit()

    _login(app_client, "ann_coh@a.edu")
    csrf = _csrf(app_client)

    r = app_client.post(
        "/instructor/announcements",
        headers={**H, "x-csrf-token": csrf},
        data={"title": "Cohort message", "body_md": "cohort only", "cohort_id": str(coh.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303

    from sqlalchemy import text as sqlt

    from app.services import announcements as ann_svc
    admin_session.execute(sqlt("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_a.id)})
    admin_session.expire_all()
    items = ann_svc.list_for_tenant(admin_session, tenant_id=tenant_a.id)
    matched = [a for a in items if a.title == "Cohort message"]
    assert len(matched) == 1
    assert matched[0].cohort_id == coh.id

def test_learn_home_surfaces_announcements(app_client, admin_session, tenant_a):
    """The learn home page shows the latest 3 announcements."""
    p = _make_user(admin_session, tenant_a, "ann_home@a.edu")
    _set_tenant(admin_session, tenant_a.id)

    from app.services import announcements as ann_svc
    ann_svc.create(
        admin_session,
        tenant_id=tenant_a.id,
        author_person_id=p.id,
        title="Home page announcement",
        body_md="Hi from home",
    )
    admin_session.commit()

    _login(app_client, "ann_home@a.edu")
    r = app_client.get("/", headers=H)
    assert r.status_code == 200
    assert "Home page announcement" in r.text

def test_announcements_nav_item_in_learn_sidebar(app_client, admin_session, tenant_a):
    """The learn sidebar contains an Announcements link."""
    _make_user(admin_session, tenant_a, "ann_nav@a.edu")
    _login(app_client, "ann_nav@a.edu")
    r = app_client.get("/", headers=H)
    assert r.status_code == 200
    assert "/announcements" in r.text
    assert "Announcements" in r.text
