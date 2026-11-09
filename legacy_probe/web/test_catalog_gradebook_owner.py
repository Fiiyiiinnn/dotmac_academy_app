"""Authored hosted-only owner, cookie, CSV and public-form scenarios; NOT RUN."""
import csv
import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_learning.delivery_models import Announcement
from dotmac_learning.models import Activity, Course, LearningEvent, Score, Submission
from dotmac_learning.participation_models import Cohort, CourseOffering, Enrollment
from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import Settings
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.platform_settings import PlatformSetting
from app.services import gradebook, settings_store
from app.services.admin_reports import admin_recipients
from app.services.gradebook_access import require_gradebook_access
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web import catalog
from app.web import gradebook as routes
from legacy_probe.conftest import login_location
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_notifications_owner import _party
from legacy_probe.web.test_bookmarks_owner import _request
from legacy_probe.web.test_reports_access_policy import _nominate_cohort, _withdraw_nomination
from legacy_probe.web.test_settings_access_policy import _grant

assert current_account


@pytest.fixture(autouse=True)
def _restore_policy(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    original = settings_store.get_all(a.admin)
    try:
        yield
    finally:
        a.db.rollback()
        a.admin.rollback()
        a.admin.query(PlatformSetting).delete()
        for key, value in original.items():
            a.admin.add(PlatformSetting(key=key, value=value))
        a.admin.commit()


def _allow(a, key="gradebook_access_roles"):
    slug = f"purpose-{uuid4().hex}"
    grant = _grant(a, slug)
    settings_store.set_many(a.admin, {key: json.dumps([slug])})
    a.admin.commit()
    return grant, slug


def _seed(a, tid=None):
    tid = tid or a.tid
    course = Course(tenant_id=tid, slug=f"gb-{uuid4().hex}", title="Owner Gradebook", discipline="networking",
                    source_ref="hosted-test", version=1, listed=True, status="published")
    a.admin.add(course)
    a.admin.flush()
    activities = [Activity(tenant_id=tid, course_id=course.id, chapter_number=i + 1, type="mcq_test",
                           title=f"Quiz {i}", pass_threshold=0.6, weight=w, grading="auto") for i, w in enumerate([1.0, 3.0])]
    cohort = Cohort(tenant_id=tid, name=f"Owner {uuid4().hex}", discipline="networking", status="active")
    a.admin.add_all([*activities, cohort])
    a.admin.flush()
    a.admin.add(CourseOffering(tenant_id=tid, cohort_id=cohort.id, course_id=course.id, status="active"))
    people = [_party(a, tid), _party(a, tid)]
    for person in people:
        a.admin.add(Enrollment(tenant_id=tid, cohort_id=cohort.id, person_id=person.id,
                               role_in_cohort="student", status="active"))
    a.admin.flush()
    sub = Submission(tenant_id=tid, activity_id=activities[0].id, person_id=people[0].id, answers={}, attempt_no=1)
    a.admin.add(sub)
    a.admin.flush()
    a.admin.add(Score(tenant_id=tid, submission_id=sub.id, score=10, max_score=10,
                      fraction=1.0, passed=True, per_item=[], source="auto"))
    a.admin.commit()
    return course, cohort, people


def test_genuine_weighted_gradebook_typed_roster_html_csv_and_caller_transaction(current_account):
    a = current_account
    _allow(a)
    course, cohort, people = _seed(a)
    # Cohort data needs a cohort scope (#188): hold an active nomination for it.
    _nominate_cohort(a, cohort.id)
    request = _request(a.tenant, a.token)
    account = require_gradebook_access(request, require_web_user(request, a.db), a.db)
    assert isinstance(account, AcademyCurrentAccount)
    root = a.db.get_transaction()
    owner = learning.cohort_gradebook(a.db, tenant_id=a.tid, cohort_id=cohort.id)
    adapted = gradebook.cohort_gradebook(a.db, tenant_id=a.tid, cohort_id=cohort.id)
    assert all(isinstance(row["person"], learning.ReportPerson) for row in adapted["rows"])
    assert [(r["person"].id, r["final_pct"]) for r in adapted["rows"]] == [(r.person.id, r.final_pct) for r in owner.rows]
    assert {r["person"].id: r["final_pct"] for r in adapted["rows"]} == {people[0].id: 25, people[1].id: 0}
    html = routes.gradebook_cohort(cohort.id, request, account, a.db)
    assert b"25%" in html.body and people[0].email.encode() in html.body
    response = routes.gradebook_csv(cohort.id, request, account, a.db)
    rows = list(csv.reader(io.StringIO(response.body.decode())))
    assert rows[0] == ["name", "email", "Quiz 0 (w=1.0)", "Quiz 1 (w=3.0)", "final_pct"]
    assert {row[1]: row[-1] for row in rows[1:]} == {people[0].email: "25", people[1].email: "0"}
    assert response.headers["content-disposition"] == f"attachment; filename=gradebook-{cohort.id}.csv"
    assert cohort.id in {c.id for c in routes.gradebook_index(request, account, a.db).context["cohorts"]}
    assert a.db.get_transaction() is root


def test_foreign_gradebook_and_csv_are_404_on_actual_paths(current_account, tenant_b, app_client):
    a = current_account
    _allow(a)
    _, cohort, _ = _seed(a, tenant_b.id)
    app_client.cookies.set("session", a.token)
    for suffix in ["", ".csv"]:
        response = app_client.get(f"/instructor/gradebook/{cohort.id}{suffix}", headers={"Host": "alpha.localhost"})
        assert response.status_code == 404


def test_custom_policy_live_empty_deny_revocation_and_purpose_separation(current_account, app_client):
    a = current_account
    grant, slug = _allow(a)
    settings_store.set_many(a.admin, {"settings_access_roles": "[]", "teaching_access_roles": "[]", "catalog_staff_roles": "[]"})
    a.admin.commit()
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    request = _request(a.tenant, a.token)
    # The purpose policy alone admits the account at the service function; the
    # index route also needs a cohort scope (#188), held by a nomination.
    assert require_gradebook_access(request, require_web_user(request, a.db), a.db).id == a.pid
    _, cohort, _ = _seed(a)
    nomination_id, _ = _nominate_cohort(a, cohort.id)
    assert app_client.get("/instructor/gradebook", headers=headers).status_code == 200
    # Withdraw the scope so empty-deny and grant withdrawal are proved with the
    # policy as the only authority, never through the nomination fallback.
    _withdraw_nomination(a, nomination_id)
    assert not catalog._is_staff(a.db, a.tid, a.pid)
    stale = a.db.get(PlatformSetting, "gradebook_access_roles")
    settings_store.set_many(a.admin, {"gradebook_access_roles": "[]"})
    a.admin.commit()
    assert stale.value == json.dumps([slug])
    with pytest.raises(HTTPException) as denied:
        require_gradebook_access(request, require_web_user(request, a.db), a.db)
    assert denied.value.status_code == 403
    for path in ["/instructor/gradebook", f"/instructor/gradebook/{uuid4()}", f"/instructor/gradebook/{uuid4()}.csv"]:
        assert app_client.get(path, headers=headers).status_code == 403
    settings_store.set_many(a.admin, {"gradebook_access_roles": json.dumps([slug]), "catalog_staff_roles": json.dumps([slug])})
    a.admin.commit()
    assert catalog._is_staff(a.db, a.tid, a.pid)
    old_grant = a.db.get(PartyRoleGrant, grant)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant))
    a.admin.commit()
    assert old_grant.id == grant
    assert not catalog._is_staff(a.db, a.tid, a.pid)
    with pytest.raises(HTTPException) as withdrawn:
        require_gradebook_access(request, require_web_user(request, a.db), a.db)
    assert withdrawn.value.status_code == 403
    assert app_client.get("/instructor/gradebook", headers=headers).status_code == 403
    assert require_web_user(request, a.db).id == a.pid


