"""Hosted historical canonical audit reads; no writer-registration proof."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from dotmac_kernel.audit import AuditEvent
from sqlalchemy import select

from app.services.admissions import applicant_transition_history
from app.services.api_identity_reads import WebAuditView, newest_applicant_invitation_event
from app.web.applications import _invitation_export_info
from legacy_probe.services.test_canonical_audit_web_read import seed_historical_event

HISTORY = (
    "applicant.transition", "applicant.transition_baseline", "applicant.assessment_reset",
    "applicant.assessment_reinvite", "applicant.invitation_resent", "applicant.access_extended",
    "applicant.intake_assigned",
)


def test_full_history_is_one_exact_tenant_entity_multi_action_query(app_client, admin_session, tenant_a, tenant_b):
    db = admin_session
    applicant = SimpleNamespace(id=uuid4(), tenant_id=tenant_a.id, email="empty@a.edu",
                                source="fallback", assessment_deadline=None)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(111):
        seed_historical_event(db, tenant_id=tenant_a.id, entity_type="applicant",
                              entity_id=str(applicant.id), action=HISTORY[i % len(HISTORY)],
                              created_at=start + timedelta(seconds=i), details={"source": f"source-{i}"})
    for tid, entity, eid, action in [
        (tenant_b.id, "applicant", str(applicant.id), HISTORY[0]),
        (tenant_a.id, "other", str(applicant.id), HISTORY[0]),
        (tenant_a.id, "applicant", str(uuid4()), HISTORY[0]),
        (tenant_a.id, "applicant", str(applicant.id), "other.action"),
    ]:
        seed_historical_event(db, tenant_id=tid, entity_type=entity, entity_id=eid,
                              action=action, created_at=start + timedelta(days=1))
    db.flush()
    root = db.get_transaction()
    expected = db.scalars(select(AuditEvent).where(
        AuditEvent.tenant_id == applicant.tenant_id, AuditEvent.entity_type == "applicant",
        AuditEvent.entity_id == str(applicant.id), AuditEvent.action.in_(HISTORY),
    ).order_by(AuditEvent.created_at.desc())).all()
    facts = applicant_transition_history(db, applicant=applicant)
    assert len(facts) == 111 and all(isinstance(f, WebAuditView) for f in facts)
    assert [f.id for f in facts] == [f.id for f in expected]
    assert {f.action for f in facts} == set(HISTORY)
    # Latest history action is not an invitation action: newest is selected
    # over the exact five invitation actions before any SQL LIMIT.
    newest = newest_applicant_invitation_event(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)
    assert newest.details == {"source": "source-109"}
    assert _invitation_export_info(db, applicant)["How Invited"] == "source-109"
    facts[0].details["source"] = "mutated"
    assert applicant_transition_history(db, applicant=applicant)[0].details != facts[0].details
    assert db.get_transaction() is root


def test_ties_and_baseline_are_preserved_and_export_falls_back(app_client, admin_session, tenant_a):
    db = admin_session
    applicant = SimpleNamespace(id=uuid4(), tenant_id=tenant_a.id, email="none@a.edu",
                                source="historical-fallback", assessment_deadline=None)
    assert applicant_transition_history(db, applicant=applicant) == []
    assert newest_applicant_invitation_event(db, tenant_id=tenant_a.id, applicant_id=applicant.id) is None
    assert _invitation_export_info(db, applicant)["How Invited"] == "historical-fallback"
    when = datetime(2026, 10, 1, tzinfo=UTC)
    events = [seed_historical_event(db, tenant_id=tenant_a.id, entity_type="applicant",
                                   entity_id=str(applicant.id), action="applicant.transition_baseline",
                                   created_at=when, details={"source": f"baseline-{i}"}) for i in range(2)]
    facts = applicant_transition_history(db, applicant=applicant)
    assert {f.id for f in facts} == {e.id for e in events} and len(facts) == 2
    assert all(f.created_at == when for f in facts)
    assert _invitation_export_info(db, applicant)["How Invited"] in {"baseline-0", "baseline-1"}
