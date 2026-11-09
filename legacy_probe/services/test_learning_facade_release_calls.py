"""The Academy facades bind the installed Learning read/policy contracts."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.credential_lifecycle import CredentialLifecycleError

from app.services import credential_authentication, roster, todo
from app.services.exceptions import NotFoundError


def test_timeline_passes_the_configured_feedback_policy_to_learning(monkeypatch):
    db, feedback = object(), object()
    tenant, person = uuid4(), uuid4()
    terms = SimpleNamespace(near_deadline_days=1, far_deadline_days=7, window_horizon_days=30)
    monkeypatch.setattr(todo, "todo_terms", lambda actual: terms)
    monkeypatch.setattr(todo, "feedback_reveal_terms", lambda actual: feedback)
    monkeypatch.setattr(todo, "effective", lambda actual, **kwargs: {"learning_join_lead_minutes": 15})
    seen = []

    def owner(actual, *, tenant_id, person_id, now, local_zone, terms, feedback_terms, join_lead_minutes):
        seen.append((actual, tenant_id, person_id, feedback_terms, join_lead_minutes))
        return SimpleNamespace(graded=(), feedback=(), overdue=(), due_today=(), next_7=(), next_30=(),
                               sessions=(), joinable=(), course_windows=())

    monkeypatch.setattr(todo.learning, "timeline_for_person", owner)
    result = todo.timeline_for_person(db, tenant_id=tenant, person_id=person)
    assert seen == [(db, tenant, person, feedback, 15)]
    assert result["feedback"] == []


def test_bulk_enroll_uses_the_installed_tenant_scoped_cohort_reader(monkeypatch):
    tenant, cohort = uuid4(), uuid4()
    request, db = object(), object()
    checked = []

    @contextmanager
    def human(actual, *, request, cohort_id, emails, track_id):
        assert actual is db and cohort_id == cohort
        yield SimpleNamespace(tenant_id=tenant, revalidate=lambda: checked.append("authority"))

    def exists(actual, *, tenant_id, cohort_id):
        checked.append((actual, tenant_id, cohort_id))
        return False

    monkeypatch.setattr(roster.audit_context, "bulk_enroll_web_roster", human)
    monkeypatch.setattr(roster.learning, "instructor_cohort_exists", exists)
    with pytest.raises(NotFoundError, match="cohort not found for tenant"):
        roster.bulk_enroll(db, request=request, tenant_id=tenant, cohort_id=cohort, emails=())
    assert checked == ["authority", (db, tenant, cohort)]


@pytest.mark.parametrize("method", ["record_receipt", "find_receipt"])
def test_sign_in_ports_explicitly_refuse_recovery_receipt_operations(method):
    with pytest.raises(CredentialLifecycleError, match="outside canonical sign-in"):
        getattr(credential_authentication._Unsupported(), method)(object(), object())
