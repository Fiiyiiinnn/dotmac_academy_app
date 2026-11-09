"""Instructor timetable UI: view, create, cancel sessions + set delivery mode."""

from __future__ import annotations

import pytest
from dotmac_kernel.models import PartyRoleGrant
from sqlalchemy import select

from app.models.class_session import ClassSession
from app.models.cohort import Cohort
from app.services import settings_store
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login_instructor(app_client, admin_session, tenant):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email="tt@a.edu", first_name="Ti", last_name="Me")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="tt@a.edu", password_hash=hash_password("password1"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles["instructor"].id))
    admin_session.commit()
    web_login(app_client, H, "tt@a.edu", "password1")
    return H

def _cohort(admin_session, tenant):
    c = Cohort(tenant_id=tenant.id, name="TT Cohort", discipline="fiber", status="active", delivery_mode="self_paced")
    admin_session.add(c)
    admin_session.commit()
    admin_session.refresh(c)
    return c

def test_timetable_page_renders(app_client, admin_session, tenant_a):
    _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    r = app_client.get(f"/instructor/cohorts/{c.id}/timetable", headers=H)
    assert r.status_code == 200
    assert "Timetable" in r.text and "Add a session" in r.text

def test_create_session_and_autopromote_delivery_mode(app_client, admin_session, tenant_a):
    h = _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{c.id}/sessions",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "title": "Live Q&A",
            "session_type": "live_class",
            "starts_at": "2026-08-01T10:00",
            "ends_at": "2026-08-01T11:00",
            "location": "Lagos lab",
            "join_url": "https://meet.example/x",
        },
    )
    assert r.status_code == 200
    session = admin_session.scalars(select(ClassSession).where(ClassSession.cohort_id == c.id)).first()
    assert session is not None and session.title == "Live Q&A"
    # Scheduling on a self-paced cohort promotes it to blended.
    admin_session.expire(c)
    assert admin_session.get(Cohort, c.id).delivery_mode == "blended"

def test_cancel_session(app_client, admin_session, tenant_a):
    h = _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    csrf = app_client.cookies.get("csrf_token", "")
    app_client.post(
        f"/instructor/cohorts/{c.id}/sessions",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"title": "Doomed", "starts_at": "2026-08-02T09:00"},
    )
    session = admin_session.scalars(select(ClassSession).where(ClassSession.cohort_id == c.id)).first()
    r = app_client.post(
        f"/instructor/sessions/{session.id}/cancel",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
    )
    assert r.status_code == 200
    admin_session.expire(session)
    assert admin_session.get(ClassSession, session.id).status == "cancelled"

def test_set_delivery_mode(app_client, admin_session, tenant_a):
    h = _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{c.id}/delivery-mode",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"mode": "live"},
    )
    assert r.status_code == 200
    admin_session.expire(c)
    assert admin_session.get(Cohort, c.id).delivery_mode == "live"

@pytest.mark.parametrize("submitted_type", [None, "live_class"])
def test_session_form_and_create_use_policy_only_for_omission(app_client, admin_session, tenant_a, submitted_type):
    h = _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    settings_store.set_many(admin_session, {"learning_default_session_type": "lab"})
    admin_session.commit()
    page = app_client.get(f"/instructor/cohorts/{c.id}/timetable", headers=h)
    assert page.status_code == 200
    assert '<option value="lab" selected>lab</option>' in page.text
    csrf = app_client.cookies.get("csrf_token", "")
    data = {"title": "Policy session", "starts_at": "2026-08-03T10:00"}
    if submitted_type is not None:
        data["session_type"] = submitted_type
    response = app_client.post(
        f"/instructor/cohorts/{c.id}/sessions",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data=data,
    )
    assert response.status_code == 200
    admin_session.expire_all()
    session = admin_session.scalars(
        select(ClassSession).where(ClassSession.tenant_id == tenant_a.id, ClassSession.cohort_id == c.id)
    ).one()
    assert session.session_type == ("lab" if submitted_type is None else submitted_type)

@pytest.mark.parametrize("submitted_type", [None, "live_class"])
def test_session_edit_keeps_explicit_type_and_composes_omitted_default(
    app_client, admin_session, tenant_a, submitted_type
):
    h = _login_instructor(app_client, admin_session, tenant_a)
    c = _cohort(admin_session, tenant_a)
    settings_store.set_many(admin_session, {"learning_default_session_type": "lab"})
    admin_session.commit()
    csrf = app_client.cookies.get("csrf_token", "")
    created = app_client.post(
        f"/instructor/cohorts/{c.id}/sessions",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"title": "Before", "starts_at": "2026-08-04T10:00", "session_type": "live_class"},
    )
    assert created.status_code == 200
    session = admin_session.scalars(select(ClassSession).where(ClassSession.cohort_id == c.id)).one()
    data = {"title": "After", "starts_at": "2026-08-04T11:00"}
    if submitted_type is not None:
        data["session_type"] = submitted_type
    edited = app_client.post(
        f"/instructor/sessions/{session.id}/edit",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data=data,
    )
    assert edited.status_code == 200
    admin_session.expire(session)
    assert session.title == "After"
    assert session.session_type == ("lab" if submitted_type is None else submitted_type)
