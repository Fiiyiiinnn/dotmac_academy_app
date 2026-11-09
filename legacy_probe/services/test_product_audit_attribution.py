"""Product legacy adapters keep the canonical actor contract explicit."""

from uuid import uuid4

import pytest
from dotmac_kernel.audit import MissingAuditActorError
from dotmac_kernel.audit_actions import AuditActionRegistry, UndeclaredAuditActionError

from app.owner_composition import compose_migration_owner_declarations
from app.services import admissions, audit
from app.services.exceptions import BadRequestError


class CaptureSession:
    def __init__(self):
        self.events = []

    def add(self, event):
        self.events.append(event)

    def flush(self):
        pass


@pytest.fixture
def audit_db(monkeypatch):
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    monkeypatch.setattr("dotmac_kernel.audit_actions._active_registry", registry)
    return CaptureSession()


def test_user_audit_keeps_explicit_principal_and_party(audit_db):
    actor = uuid4()
    row = audit.write_audit_event(
        audit_db, tenant_id=uuid4(), actor_type="user", actor_id=str(actor), actor_party_id=actor,
        source_application="dotmac_academy_app", action="facilitator_nomination.create",
        entity_type="facilitator_nomination",
    )
    assert (row.actor_type, row.actor_id, row.actor_party_id, row.source_application) == (
        "user", str(actor), actor, "dotmac_academy_app",
    )
    assert audit_db.events == [row]


def test_party_enrichment_cannot_supply_missing_actor_semantics(audit_db):
    with pytest.raises(MissingAuditActorError):
        audit.write_audit_event(
            audit_db, tenant_id=uuid4(), actor_type="user", actor_id=None, actor_party_id=uuid4(),
            source_application="dotmac_academy_app", action="facilitator_nomination.create",
            entity_type="facilitator_nomination",
        )
    assert not audit_db.events


def test_undeclared_product_action_is_refused_before_write(audit_db):
    with pytest.raises(UndeclaredAuditActionError):
        audit.write_audit_event(
            audit_db, tenant_id=uuid4(), actor_type="system", actor_id="fixed_component", actor_party_id=None,
            source_application="dotmac_academy_app", action="facilitator_nomination.unreviewed",
            entity_type="facilitator_nomination",
        )
    assert not audit_db.events


def test_legacy_assessment_bridge_is_explicitly_system_attributed(audit_db):
    row = admissions._legacy_write_audit_event(
        audit_db, tenant_id=uuid4(), actor_person_id=None,
        action="applicant.transition", entity_type="applicant",
    )
    assert (row.actor_type, row.actor_id, row.actor_party_id, row.source_application) == (
        "system", "assessment_policy", None, "dotmac_academy_app",
    )


def test_legacy_assessment_bridge_refuses_human_claim(audit_db):
    with pytest.raises(BadRequestError, match="cannot assert a human actor"):
        admissions._legacy_write_audit_event(
            audit_db, tenant_id=uuid4(), actor_person_id=uuid4(),
            action="applicant.transition", entity_type="applicant",
        )
    assert not audit_db.events


def test_fixed_facilitator_vocabulary_is_installed():
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    for action in ("facilitator_nomination.create", "facilitator_nomination.activate", "facilitator_nomination.revoke"):
        registry.require(action)
