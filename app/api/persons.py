"""Tenant-scoped Person CRUD.

Demonstrates the canonical pattern: routes never read `tenant_id` from a payload or
URL; it always comes from `request.state.tenant`, set by `TenantResolverMiddleware`,
and enforced at the DB layer by RLS.

A request to `acme.app.com/people` lists ACME's people. A request to
`widgets.app.com/people` with the SAME ID will 404 even if the ID exists in ACME —
because RLS filters it out before the row reaches the application.
"""

# Ported at new-composition genesis (API routers wave).

from __future__ import annotations

from typing import cast
from uuid import UUID

from dotmac_kernel.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role, require_tenant
from app.services import api_identity_operations as operations
from app.services import api_identity_reads as reads
from app.services.identity import AcademyCurrentAccount

router = APIRouter(
    prefix="/people",
    tags=["people"],
    dependencies=[Depends(require_tenant)],
)


class PersonCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)


class PersonRead(BaseModel):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    model_config = {"from_attributes": True}


@router.post("", response_model=PersonRead, status_code=status.HTTP_201_CREATED)
def create_person(
    payload: PersonCreate,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> reads.ApiPersonView:
    try:
        return operations.create_person(
            db, request=request, email=str(payload.email), first_name=payload.first_name, last_name=payload.last_name
        )
    finally:
        payload = cast(PersonCreate, None)
        request = cast(Request, None)
        db = cast(Session, None)
        del tenant, actor


@router.get("", response_model=list[PersonRead])
def list_people(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> list[reads.ApiPersonView]:
    return reads.list_people(db, tenant_id=tenant.id)


@router.get("/{person_id}", response_model=PersonRead)
def get_person(
    person_id: UUID,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> reads.ApiPersonView:
    person = reads.get_person(db, tenant_id=tenant.id, person_id=person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def delete_person(
    person_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> None:
    try:
        operations.delete_person(db, request=request, person_id=person_id)
    finally:
        person_id = cast(UUID, None)
        request = cast(Request, None)
        db = cast(Session, None)
        del tenant, actor
