"""Authored hosted canaries for canonical admin-report recipient admission.

Requires the genuine migrated assembly via the existing hosted account fixture.
No source-capsule activation or installed qualification is implied.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, Role
from sqlalchemy import delete, select, update

from app.models.identity_extensions import AcademyAccountProfile
from app.services import admin_reports
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


@pytest.mark.parametrize(
    "active,status,grant_slug,eligible",
    [
        (True, "active", "admin", True),
        (False, "active", "admin", True),
        (True, "suspended", "admin", False),
        (True, "active", "instructor", False),
    ],
)
def test_admin_recipients_keep_profile_status_only_admission_and_live_email(
    current_account, tenant_b, active, status, grant_slug, eligible
):
    a = current_account
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == grant_slug)).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug=grant_slug, name=grant_slug)
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    email = f"admin-report-{uuid4().hex}@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=email, is_active=active))
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(status=status, prefs={"nested": {"unhashable": [1, 2]}})
    )
    a.admin.commit()
    recipients = admin_reports.admin_recipients(a.admin, tenant_id=a.tid)
    ours = [row for row in recipients if row.id == a.pid]
    assert len(ours) == int(eligible)
    if eligible:
        assert ours[0].email == email and tuple(ours[0]._mapping) == ("id", "email")
    assert a.pid not in {row.id for row in admin_reports.admin_recipients(a.admin, tenant_id=tenant_b.id)}


@pytest.mark.parametrize("missing", ["profile", "subtype"])
def test_admin_recipient_requires_canonical_profile_and_person_subtype(current_account, missing):
    a = current_account
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="admin", name="Admin")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
    if missing == "subtype":
        a.admin.execute(delete(PartyPerson).where(PartyPerson.party_id == a.pid))
    a.admin.commit()
    assert a.pid not in {row.id for row in admin_reports.admin_recipients(a.admin, tenant_id=a.tid)}


def test_admin_report_sender_consumes_uuid_email_projection_without_transaction_ownership(current_account, monkeypatch):
    a = current_account
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="admin", name="Admin")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.commit()
    snapshot_calls, queued = [], []
    monkeypatch.setattr(admin_reports, "activity_snapshot", lambda db, **kw: snapshot_calls.append(kw) or {})
    monkeypatch.setattr(admin_reports, "_render", lambda snapshot: ("html", "text"))
    monkeypatch.setattr(admin_reports, "enqueue_email", lambda db, **kw: queued.append(kw) or True)
    before = datetime.now(UTC).strftime("%Y-%m-%d")
    count = admin_reports.send_activity_report(a.admin, tenant_id=a.tid, hours=0)
    after = datetime.now(UTC).strftime("%Y-%m-%d")
    ours = [item for item in queued if item["recipient"] == a.email]
    assert count == len(queued) and len(ours) == 1
    assert snapshot_calls[0]["tenant_id"] == a.tid
    item = ours[0]
    assert item["idempotency_key"] in {f"admin-report:{a.pid}:{day}:0" for day in (before, after)}
    assert item["tenant_id"] == a.tid and item["kind"] == "admin_report"
    assert item["html_body"] == "html" and item["text_body"] == "text"
    assert a.admin.in_transaction()
