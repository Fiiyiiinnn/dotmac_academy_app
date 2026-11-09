"""Authored hosted canaries for the offline CLI digest's canonical identity read.

Requires the genuine migrated assembly and existing hosted current-account fixture.
The source capsule does not run or activate these canaries.
"""

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from sqlalchemy import update

from app import cli
from app.models.cohort import Cohort, Enrollment
from app.models.identity_extensions import AcademyAccountProfile
from app.services import email, email_outbox, lab_jobs, reports, settings_store
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


@pytest.mark.parametrize(
    "active,account_status,prefs,role,enrollment_status,expected",
    [
        (True, "active", {}, "instructor", "active", 2),
        (False, "suspended", {"other_preference": False}, "instructor", "active", 2),
        (True, "active", {"email_digest": False}, "instructor", "active", 0),
        (True, "active", {}, "student", "active", 0),
        (True, "active", {}, "instructor", "dropped", 0),
    ],
)
def test_cli_digest_canonical_current_fields_preserve_enrollment_admission_and_cohort_keys(
    current_account, monkeypatch, capsys, active, account_status, prefs, role, enrollment_status, expected
):
    a = current_account
    new_email = f"cli-digest-{uuid4().hex}@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=new_email, is_active=active))
    a.admin.execute(
        update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name="Live", last_name="Name")
    )
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(status=account_status, prefs=prefs)
    )
    cohorts = [Cohort(tenant_id=a.tid, name="Same name", discipline="networking", status="inactive") for _ in range(2)]
    a.admin.add_all(cohorts)
    a.admin.flush()
    cids = {c.id for c in cohorts}
    a.admin.add_all(
        Enrollment(tenant_id=a.tid, cohort_id=c.id, person_id=a.pid, role_in_cohort=role, status=enrollment_status)
        for c in cohorts
    )
    a.admin.commit()

    @contextmanager
    def admin_session():
        yield a.admin

    rows, queued, matrix_calls = [], [], []
    original_allows = email.recipient_allows

    def allows(row, kind):
        if row.id == a.pid:
            rows.append(row)
        return original_allows(row, kind)

    def matrix(db, *, tenant_id, cohort_id):
        matrix_calls.append((tenant_id, cohort_id))
        return "matrix"

    monkeypatch.setattr(lab_jobs, "admin_session", admin_session)
    monkeypatch.setattr(settings_store, "effective", lambda db: SimpleNamespace(email_digest_enabled=True))
    monkeypatch.setattr(reports, "cohort_matrix", matrix)
    monkeypatch.setattr(email, "recipient_allows", allows)
    monkeypatch.setattr(email, "render_cohort_html", lambda matrix: "rendered matrix")
    monkeypatch.setattr(email_outbox, "enqueue_email", lambda db, **kw: queued.append(kw) or True)
    before = datetime.now(UTC).strftime("%G-W%V")
    cli._email_digest(argparse.Namespace())
    after = datetime.now(UTC).strftime("%G-W%V")
    ours = [q for q in queued if q["payload"]["cohort_id"] in {str(cid) for cid in cids}]
    assert len(ours) == expected
    assert all((a.tid, cid) in matrix_calls for cid in cids)
    assert all(
        row.email == new_email and row.first_name == "Live" and row.last_name == "Name" and row.prefs == prefs
        for row in rows
    )
    assert len(rows) == (2 if role == "instructor" and enrollment_status == "active" else 0)
    for item in ours:
        cid = item["payload"]["cohort_id"]
        assert item["tenant_id"] == a.tid and item["recipient"] == new_email
        assert item["kind"] == "weekly_digest" and item["html_body"] == "rendered matrix"
        assert item["subject"] == "Weekly progress digest — Same name"
        assert item["idempotency_key"] in {f"weekly-digest:{period}:{cid}:{a.pid}" for period in (before, after)}
    assert f"email-digest: queued {len(queued)} message(s)" in capsys.readouterr().out