@pytest.mark.parametrize("key", ["catalog_staff_roles", "gradebook_access_roles"])
@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', '[1]'])
def test_malformed_purpose_policy_refuses_config_storage_and_reads(current_account, key, raw):
    a = current_account
    with pytest.raises(ValueError, match=key):
        Settings(**{key: raw})
    with pytest.raises(ValueError, match=key):
        settings_store.set_many(a.admin, {key: raw})
    a.admin.merge(PlatformSetting(key=key, value=raw))
    a.admin.commit()
    with pytest.raises(ValueError, match=key):
        settings_store.effective(a.db, require_store=True)


@pytest.mark.parametrize("mutation", ["revoked", "inactive", "suspended"])
def test_live_cookie_authority_on_gradebook_and_catalog(current_account, app_client, mutation):
    a = current_account
    _allow(a)
    _, cohort, _ = _seed(a)
    _nominate_cohort(a, cohort.id)
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    assert app_client.get("/instructor/gradebook", headers=headers).status_code == 200
    if mutation == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif mutation == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended"))
    a.admin.commit()
    for path in ["/instructor/gradebook", f"/courses/{uuid4()}", "/announcements", "/calendar"]:
        response = app_client.get(path, headers=headers, follow_redirects=False)
        if mutation == "revoked":
            # The kernel facet auth redirects a revoked session (302) with ?next=.
            assert response.status_code == 302 and response.headers["location"] == login_location(path)
        else:
            assert response.status_code == 303 and response.headers["location"] == "/login"


