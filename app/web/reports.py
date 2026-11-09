"""Academy report presentation over public Learning facts and live access policy."""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import csv
import io
from uuid import UUID, uuid4

from dotmac_kernel.models import Party, PartyType
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import csv_reports, insights
from app.services.email import render_cohort_html, render_transcript_html
from app.services.email_outbox import enqueue_email
from app.services.facilitator_access import FacilitatorAccess, allows_person, facilitator_access
from app.services.identity import AcademyCurrentAccount
from app.services.learning_policy import cohort_insight_terms
from app.services.lookups import cohort_or_404
from app.services.reports import cohort_matrix, student_transcript
from app.services.reports_access import require_reports_access
from app.web.templating import templates

router = APIRouter(prefix="/instructor", dependencies=[Depends(require_tenant)])


def _require_cohort_scope(db: Session, *, tenant_id: UUID, person_id: UUID, cohort_id: UUID) -> FacilitatorAccess:
    """404 a cohort outside the tenant before any scope decision, then scope."""
    cohort_or_404(db, tenant_id=tenant_id, cohort_id=cohort_id)
    access = facilitator_access(db, tenant_id=tenant_id, person_id=person_id)
    if not access.allows_cohort(cohort_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    return access


def _require_person_scope(db: Session, *, tenant_id: UUID, person_id: UUID, target_person_id: UUID) -> None:
    """404 a person outside the tenant before any scope decision, then scope."""
    exists = db.scalar(
        select(Party.id)
        .where(Party.tenant_id == tenant_id)
        .where(Party.id == target_person_id)
        .where(Party.party_type == PartyType.person)
        .limit(1)
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Person not found")
    access = facilitator_access(db, tenant_id=tenant_id, person_id=person_id)
    if not allows_person(db, tenant_id=tenant_id, access=access, target_person_id=target_person_id):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/reports", response_class=HTMLResponse)
def reports_index(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    access = facilitator_access(db, tenant_id=tenant.id, person_id=person.id)
    if not access.has_global_staff_access and not access.cohort_ids:
        raise HTTPException(status_code=403, detail="Forbidden")
    all_comparison = insights.cohorts_comparison(db, tenant_id=tenant.id)
    # Cross-cohort comparison is a global-staff surface; scoped facilitators
    # see only their own cohorts and no comparison table.
    comparison = all_comparison if access.has_global_staff_access else []
    cohorts = [
        row["cohort"]
        for row in all_comparison
        if access.has_global_staff_access or row["cohort"].id in access.cohort_ids
    ]
    return templates.TemplateResponse(
        request,
        "instructor/reports_index.html",
        {"request": request, "cohorts": cohorts, "comparison": comparison, "insight_terms": cohort_insight_terms(db)},
    )


@router.get("/reports/cohort/{cohort_id}/analytics", response_class=HTMLResponse)
def cohort_analytics(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    """Cohort insight projection (roadmap P3a): ledger + score derivations."""
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    cohort = cohort_matrix(db, tenant_id=tenant.id, cohort_id=cohort_id)["cohort"]
    ov = insights.cohort_overview(db, tenant_id=tenant.id, cohort_id=cohort_id)
    return templates.TemplateResponse(
        request,
        "instructor/cohort_analytics.html",
        {"request": request, "cohort": cohort, "ov": ov, "insight_terms": cohort_insight_terms(db)},
    )


# NOTE: the .csv route MUST be registered before /reports/cohort/{cohort_id}.
# FastAPI compiles {cohort_id} to a greedy [^/]+ matcher, so the plain route would
# otherwise capture "<uuid>.csv" and 422 on UUID validation.
@router.get("/reports/cohort/{cohort_id}.csv")
def reports_cohort_csv(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    matrix = cohort_matrix(db, tenant_id=tenant.id, cohort_id=cohort_id)
    activities = matrix["activities"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [csv_reports.sanitize_cell(c) for c in ["name", "email", *[a.title for a in activities], "completion_pct"]]
    )
    for row in matrix["rows"]:
        cells = []
        for a in activities:
            score = row["cells"].get(a.id)
            cells.append(f"{score.fraction:.2f}" if score is not None else "")
        writer.writerow(
            [
                csv_reports.sanitize_cell(c)
                for c in [row["name"], row["email"], *cells, f"{row['completion'] * 100:.0f}"]
            ]
        )

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=cohort-{cohort_id}.csv"},
    )


@router.get("/reports/cohort/{cohort_id}", response_class=HTMLResponse)
def reports_cohort(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    matrix = cohort_matrix(db, tenant_id=tenant.id, cohort_id=cohort_id)
    return templates.TemplateResponse(request, "instructor/reports_cohort.html", {"request": request, **matrix})


@router.post("/reports/student/{person_id}/email", response_class=HTMLResponse)
def reports_student_email(
    person_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    """Email the student their transcript; return an htmx flash partial."""
    tenant = require_tenant(request)
    _require_person_scope(db, tenant_id=tenant.id, person_id=person.id, target_person_id=person_id)
    transcript = student_transcript(db, tenant_id=tenant.id, person_id=person_id)
    student = transcript["person"]
    sent = enqueue_email(
        db,
        tenant_id=tenant.id,
        idempotency_key=f"transcript:{student.id}:{uuid4()}",
        kind="transcript",
        recipient=student.email,
        subject=f"Your Dotmac Academy transcript — {student.first_name} {student.last_name}".strip(),
        html_body=render_transcript_html(transcript),
    )
    return templates.TemplateResponse(
        request,
        "instructor/_email_result.html",
        {"request": request, "sent": sent, "to": student.email},
    )


@router.post("/reports/cohort/{cohort_id}/email", response_class=HTMLResponse)
def reports_cohort_email(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    """Email the requesting instructor the cohort summary; return a flash partial."""
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    matrix = cohort_matrix(db, tenant_id=tenant.id, cohort_id=cohort_id)
    sent = enqueue_email(
        db,
        tenant_id=tenant.id,
        idempotency_key=f"cohort-report:{cohort_id}:{person.id}:{uuid4()}",
        kind="cohort_report",
        recipient=person.email,
        subject=f"Cohort progress — {matrix['cohort'].name}",
        html_body=render_cohort_html(matrix),
    )
    return templates.TemplateResponse(
        request,
        "instructor/_email_result.html",
        {"request": request, "sent": sent, "to": person.email},
    )


@router.get("/reports/student/{person_id}", response_class=HTMLResponse)
def reports_student(
    person_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_person_scope(db, tenant_id=tenant.id, person_id=person.id, target_person_id=person_id)
    transcript = student_transcript(db, tenant_id=tenant.id, person_id=person_id)
    return templates.TemplateResponse(request, "instructor/reports_student.html", {"request": request, **transcript})


@router.get("/reports/cohort/{cohort_id}/roster.csv")
def reports_roster_csv(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    try:
        name, text = csv_reports.cohort_roster_csv(db, tenant_id=tenant.id, cohort_id=cohort_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    return Response(
        content=text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/reports/cohort/{cohort_id}/funnel.csv")
def reports_funnel_csv(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_cohort_scope(db, tenant_id=tenant.id, person_id=person.id, cohort_id=cohort_id)
    try:
        name, text = csv_reports.cohort_funnel_csv(db, tenant_id=tenant.id, cohort_id=cohort_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    return Response(
        content=text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/reports/cohort/{cohort_id}/queue.csv")
def reports_queue_csv(
    cohort_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    if not facilitator_access(db, tenant_id=tenant.id, person_id=person.id).has_global_staff_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    name, text = csv_reports.queue_summary_csv(db, tenant_id=tenant.id, cohort_id=cohort_id)
    return Response(
        content=text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/reports/academy-summary.csv")
def reports_academy_csv(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_reports_access),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    if not facilitator_access(db, tenant_id=tenant.id, person_id=person.id).has_global_staff_access:
        raise HTTPException(status_code=403, detail="Forbidden")
    name, text = csv_reports.academy_summary_csv(db, tenant_id=tenant.id)
    return Response(
        content=text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )
