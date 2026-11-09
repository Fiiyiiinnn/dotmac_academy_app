"""Audit event helpers (kernel-owned canonical audit).

Ported at new-composition genesis: writes delegate to the kernel audit
owner exactly as the legacy app did; reads go through the ported
`api_identity_reads` view.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.audit import write_audit_event as write_canonical_audit_event
from sqlalchemy.orm import Session

from app.services.api_identity_reads import WebAuditView, list_web_audit_events


def write_audit_event(
    db: Session,
    *,
    tenant_id: UUID,
    actor_type: str,
    actor_id: str | None,
    actor_party_id: UUID | None,
    source_application: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    details: dict[str, object] | None = None,
):
    """Write explicit forensic attribution through the canonical Kernel owner."""
    return write_canonical_audit_event(
        db,
        tenant_id=tenant_id,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_party_id=actor_party_id,
        source_application=source_application,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
    )


def list_events(
    db: Session,
    *,
    tenant_id: UUID,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    actor_person_id: UUID | None = None,
) -> list[WebAuditView]:
    """Read canonical audit facts; actor_person_id remains a legacy input name."""
    return list_web_audit_events(
        db,
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
        action=action,
        actor_party_id=actor_person_id,
    )
