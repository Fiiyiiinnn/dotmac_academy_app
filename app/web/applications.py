"""Admin applications page - stored applicant intake records."""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import csv
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.admissions import APPLICANT_STATUSES, Applicant
from app.models.email_outbox import EmailOutbox
from app.services import admissions as admissions_service
from app.services.api_identity_reads import newest_applicant_invitation_event
from app.services.csv_reports import sanitize_cell
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_role
from app.web.pagination import pagination_context
from app.web.templating import templates

router = APIRouter(
    prefix="/admin/applications",
    dependencies=[Depends(require_tenant), Depends(require_web_role("admin"))],
)


def _parse_optional_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field} date.") from exc


def _invitation_export_info(db: Session, applicant: Applicant) -> dict[str, str]:
    invite = db.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == applicant.tenant_id)
        .where(EmailOutbox.recipient == applicant.email)
        .where(EmailOutbox.kind.in_(("entrance_invite", "onboarding_invite", "enrollment_welcome")))
        .order_by(EmailOutbox.created_at.desc())
    ).first()
    event = newest_applicant_invitation_event(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)
    status = "not_sent"
    sent_date = ""
    method = ""
    if invite is not None:
        status = invite.status
        sent_date = (invite.sent_at or invite.created_at).isoformat()
        method = "email"
    source = ""
    if event is not None:
        source = str((event.details or {}).get("source") or event.action)
    elif applicant.source:
        source = applicant.source
    return {
        "Invitation Status": status,
        "Invitation Sent Date": sent_date,
        "Invitation Expiry": applicant.assessment_deadline.isoformat() if applicant.assessment_deadline else "",
        "Invitation Method": method,
        "How Invited": source,
    }


def _applications_csv(applicants: list[Applicant], db: Session, *, include_invitation: bool) -> str:
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    columns = [
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Program",
        "Status",
        "Applied On",
        "Source",
        "Assessment Score",
        "Assessment Taken At",
    ]
    invite_columns = [
        "Invitation Status",
        "Invitation Sent Date",
        "Invitation Expiry",
        "Invitation Method",
        "How Invited",
    ]
    writer.writerow([sanitize_cell(c) for c in columns + (invite_columns if include_invitation else [])])
    for applicant in applicants:
        row = [
            applicant.first_name,
            applicant.last_name,
            applicant.email,
            applicant.phone or "",
            applicant.program or "",
            applicant.status,
            applicant.applied_on.isoformat() if applicant.applied_on else "",
            applicant.source or "",
            "" if applicant.assessment_score is None else f"{applicant.assessment_score:.4f}",
            applicant.assessment_taken_at.isoformat() if applicant.assessment_taken_at else "",
        ]
        if include_invitation:
            info = _invitation_export_info(db, applicant)
            row.extend(info[col] for col in invite_columns)
        writer.writerow([sanitize_cell(c) for c in row])
    return buf.getvalue()


@router.get("", response_class=HTMLResponse)
def applications_page(
    request: Request,
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    applied_from: str | None = Query(default=None),
    applied_to: str | None = Query(default=None),
    rank: bool = Query(default=False),
    limit: int = Query(default=8, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    from_date = _parse_optional_date(applied_from, "from")
    to_date = _parse_optional_date(applied_to, "to")
    filter_error = None
    if from_date is not None and to_date is not None and from_date > to_date:
        filter_error = "Start date must be before or the same as end date."
        applicants = []
        total = 0
    else:
        total = admissions_service.count_applicants(
            db,
            status=status or None,
            search=q or None,
            applied_from=from_date,
            applied_to=to_date,
            rank_by_score=rank,
        )
        applicants = admissions_service.list_applicants(
            db,
            status=status or None,
            search=q or None,
            applied_from=from_date,
            applied_to=to_date,
            rank_by_score=rank,
            limit=limit,
            offset=offset,
        )
    application_metrics = {
        "total": admissions_service.count_applicants(db),
        "screened": admissions_service.count_applicants(db, status="screened"),
    }
    return templates.TemplateResponse(
        request,
        "admin/applications.html",
        {
            "request": request,
            "applicants": applicants,
            "filter_error": filter_error,
            "statuses": APPLICANT_STATUSES,
            "selected_status": status or "",
            "search_query": q or "",
            "applied_from": from_date.isoformat() if from_date else "",
            "applied_to": to_date.isoformat() if to_date else "",
            "rank": rank,
            "metrics": application_metrics,
            "pagination": pagination_context(total=total, limit=limit, offset=offset),
        },
    )


@router.get("/export.csv")
def applications_export(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    applied_from: str | None = Query(default=None),
    applied_to: str | None = Query(default=None),
    rank: bool = Query(default=False),
    include_invitation: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    from_date = _parse_optional_date(applied_from, "from")
    to_date = _parse_optional_date(applied_to, "to")
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=400, detail="Start date must be before or the same as end date.")
    applicants = admissions_service.list_applicants(
        db,
        status=status or None,
        search=q or None,
        applied_from=from_date,
        applied_to=to_date,
        rank_by_score=rank,
    )
    return Response(
        content=_applications_csv(applicants, db, include_invitation=include_invitation),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


@router.get("/{applicant_id:uuid}", response_class=HTMLResponse)
def application_detail(
    applicant_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    applicant = admissions_service.get_applicant(db, applicant_id=applicant_id)
    return templates.TemplateResponse(
        request,
        "admin/application_detail.html",
        {
            "request": request,
            "applicant": applicant,
            "actions": admissions_service.admin_review_actions(
                applicant,
                placement_ready=admissions_service.has_active_intake(
                    db,
                    applicant=applicant,
                ),
            ),
            "intake_choices": admissions_service.active_intake_choices(
                db,
                tenant_id=applicant.tenant_id,
            ),
            "history": admissions_service.applicant_transition_history(db, applicant=applicant),
        },
    )


@router.post("/{applicant_id:uuid}/intake")
def application_intake(
    applicant_id: UUID,
    request: Request,
    intake_choice: str = Form(...),
    reason: str = Form(""),
    actor: AcademyCurrentAccount = Depends(require_web_role("admin")),
    db: Session = Depends(get_db),
):
    try:
        cohort_raw, track_raw = intake_choice.split(":", 1)
        cohort_id, track_id = UUID(cohort_raw), UUID(track_raw)
    except (AttributeError, ValueError):
        raise HTTPException(status_code=400, detail="Choose an active cohort and track.") from None
    admissions_service.assign_applicant_intake(
        db,
        applicant_id=applicant_id,
        cohort_id=cohort_id,
        track_id=track_id,
        request=request,
        reason=reason.strip() or None,
    )
    return RedirectResponse(f"/admin/applications/{applicant_id}", status_code=303)


@router.post("/{applicant_id:uuid}/action")
def application_action(
    applicant_id: UUID,
    request: Request,
    action: str = Form(...),
    reason: str = Form(""),
    extend_access: bool = Form(False),
    actor: AcademyCurrentAccount = Depends(require_web_role("admin")),
    db: Session = Depends(get_db),
):
    if action == "resend_invitation" and extend_access:
        admissions_service.resend_applicant_invitation(
            db,
            applicant_id=applicant_id,
            request=request,
            extend_access=True,
            reason=reason.strip() or None,
        )
    else:
        admissions_service.apply_admin_review_action(
            db,
            applicant_id=applicant_id,
            action=action,
            request=request,
            reason=reason.strip() or None,
        )
    return RedirectResponse(f"/admin/applications/{applicant_id}", status_code=303)