def test_management_form_real_outbox_escaped_payload_and_caller_rollback(current_account):
    a = current_account
    request = _request(a.tenant)
    response = catalog.management_enrollment_form(request, a.db)
    assert response.template.name == "public/management_enrollment.html"
    before = set(a.db.scalars(select(EmailOutbox.id).where(EmailOutbox.tenant_id == a.tid)).all())
    recipients = {r.email for r in admin_recipients(a.db, tenant_id=a.tid)}
    if not recipients:
        recipients = {str(settings_store.effective(a.db).management_inquiry_recipient).strip()}
    root = a.db.get_transaction()
    response = catalog.submit_management_enrollment(request, "<First>", "Last", " USER@EXAMPLE.TEST ",
                                                    "123", "individual", "Management", "<script>unsafe</script>", a.db)
    assert b"Inquiry received" in response.body
    queued = [r for r in a.db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid)).all() if r.id not in before]
    assert {r.recipient for r in queued} == recipients
    assert all(r.kind == "management_inquiry" and r.payload["email"] == "user@example.test" for r in queued)
    assert all("&lt;script&gt;unsafe&lt;/script&gt;" in r.html_body and "<script>" not in r.html_body for r in queued)
    assert a.db.get_transaction() is root
    ids = {r.id for r in queued}
    a.db.rollback()
    assert not set(a.db.scalars(select(EmailOutbox.id).where(EmailOutbox.id.in_(ids))).all())


def test_unknown_course_404_before_actual_entitlement_and_enrolled_course_view_event(current_account):
    a = current_account
    _allow(a, "catalog_staff_roles")
    course, cohort, _ = _seed(a)
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    with pytest.raises(HTTPException) as missing:
        catalog.course_landing("missing-" + uuid4().hex, request, account, a.db)
    assert missing.value.status_code == 404
    settings_store.set_many(a.admin, {"catalog_staff_roles": "[]"})
    a.admin.commit()
    with pytest.raises(HTTPException) as denied:
        catalog.course_landing(course.slug, request, account, a.db)
    assert denied.value.status_code == 403

    a.admin.add(Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid,
                           role_in_cohort="student", status="active"))
    a.admin.commit()
    root = a.db.get_transaction()
    response = catalog.course_landing(course.slug, request, account, a.db)
    assert response.context["course"].id == course.id
    assert response.context["is_staff"] is False
    assert a.db.scalar(select(LearningEvent.id).where(LearningEvent.tenant_id == a.tid,
        LearningEvent.person_id == a.pid, LearningEvent.course_id == course.id,
        LearningEvent.kind == "course_viewed")) is not None
    assert a.db.get_transaction() is root


