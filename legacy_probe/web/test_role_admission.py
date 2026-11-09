"""Role admission over the real web path: a signed-in account with no standard role is refused.

The Academy web facet admits only student/instructor/admin (permission academy.web.access).
This is the behavioural proof: real login form, the kernel's double-submit CSRF and the kernel
admission path, with nothing patched. The same account then receives the student role and is
admitted. (tests/architecture/test_academy_web_facet.py proves the declaration; this proves the
behaviour. See also the warning in tests.conftest.grant_role.)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

PASSWORD = "password1-role-admission"
PORTAL_PAGE = "/progress"
HOST = {"Host": "alpha.localhost"}


def _login_without_refresh(client: TestClient, email: str) -> None:
    """Real login form with CSRF; no authenticated GET afterwards.

    ``web_login`` refreshes the CSRF token with a GET of an authenticated page, which a role-less
    account is (correctly) refused, so the refresh step cannot be used here.
    """
    page = client.get("/login", headers=HOST, follow_redirects=False)
    csrf = page.cookies.get("csrf_token") or client.cookies.get("csrf_token", "")
    assert csrf, "csrf_token cookie not set by GET /login"
    response = client.post(
        "/login",
        headers={**HOST, "x-csrf-token": csrf},
        data={"email": email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"login for {email!r} returned {response.status_code}"
    assert client.cookies.get("session"), "login did not establish a session"


def test_roleless_account_gets_403_and_the_same_account_with_student_role_gets_200(admin_session, tenant_a):
    email = "roleless@a.edu"
    ensure_roles(admin_session, tenant_a.id)
    party = make_person_party(admin_session, tenant_id=tenant_a.id, email=email, first_name="Role", last_name="Less")
    make_person_credential(
        admin_session, tenant_id=tenant_a.id, party_id=party.id, email=email, password_hash=hash_password(PASSWORD)
    )
    admin_session.commit()

    # Signed in, but no student/instructor/admin grant: authenticated yet not admitted.
    roleless = TestClient(app)
    _login_without_refresh(roleless, email)
    denied = roleless.get(PORTAL_PAGE, headers=HOST, follow_redirects=False)
    assert denied.status_code == 403, f"expected 403 for a role-less account, got {denied.status_code}"

    # The same account, now holding the student role, signs in afresh and is admitted.
    grant_role(admin_session, tenant_id=tenant_a.id, party_id=party.id, role="student")
    admin_session.commit()
    student = TestClient(app)
    web_login(student, HOST, email, PASSWORD)
    admitted = student.get(PORTAL_PAGE, headers=HOST, follow_redirects=False)
    assert admitted.status_code == 200, f"expected 200 for the student account, got {admitted.status_code}"
