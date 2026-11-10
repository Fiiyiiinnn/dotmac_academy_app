"""Academy Keycloak (OIDC) staff login: config gate + hosted service proof.

The hosted tests run the genuine ceremony state store, the kernel's
locking external-login finalizer and Academy session issuance against
the per-test genesis database. Only the provider protocol client is
faked (it returns an already-verified subject, which is exactly the
``dotmac-auth-oidc`` output boundary); the real provider leg is the
operator-run live proof in docs/oidc-login.md.
"""

from __future__ import annotations

import time

import pytest
from dotmac_auth_oidc import AuthorizationRedirect, LoginState, VerifiedSubject
from dotmac_auth_oidc.errors import StateError
from dotmac_kernel.external_identity import bind_external_identity
from dotmac_kernel.models import AuthSession
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings, validate_settings
from app.services import oidc_login
from tests.issue2_wave3_support import current_account, grant_std, make_person_party

assert current_account  # Imported pytest fixture.

ISSUER = "https://idp.dotmac.io/realms/dotmac"
SUBJECT = "3f6b1f90-0000-4000-8000-000000000001"


def test_oidc_enabled_without_secret_is_a_configuration_error():
    errors = validate_settings(Settings(oidc_enabled=True, oidc_client_secret=""))
    assert any("OIDC_CLIENT_SECRET" in error for error in errors)


def test_oidc_enabled_with_complete_config_has_no_oidc_error():
    errors = validate_settings(Settings(oidc_enabled=True, oidc_client_secret="test-secret"))
    assert not any("OIDC" in error for error in errors)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("instructor,admin", ("instructor", "admin")),
        (" instructor , custom-staff ", ("instructor", "custom-staff")),
        ('["instructor", "admin"]', ("instructor", "admin")),
        ("", ()),
    ],
)
def test_oidc_staff_roles_load_from_environment(monkeypatch, value, expected):
    monkeypatch.setenv("OIDC_STAFF_ROLES", value)
    assert Settings(_env_file=None).oidc_staff_roles == expected


@pytest.mark.parametrize("value", ["instructor,", "admin,admin", '["admin", 1]', "[invalid"])
def test_malformed_oidc_staff_roles_refuse_configuration(monkeypatch, value):
    monkeypatch.setenv("OIDC_STAFF_ROLES", value)
    with pytest.raises(ValidationError, match="oidc_staff_roles"):
        Settings(_env_file=None)


class _FakeClient:
    """Stands in for dotmac-auth-oidc at its verified-subject boundary."""

    def __init__(self, subject: str) -> None:
        self._subject = subject

    def start_login(self, *, return_to="/", ttl_seconds=600, state_store=None):
        state = LoginState(
            state_id="ceremony-1",
            nonce="nonce-1",
            code_verifier="verifier-1",
            redirect_uri="https://academy.dotmac.io/login/oidc/callback",
            issued_at=int(time.time()),
            return_to=return_to,
        )
        state_store.put(state, ttl_seconds=ttl_seconds)
        return AuthorizationRedirect(
            url=f"{ISSUER}/protocol/openid-connect/auth?state=ceremony-1",
            state="ceremony-1",
        )

    def complete_login(self, *, code, state_parameter, stored_state, ttl_seconds=600, state_store=None):
        if state_parameter != stored_state:
            raise StateError("state mismatch")
        claimed = state_store.take(state_parameter)
        if claimed is None:
            raise StateError("ceremony unavailable")
        return VerifiedSubject(issuer=ISSUER, subject=self._subject, return_to=claimed.return_to)


def _staff_party(a, *, email: str, role: str = "instructor"):
    party = make_person_party(a.admin, tenant_id=a.tid, email=email, first_name="Staff", last_name="Member")
    grant_std(a.admin, tenant_id=a.tid, party_id=party.id, role=role)
    a.admin.commit()
    return party


def test_bound_staff_member_completes_login_with_provenance(current_account):
    a = current_account
    party = _staff_party(a, email="oidc-staff@a.edu")
    binding = oidc_login.bind_staff_identity(
        a.db, tenant=a.tenant, party=party, subject=SUBJECT, bound_by="test-operator", reason="oidc pilot proof"
    )
    a.db.commit()

    client = _FakeClient(SUBJECT)
    started = oidc_login.begin_login(a.db, tenant=a.tenant, client=client)
    a.db.commit()
    completed = oidc_login.complete_login(
        a.db, tenant=a.tenant, code="code-1", state=started.state, stored_state=started.state, client=client
    )
    assert completed.account.id == party.id
    session = a.db.scalar(select(AuthSession).where(AuthSession.tenant_id == a.tid, AuthSession.party_id == party.id))
    assert session is not None
    assert session.external_identity_binding_id == binding.id

    # The ceremony is single-use: replaying the callback is refused.
    with pytest.raises(oidc_login.OidcLoginRefused):
        oidc_login.complete_login(
            a.db, tenant=a.tenant, code="code-1", state=started.state, stored_state=started.state, client=client
        )


def test_unbound_subject_and_student_binding_are_refused(current_account):
    a = current_account
    student = _staff_party(a, email="oidc-student@a.edu", role="student")

    # The bind path itself refuses non-staff for the pilot cohort.
    with pytest.raises(ValueError, match="staff"):
        oidc_login.bind_staff_identity(
            a.db, tenant=a.tenant, party=student, subject=SUBJECT, bound_by="test-operator", reason="oidc pilot proof"
        )
    a.db.rollback()

    # Even a binding created directly through the kernel cannot mint a
    # student session: the login service re-checks the staff cohort.
    with a.db.begin():
        bind_external_identity(
            a.db,
            tenant=a.tenant,
            party=student,
            provider_binding=oidc_login.provider_binding(),
            issuer=ISSUER,
            subject=SUBJECT,
            bound_by="test-operator",
            reason="direct kernel bind",
        )
    client = _FakeClient(SUBJECT)
    started = oidc_login.begin_login(a.db, tenant=a.tenant, client=client)
    a.db.commit()
    with pytest.raises(oidc_login.OidcLoginRefused):
        oidc_login.complete_login(
            a.db, tenant=a.tenant, code="code-1", state=started.state, stored_state=started.state, client=client
        )
    a.db.rollback()

    # An entirely unbound subject is the same refusal shape.
    client = _FakeClient("00000000-0000-4000-8000-000000000099")
    started = oidc_login.begin_login(a.db, tenant=a.tenant, client=client)
    a.db.commit()
    with pytest.raises(oidc_login.OidcLoginRefused):
        oidc_login.complete_login(
            a.db, tenant=a.tenant, code="code-1", state=started.state, stored_state=started.state, client=client
        )
