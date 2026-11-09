"""Onboarding workflow — the checklist an accepted applicant works through.

Seeded when an applicant enters ``onboarding``; enrolment is gated on every
task being ``done``. Repo convention: ``db`` + explicit ids, ``flush`` not
``commit``, domain exceptions for the router. RLS scopes reads to the tenant.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admissions import Applicant
from app.models.onboarding import TASK_STATUSES, OnboardingTask
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.onboarding_policy import onboarding_policy


def _lock_applicant(db, *, tenant_id, applicant_id):
    row = db.scalars(
        select(Applicant)
        .where(Applicant.tenant_id == tenant_id, Applicant.id == applicant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise NotFoundError("Applicant not found.")
    return row


def _lock_tasks(db, *, tenant_id, applicant_id):
    return list(
        db.scalars(
            select(OnboardingTask)
            .where(OnboardingTask.tenant_id == tenant_id, OnboardingTask.applicant_id == applicant_id)
            .order_by(OnboardingTask.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def seed_tasks(db: Session, *, tenant_id: UUID, applicant_id: UUID) -> list[OnboardingTask]:
    """Create the default onboarding tasks for an applicant. Idempotent."""
    _lock_applicant(db, tenant_id=tenant_id, applicant_id=applicant_id)
    catalog = onboarding_policy(db).task_catalog
    existing = {t.key: t for t in _lock_tasks(db, tenant_id=tenant_id, applicant_id=applicant_id)}
    for order, (key, label) in enumerate(catalog):
        if key in existing:
            continue
        db.add(
            OnboardingTask(
                tenant_id=tenant_id,
                applicant_id=applicant_id,
                key=key,
                label=label,
                order_index=order,
                status="pending",
            )
        )
    db.flush()
    return list_tasks(db, tenant_id=tenant_id, applicant_id=applicant_id)


def list_tasks(db: Session, *, tenant_id: UUID, applicant_id: UUID) -> list[OnboardingTask]:
    return list(
        db.scalars(
            select(OnboardingTask)
            .where(OnboardingTask.tenant_id == tenant_id)
            .where(OnboardingTask.applicant_id == applicant_id)
            .order_by(OnboardingTask.order_index, OnboardingTask.created_at)
        ).all()
    )


def get_task(db: Session, *, task_id: UUID) -> OnboardingTask:
    task = db.get(OnboardingTask, task_id)
    if task is None:  # missing or hidden by RLS
        raise NotFoundError("Onboarding task not found.")
    return task


def set_task_status(db: Session, *, task_id: UUID, status: str, now: datetime | None = None) -> OnboardingTask:
    if status not in TASK_STATUSES:
        raise BadRequestError(f"Unknown task status: {status}")
    identified = get_task(db, task_id=task_id)
    tenant_id, applicant_id = identified.tenant_id, identified.applicant_id
    _lock_applicant(db, tenant_id=tenant_id, applicant_id=applicant_id)
    task = next(
        (row for row in _lock_tasks(db, tenant_id=tenant_id, applicant_id=applicant_id) if row.id == task_id), None
    )
    if task is None:
        raise NotFoundError("Onboarding task not found.")
    task.status = status
    task.completed_at = (now or datetime.now(UTC)) if status == "done" else None
    db.flush()
    return task


def complete_task_by_key(db: Session, *, tenant_id: UUID, applicant_id: UUID, key: str) -> OnboardingTask | None:
    """Mark a task done by key (used when a step self-completes, e.g. a passed
    entrance assessment). Returns the task, or None if the applicant has no such task."""
    _lock_applicant(db, tenant_id=tenant_id, applicant_id=applicant_id)
    task = next(
        (row for row in _lock_tasks(db, tenant_id=tenant_id, applicant_id=applicant_id) if row.key == key), None
    )
    if task is None:
        return None
    return set_task_status(db, task_id=task.id, status="done")


def is_complete(db: Session, *, tenant_id: UUID, applicant_id: UUID) -> bool:
    """True when the applicant has onboarding tasks and all are done."""
    tasks = list_tasks(db, tenant_id=tenant_id, applicant_id=applicant_id)
    return bool(tasks) and all(t.status == "done" for t in tasks)
