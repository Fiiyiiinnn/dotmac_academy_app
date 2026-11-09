"""Hosted attachment integration against genuine canonical and Learning owners.

Authored only; canonical assembly/FK migration remains a hosted prerequisite.
Only PDF rendering is stubbed: both public Learning reads execute real SQL.
"""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from dotmac_learning import CertificateFact, CourseWrite, create_course, get_certificate
from dotmac_learning.models import Certificate
from sqlalchemy import delete, update

from app.models.identity_extensions import AcademyAccountProfile
from app.services.api_identity_reads import certificate_recipient
from app.services.email_outbox import _attachments
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


def _seed(a):
    course = create_course(a.admin, CourseWrite(
        id=uuid4(), tenant_id=a.tid, slug=uuid4().hex, title="Historical Unicode 学",
        discipline="test", source_ref="read-fixture", status="draft", listed=False,
    ))
    cert = Certificate(tenant_id=a.tid, person_id=a.pid, course_id=course.id,
                       serial=f"SERIAL-{uuid4().hex[:16]}", issued_at=datetime(2026, 10, 1, tzinfo=UTC))
    a.admin.add(cert)
    a.admin.commit()
    return SimpleNamespace(kind="certificate", tenant_id=a.tid,
                           payload={"person_id": str(a.pid), "course_id": str(course.id)}), cert


@pytest.mark.parametrize("profile", [True, False])
def test_inactive_historical_recipient_needs_no_profile_or_completion(current_account, profile, monkeypatch):
    a = current_account
    row, cert = _seed(a)
    a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    a.admin.execute(update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name="Current 学", last_name=""))
    if not profile:
        a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
    a.admin.commit()
    rendered = []
    monkeypatch.setattr("app.services.certificates.render_certificate_pdf",
                        lambda **kw: rendered.append(kw) or b"pdf")
    root = a.db.get_transaction()
    assert _attachments(a.db, row) == [("certificate.pdf", b"pdf", "application/pdf")]
    fact = get_certificate(a.db, tenant_id=a.tid, person_id=a.pid, course_id=cert.course_id)
    assert isinstance(fact, CertificateFact)
    assert rendered == [dict(recipient_name="Current 学", course_title="Historical Unicode 学",
                             serial=cert.serial, issued_at=cert.issued_at)]
    assert a.db.get_transaction() is root and not a.db.new


@pytest.mark.parametrize("missing", ["person", "course", "certificate", "subtype", "foreign"])
def test_missing_attachment_state_preserves_failure(current_account, tenant_b, missing, monkeypatch):
    a = current_account
    row, cert = _seed(a)
    if missing == "person":
        row.payload["person_id"] = str(uuid4())
    elif missing == "course":
        row.payload["course_id"] = str(uuid4())
    elif missing == "certificate":
        a.admin.delete(cert)
        a.admin.commit()
    elif missing == "foreign":
        row.tenant_id = tenant_b.id
    else:
        a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
        a.admin.execute(delete(PartyPerson).where(PartyPerson.party_id == a.pid))
        a.admin.commit()
    def unexpected(**kwargs):
        pytest.fail("missing state must fail before rendering")
    monkeypatch.setattr("app.services.certificates.render_certificate_pdf", unexpected)
    with pytest.raises(ValueError, match="certificate attachment state is missing"):
        _attachments(a.db, row)
    assert certificate_recipient(a.db, tenant_id=tenant_b.id, person_id=a.pid) is None


@pytest.mark.parametrize("payload", [{}, {"person_id": "bad", "course_id": "bad"}])
def test_malformed_payload_keeps_error(payload):
    row = SimpleNamespace(kind="certificate", tenant_id=uuid4(), payload=payload)
    with pytest.raises(ValueError, match="invalid certificate attachment payload"):
        _attachments(object(), row)
