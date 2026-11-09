"""Maintained port of legacy_probe/test_end_to_end.py ci-skips (issue #2 wave 5).

Repo-owned fixture content under tests/fixtures/foundation/ replaces the
author-local manuals path: two lint-clean BankDoc YAMLs, three chapters and
an empty figures/ directory. The legacy web-login leg used the shared
hosted ``app_client``; here the login leg is the genuine ``sign_in``
service against the per-test genesis database, and the tenant/account are
seeded canonically because ``bootstrap_tenant`` requires the process-wide
identity-provisioning assembly (recorded as a finding in the wave report).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dotmac_kernel.models import Party, PartyPerson, PartyType, Tenant, UserCredential
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.bank_loader import lint_bank, load_bank, parse_bank
from app.services.bootstrap import ensure_roles
from app.services.content_import import import_foundation
from app.services.credential_authentication import sign_in
from app.services.security import hash_password

FOUNDATION = Path(__file__).resolve().parent / "fixtures" / "foundation"
FIGS = FOUNDATION / "figures"


def _install_audit_actions_best_effort() -> None:
    try:
        from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions

        from app.owner_composition import compose_migration_owner_declarations

        install_audit_actions(AuditActionRegistry.from_manifests(compose_migration_owner_declarations()))
    except Exception:  # noqa: S110 - audit registry may already be installed process-wide
        pass


def test_banks_lint_clean_fixture_banks():
    """Both fixture banks must parse and pass lint with zero violations."""
    for name in ("foundation-ch03.yaml", "foundation-ch08.yaml"):
        doc = parse_bank(FOUNDATION / "assessments" / "banks" / name)
        violations = lint_bank(doc)
        assert violations == [], f"{name} lint violations: {violations}"


def test_full_flow_fixture_foundation(genesis_database):
    """Seed a tenant, import fixture content, load a bank, sign in."""
    admin, _superuser, _name = genesis_database
    _install_audit_actions_best_effort()
    with Session(admin) as db:
        tenant = Tenant(slug="e2eacad", name="E2E Academy")
        db.add(tenant)
        db.flush()
        roles = ensure_roles(db, tenant.id)
        assert "student" in roles
        now = datetime.now(UTC)
        party = Party(
            tenant_id=tenant.id,
            party_type=PartyType.person,
            display_name="E2E Admin",
            email="admin@e2eacad.edu",
            is_active=True,
        )
        db.add(party)
        db.flush()
        db.add(PartyPerson(party_id=party.id, first_name="E2E", last_name="Admin"))
        db.flush()
        db.add(
            AcademyAccountProfile(
                tenant_id=tenant.id, party_id=party.id, status="active", prefs={}, created_at=now, updated_at=now
            )
        )
        credential = UserCredential(tenant_id=tenant.id, party_id=party.id, password_hash=hash_password("password1"))
        db.add(credential)
        db.flush()
        db.add(
            AcademyCredentialPolicy(
                tenant_id=tenant.id,
                credential_id=credential.id,
                email=party.email,
                failed_login_attempts=0,
                locked_until=None,
                lifecycle_version=0,
                reset_required=False,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()
        db.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(tenant.id)},
        )
        course = import_foundation(
            db,
            tenant_id=tenant.id,
            chapters_dir=FOUNDATION / "chapters",
            figures_dir=FIGS,
        )
        doc = parse_bank(FOUNDATION / "assessments" / "banks" / "foundation-ch03.yaml")
        bank = load_bank(db, tenant_id=tenant.id, course_id=course.id, doc=doc)
        db.commit()
        db.execute(
            text("SELECT set_config('app.current_tenant', :t, true)"),
            {"t": str(tenant.id)},
        )
        chapters = db.execute(
            text("SELECT count(*) FROM mod_learning.chapters WHERE tenant_id = :t AND course_id = :c"),
            {"t": tenant.id, "c": course.id},
        ).scalar_one()
        assert chapters == 3
        assert bank is not None
        result = sign_in(db, tenant_id=tenant.id, email="admin@e2eacad.edu", password="password1")
        assert result is not None
        assert result.account.tenant_id == tenant.id
        assert result.account.id == party.id
        db.rollback()