def test_public_partition_and_announcement_target_names_use_tenant_owner_choices(current_account, tenant_b):
    a = current_account
    course, target, _ = _seed(a)
    foreign_course, foreign_target, _ = _seed(a, tenant_b.id)
    a.admin.execute(update(Course).where(Course.id == course.id).values(discipline="management"))
    # Target labels come from the full tenant directory, with no cross-tenant name exposure.
    a.admin.add(Announcement(tenant_id=a.tid, cohort_id=target.id, author_person_id=a.pid,
                              title="Target notice", body_md="notice", body_html="<p>notice</p>"))
    a.admin.add(Enrollment(tenant_id=a.tid, cohort_id=target.id, person_id=a.pid,
                           role_in_cohort="student", status="active"))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    public = catalog.courses_list(_request(a.tenant), None, a.db)
    assert course.id in {i["course"].id for i in public.context["mgmt"]}
    assert foreign_course.id not in {i["course"].id for i in public.context["courses"]}
    response = catalog.announcements(request, account, a.db)
    assert response.context["cohort_map"][target.id] == target.name
    assert foreign_target.id not in response.context["cohort_map"]
    assert any(item.title == "Target notice" for item in response.context["announcements"])
    calendar = catalog.calendar(request, account, a.db)
    assert calendar.template.name == "learn/calendar.html"


@pytest.mark.parametrize("queued,expected", [(1, b"Inquiry received"), (0, b"Inquiry not sent")])
def test_management_post_status_preserves_enqueue_result_and_form_fields(current_account, monkeypatch, queued, expected):
    a = current_account
    calls = []
    def enqueue(db, **fields):
        calls.append((db, fields))
        return queued
    monkeypatch.setattr(catalog.management_inquiries, "queue_management_inquiry", enqueue)
    response = catalog.submit_management_enrollment(_request(a.tenant), "First", "Last", "sender@example.test",
                                                    "123", "Team", "Leadership", "Seats", a.db)
    assert expected in response.body
    assert calls == [(a.db, {"tenant_id": a.tid, "first_name": "First", "last_name": "Last",
                            "email": "sender@example.test", "phone": "123", "learner_type": "Team",
                            "course_interest": "Leadership", "message": "Seats"})]


def test_management_configured_fallback_uses_existing_outbox_without_recipient_join(current_account, monkeypatch):
    from app.services import management_inquiries
    a = current_account
    settings_store.set_many(a.admin, {"management_inquiry_recipient": "management-fallback@example.test"})
    a.admin.commit()
    monkeypatch.setattr(management_inquiries, "admin_recipients", lambda db, *, tenant_id: [])
    before = set(a.db.scalars(select(EmailOutbox.id).where(EmailOutbox.tenant_id == a.tid)).all())
    count = management_inquiries.queue_management_inquiry(a.db, tenant_id=a.tid,
        first_name="Fallback", last_name="Person", email="reply@example.test")
    rows = [r for r in a.db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid)).all() if r.id not in before]
    assert count == 1 and len(rows) == 1
    assert rows[0].recipient == "management-fallback@example.test"
    assert rows[0].kind == "management_inquiry"
    a.db.rollback()


@pytest.mark.parametrize("key", ["catalog_staff_roles", "gradebook_access_roles"])
def test_custom_and_empty_environment_purpose_policy(monkeypatch, key):
    monkeypatch.setenv(key.upper(), '["custom-facilitator"]')
    assert getattr(Settings(), key) == ("custom-facilitator",)
    assert getattr(Settings(**{key: []}), key) == ()
