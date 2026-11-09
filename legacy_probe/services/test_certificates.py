"""Certificate issuance + PDF rendering (Slice 2d)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.certificate import Certificate
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.services.certificates import issue_certificate, render_certificate_pdf
from app.services.exceptions import ConflictError
from legacy_probe.conftest import make_person_party


def _person_course(db, tid):
    p = make_person_party(db, tenant_id=tid, email="cert@a.edu", first_name="Cert", last_name="Holder")
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    db.add_all([p, c])
    db.flush()
    return p, c

def _complete(db, tid, p, c):
    db.add(CourseCompletion(tenant_id=tid, person_id=p.id, course_id=c.id,
                            status="completed", pct=1.0, completed_at=datetime.now(UTC)))
    db.flush()

def test_issue_requires_completion(admin_session, tenant_a):
    p, c = _person_course(admin_session, tenant_a.id)
    # No completion record → cannot issue.
    with pytest.raises(ConflictError):
        issue_certificate(admin_session, tenant_id=tenant_a.id, person_id=p.id, course_id=c.id)
    assert admin_session.query(CourseCompletion).filter_by(
        tenant_id=tenant_a.id, person_id=p.id, course_id=c.id
    ).count() == 0
    admin_session.rollback()

def test_issue_is_idempotent(admin_session, tenant_a):
    p, c = _person_course(admin_session, tenant_a.id)
    _complete(admin_session, tenant_a.id, p, c)
    now = datetime(2026, 6, 27, tzinfo=UTC)
    cert1 = issue_certificate(admin_session, tenant_id=tenant_a.id, person_id=p.id,
                              course_id=c.id, now=now)
    cert2 = issue_certificate(admin_session, tenant_id=tenant_a.id, person_id=p.id,
                              course_id=c.id, now=datetime(2026, 7, 1, tzinfo=UTC))
    assert cert1.serial == cert2.serial
    assert cert1.issued_at == now
    n = admin_session.query(Certificate).filter(
        Certificate.tenant_id == tenant_a.id, Certificate.person_id == p.id,
        Certificate.course_id == c.id).count()
    assert n == 1
    admin_session.rollback()

def test_render_pdf_bytes(admin_session, tenant_a):
    pdf = render_certificate_pdf(recipient_name="Ada Lovelace", course_title="Networking 101",
                                 serial="CERT-ABC123", issued_at=datetime(2026, 6, 27, tzinfo=UTC))
    assert isinstance(pdf, bytes | bytearray)
    assert bytes(pdf[:5]) == b"%PDF-"
    assert len(pdf) > 500

# Retired in C1 with app/services/completion_aggregate.py: test_concurrent_issuers_recheck_certificate_after_completion_lock
# monkeypatched ensure_locked_completion/lock_existing_completion, which Learning
# now owns. Concurrency intent is re-proved at the Learning boundary (see C1 receipt).

