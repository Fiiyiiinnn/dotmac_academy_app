"""A2 pure call-site proofs that need no database.

Covers the HM-04 certificate-None completion path and the declared HM-09
success_queue severity import seam against final Learning. Hosted DB
goldens for the remaining HM items live in their adapter test files.
"""

import sys
import types
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning

from app.services import completion

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class _Db:
    @contextmanager
    def begin_nested(self):
        yield


def _completion(course_id, person_id, tenant_id):
    return learning.CompletionFact(
        id=uuid4(),
        tenant_id=tenant_id,
        person_id=person_id,
        course_id=course_id,
        status="completed",
        pct=100.0,
        completed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _observe(monkeypatch):
    calls = {"cert_delivery": [], "notify": [], "email": []}
    monkeypatch.setattr(completion, "get_course", lambda db, **kw: SimpleNamespace(title="Fiber Basics"))
    certificates = types.ModuleType("app.services.certificates")
    certificates.deliver_certificate_notification = (
        lambda db, certificate: calls["cert_delivery"].append(certificate)
    )
    monkeypatch.setitem(sys.modules, "app.services.certificates", certificates)
    monkeypatch.setattr(
        "app.services.notifications.notify", lambda db, **kw: calls["notify"].append(kw)
    )
    monkeypatch.setattr(completion, "_email_certificate", lambda db, **kw: calls["email"].append(kw))
    return calls


def test_first_completion_without_certificate_notifies_and_skips_cert_delivery(monkeypatch):
    tenant_id, person_id, course_id = uuid4(), uuid4(), uuid4()
    calls = _observe(monkeypatch)
    result = learning.CompletionResult(
        completion=_completion(course_id, person_id, tenant_id),
        first_completion=True,
        certificate=None,
    )
    completion.deliver_completion_notifications(_Db(), result)
    assert calls["cert_delivery"] == []
    assert calls["email"] == []
    assert calls["notify"] == [
        {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "kind": "course_completed",
            "title": "Well done, course completed",
            "body": "Congratulations on completing Fiber Basics.",
            "link": "/progress",
        }
    ]


def test_first_completion_with_certificate_keeps_historical_delivery(monkeypatch):
    tenant_id, person_id, course_id = uuid4(), uuid4(), uuid4()
    calls = _observe(monkeypatch)
    certificate = learning.CertificateIssue(
        certificate=learning.CertificateFact(
            id=uuid4(),
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            serial="CERT-1",
            issued_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        ),
        created=True,
    )
    result = learning.CompletionResult(
        completion=_completion(course_id, person_id, tenant_id),
        first_completion=True,
        certificate=certificate,
    )
    completion.deliver_completion_notifications(_Db(), result)
    assert calls["cert_delivery"] == [certificate]
    assert calls["notify"][0]["link"] == f"/certificates/{course_id}"
    assert len(calls["email"]) == 1


def test_success_queue_severity_order_is_the_learning_submodule_ordering():
    from dotmac_learning.success_queue import severity_order as owner_order

    from app.services import success_queue

    assert success_queue.severity_order is owner_order
