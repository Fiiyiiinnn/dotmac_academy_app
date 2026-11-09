"""Tenant-scoped RBAC and audit endpoints."""

# Ported at new-composition genesis (API routers wave).

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from dotmac_kernel.models import Tenant
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role, require_tenant
from app.services import api_identity_operations as operations
from app.services import api_identity_reads as reads
from app.services.canonical_identity_store import RoleFact
from app.services.identity import AcademyCurrentAccount

router = APIRouter(prefix="/rbac", tags=["rbac"], dependencies=[Depends(require_tenant)])


class RoleCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=120)


class RoleRead(BaseModel):
    id: UUID
    slug: str
    name: str
    model_config = {"from_attributes": True}


class RoleGrantRequest(BaseModel):
    person_id: UUID
    role_id: UUID


class AuditEventRead(BaseModel):
    id: UUID
    actor_person_id: UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    details: object
    created_at: datetime | None
    model_config = {"from_attributes": True}


@router.post(
    "/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_role(
    payload: RoleCreate,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> RoleFact:
    try:
        return operations.create_role(db, request=request, slug=payload.slug, name=payload.name)
    finally:
        payload = cast(RoleCreate, None)
        request = cast(Request, None)
        db = cast(Session, None)
        del tenant, actor


@router.post("/role-grants", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def grant_role(
    payload: RoleGrantRequest,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> None:
    try:
        operations.grant_role(db, request=request, person_id=payload.person_id, role_id=payload.role_id)
    finally:
        payload = cast(RoleGrantRequest, None)
        request = cast(Request, None)
        db = cast(Session, None)
        del tenant, actor


@router.get("/audit-events", response_model=list[AuditEventRead])
def list_audit_events(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> list[reads.ApiAuditView]:
    return reads.list_audit_events(db, tenant_id=tenant.id)
