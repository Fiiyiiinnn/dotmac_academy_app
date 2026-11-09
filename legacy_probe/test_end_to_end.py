"""End-to-end smoke test: bootstrap → import content → load banks → login → read → test.

Proves the full stack works together:
  1. test_banks_lint_clean — YAML banks parse and lint-validate cleanly.
  2. test_full_flow — a real tenant is bootstrapped, Foundation content imported,
     a bank loaded, an Activity created, the admin person given the student role,
     login succeeds, dashboard and chapter pages return 200.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from dotmac_kernel.models import Party, PartyRoleGrant
from sqlalchemy import text

from app.models.assessment import Activity
from app.services.bank_loader import lint_bank, load_bank, parse_bank
from app.services.bootstrap import bootstrap_tenant, ensure_roles
from app.services.content_import import import_foundation

ACADEMY = Path("/home/dotmac/projects/dotmac-academy/manuals/00-foundation")
FIGS = Path("/home/dotmac/projects/dotmac-academy/figures/final")

# This smoke test reads authored Foundation manuals from a fixed local path that
# only exists on an author's machine — skip cleanly where the content isn't present
# (CI, fresh clones) rather than fail on a missing directory.
pytestmark = pytest.mark.skipif(
    not ACADEMY.exists(), reason="Foundation manuals not present in this checkout"
)

# ─── Part A: lint only ────────────────────────────────────────────────────────

def test_banks_lint_clean():
    """Both authored banks must parse and pass lint with zero violations."""
    for name in ("foundation-ch03.yaml", "foundation-ch08.yaml"):
        doc = parse_bank(ACADEMY / "assessments" / "banks" / name)
        violations = lint_bank(doc)
        assert violations == [], f"{name} lint violations: {violations}"

# ─── Part B: full stack ───────────────────────────────────────────────────────

def test_full_flow(app_client, admin_session):
    """Bootstrap a tenant, import content, load a bank, log in, and read pages."""
    # ── Setup ──────────────────────────────────────────────────────────────────
    # Use a distinct slug to avoid colliding with the conftest's tenant_a ("alpha").
    t = bootstrap_tenant(
        admin_session,
        slug="e2eacad",
        name="E2E Academy",
        admin_email="admin@e2eacad.edu",
        admin_password="password1",
    )
    roles = ensure_roles(admin_session, t.id)

    course = import_foundation(
        admin_session,
        tenant_id=t.id,
        chapters_dir=ACADEMY / "chapters",
        figures_dir=FIGS,
    )

    doc = parse_bank(ACADEMY / "assessments" / "banks" / "foundation-ch03.yaml")
    bank = load_bank(admin_session, tenant_id=t.id, course_id=course.id, doc=doc)

    admin_session.add(
        Activity(
            tenant_id=t.id,
            course_id=course.id,
            chapter_number=3,
            type="mcq_test",
            bank_id=bank.id,
            title="Ch3 test",
            pass_threshold=0.6, grading="auto", weight=1.0,
        )
    )

    # Grant the admin person the student role so they can use the learner portal.
    from app.models.cohort import Cohort, Enrollment
    from app.models.offering import CourseOffering

    person = admin_session.query(Party).filter(Party.tenant_id == t.id).first()
    admin_session.add(
        PartyRoleGrant(tenant_id=t.id, party_id=person.id, role_id=roles["student"].id)
    )
    # Entitle the learner: cohort + enrollment + an offering for the course.
    coh = Cohort(tenant_id=t.id, name="E2E Cohort", discipline=course.discipline, status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=t.id, cohort_id=coh.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=t.id, cohort_id=coh.id, course_id=course.id,
                                     status="active"))
    admin_session.commit()

    # ── Web flow ───────────────────────────────────────────────────────────────
    try:
        h = {"Host": "e2eacad.localhost"}

        # GET /login sets the csrf_token cookie; the subsequent POST must double-submit it.
        r_login_page = app_client.get("/login", headers=h, follow_redirects=False)
        csrf = (
            r_login_page.cookies.get("csrf_token")
            or app_client.cookies.get("csrf_token", "")
        )

        assert csrf, "csrf_token cookie not set by GET /login"

        r_post = app_client.post(
            "/login",
            headers={**h, "x-csrf-token": csrf},
            data={"email": "admin@e2eacad.edu", "password": "password1"},
            follow_redirects=False,
        )
        assert r_post.status_code == 303, (
            f"Expected 303 from /login, got {r_post.status_code}: {r_post.text[:200]}"
        )

        # Authenticated requests — session cookie is stored in the TestClient jar.
        r_dash = app_client.get("/", headers=h)
        assert r_dash.status_code == 200, (
            f"Dashboard returned {r_dash.status_code}: {r_dash.text[:200]}"
        )

        r_chap = app_client.get("/courses/foundation/chapters/3", headers=h)
        assert r_chap.status_code == 200, (
            f"Chapter 3 returned {r_chap.status_code}: {r_chap.text[:200]}"
        )
    finally:
        # ── Cleanup ────────────────────────────────────────────────────────────────
        # Deleting the Tenant cascades (ON DELETE CASCADE) to all child rows
        # (courses, chapters, banks, questions, activities, people, sessions, …).
        admin_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)}
        )
        admin_session.commit()
