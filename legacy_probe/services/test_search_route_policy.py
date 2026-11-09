"""Hosted genuine search canaries; whole installed assembly is still required.

NOT executed locally. The retained source lacks app.main and template/router
closure. These tests do not replace that closure or override authentication.
"""

import json
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyRoleGrant, PartyType, Role
from dotmac_learning.curriculum_models import Chapter
from dotmac_learning.models import Course
from dotmac_learning.participation_models import Cohort, CourseOffering, Enrollment
from sqlalchemy import update
from starlette.requests import Request

from app.config import Settings
from app.models.platform_settings import PlatformSetting
from app.services import settings_store, web_auth
from app.web import search
from legacy_probe.conftest import grant_role

KEY = "learning_search_staff_roles"


@pytest.fixture(autouse=True)
def _restore_search_policy(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    row = a.admin.get(PlatformSetting, KEY)
    existed, original = row is not None, None if row is None else row.value
    try:
        yield
    finally:
        a.db.rollback()
        a.admin.rollback()
        row = a.admin.get(PlatformSetting, KEY)
        if existed:
            a.admin.merge(PlatformSetting(key=KEY, value=original))
        elif row is not None:
            a.admin.delete(row)
        a.admin.commit()


def _policy(a, value):
    settings_store.set_many(a.admin, {KEY: json.dumps(value)})
    a.admin.commit()


def _grant(a, slug, tenant_id=None, party_id=None):
    # A custom purpose role never admits anyone to the web facet by itself (only
    # student/instructor/admin do), so the account also holds the standard student
    # role. The policy-level refusals under test are unchanged: student is not in
    # any *_access_roles policy.
    if tenant_id is None and party_id is None and slug not in {"student", "instructor", "admin"}:
        grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
    tid = a.tid if tenant_id is None else tenant_id
    role = Role(tenant_id=tid, slug=slug, name=slug)
    a.admin.add(role)
    a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=tid, party_id=a.pid if party_id is None else party_id, role_id=role.id))
    a.admin.commit()


def _course(a, *, accessible=False, status="published"):
    c = Course(tenant_id=a.tid, slug=uuid4().hex, title="SearchPolicyNeedle", discipline="networking",
               source_ref="search-policy", version=1, status=status, listed=False)
    a.admin.add(c)
    a.admin.flush()
    a.admin.add(Chapter(tenant_id=a.tid, course_id=c.id, number=1, title="SearchPolicyNeedle chapter",
                        part="I", body_html="<p>SearchPolicyNeedle</p>", source_hash="policy", order_index=1))
    if accessible:
        cohort = Cohort(tenant_id=a.tid, name=uuid4().hex, discipline="networking", status="active")
        a.admin.add(cohort)
        a.admin.flush()
        a.admin.add_all([
            Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid,
                       role_in_cohort="student", status="active"),
            CourseOffering(tenant_id=a.tid, cohort_id=cohort.id, course_id=c.id, status="active"),
        ])
    a.admin.commit()
    return c


def _request(a):
    return Request({"type": "http", "headers": [(b"cookie", f"session={a.token}".encode())],
                    "state": {"tenant": a.tenant}})


def _context(a, monkeypatch):
    # Capture presentation only; genuine session guard, tenant role query,
    # effective settings and installed Learning search all run unchanged.
    def render(request, name, context):
        assert name == "search.html"
        assert context["person"].id == a.pid
        assert context["q"] == "SearchPolicyNeedle"
        return context

    monkeypatch.setattr(search.templates, "TemplateResponse", render)
    request = _request(a)
    account = web_auth.require_web_user(request, a.db)
    return search.search_page(request, q="SearchPolicyNeedle", person=account, db=a.db)


def test_custom_role_actual_membership_and_real_testclient_rendering(current_account, app_client):
    a = current_account
    _policy(a, ["curriculum-reviewer"])
    _grant(a, "curriculum-reviewer")
    course = _course(a)
    response = app_client.get("/search", params={"q": "SearchPolicyNeedle"},
                              headers={"Host": "alpha.localhost", "Cookie": f"session={a.token}"})
    assert response.status_code == 200
    assert f"/courses/{course.slug}" in response.text
    assert f"/courses/{course.slug}/chapters/1" in response.text


def test_nonstaff_keeps_enrolled_published_visibility(current_account, monkeypatch):
    a = current_account
    _policy(a, ["curriculum-reviewer"])
    accessible = _course(a, accessible=True)
    hidden = _course(a)
    draft = _course(a, accessible=True, status="draft")
    context = _context(a, monkeypatch)
    slugs = {c["slug"] for c in context["courses"]}
    assert accessible.slug in slugs
    assert hidden.slug not in slugs and draft.slug not in slugs


def test_empty_roles_disable_staff_even_with_existing_admin_membership(current_account, monkeypatch):
    a = current_account
    _policy(a, [])
    _grant(a, "admin")
    course = _course(a)
    context = _context(a, monkeypatch)
    assert course.slug not in {c["slug"] for c in context["courses"]}
    assert not context["chapters"]


@pytest.mark.parametrize("raw", [None, "null", "{}", '"admin"', '["admin", "admin"]',
                                  '[""]', '[" admin"]', '[1]', "invalid-json"])
def test_malformed_stored_policy_refuses_before_search(current_account, monkeypatch, raw):
    a = current_account
    a.admin.merge(PlatformSetting(key=KEY, value=raw))
    a.admin.commit()
    calls = []

    def delegated(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("malformed policy delegated search")

    monkeypatch.setattr(search, "svc_search", delegated)
    request = _request(a)
    account = web_auth.require_web_user(request, a.db)
    with pytest.raises(ValueError, match=KEY):
        search.search_page(request, q="SearchPolicyNeedle", person=account, db=a.db)
    assert calls == []


def test_retained_setting_object_does_not_authorize_after_committed_policy_change(current_account, monkeypatch):
    a = current_account
    _policy(a, ["curriculum-reviewer"])
    _grant(a, "curriculum-reviewer")
    course = _course(a)
    retained = a.db.get(PlatformSetting, KEY)
    assert course.slug in {c["slug"] for c in _context(a, monkeypatch)["courses"]}
    a.admin.execute(update(PlatformSetting).where(PlatformSetting.key == KEY).values(value="[]"))
    a.admin.commit()
    assert retained.value == '["curriculum-reviewer"]'
    assert course.slug not in {c["slug"] for c in _context(a, monkeypatch)["courses"]}
    assert retained.value == '["curriculum-reviewer"]'


def test_other_tenant_membership_never_grants_staff(current_account, tenant_b, monkeypatch):
    a = current_account
    _policy(a, ["curriculum-reviewer"])
    # Canonical composite FKs forbid attaching A's party UUID to B. A genuine
    # B party with the same email and configured role must not authorize A.
    other = Party(tenant_id=tenant_b.id, party_type=PartyType.person,
                  display_name="Other tenant account", email=a.email, is_active=True)
    a.admin.add(other)
    a.admin.flush()
    _grant(a, "curriculum-reviewer", tenant_b.id, other.id)
    course = _course(a)
    assert course.slug not in {c["slug"] for c in _context(a, monkeypatch)["courses"]}


@pytest.mark.parametrize("value", [None, {}, "admin", ["admin", "admin"], [""], [" admin"], [True]])
def test_settings_rejects_malformed_role_policy(value):
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: value})


def test_settings_accepts_unique_custom_roles_and_empty_policy():
    assert Settings(**{KEY: []}).learning_search_staff_roles == ()
    assert Settings(**{KEY: ["custom:review", "Course-Staff"]}).learning_search_staff_roles == (
        "custom:review", "Course-Staff",
    )
