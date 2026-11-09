"""Fixed reminder-resend command over genuine request authority.

Party-first canonical locks precede profile/session/grants and the product log.
Every blocking stage is followed by fresh authority/policy reads. The public
Kernel savepoint owns command rollback, while the caller owns root commit.
"""

# Ported at new-composition genesis (reminders wave).
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from uuid import UUID

from dotmac_delivery_resend import (
    ReminderResendError,
    ResendFence,
    WritePhase,
    authority_preflight,
    caller_configuration,
    configure_default_fence,
    enter_resend,
    finish_resend,
    prepare_resend,
    recover_previous,
    values,
)
from dotmac_kernel import identity_storage as storage
from dotmac_kernel.audit import AuditEvent, write_audit_event
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, Role
from dotmac_kernel.settings_models import DomainSetting as PlatformSetting
from dotmac_kernel.transactions import conflict_savepoint
from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.reminder import STATUS_SENT, ReminderLog
from app.reminder_declarations import REMINDER_RESEND_ACTION
from app.services.api_identity_reads import certificate_recipient
from app.services.email_outbox import enqueue_email
from app.services.product_admin_access import require_reminders_admin_access
from app.services.security import hash_token
from app.services.settings_store import effective
from app.services.web_auth import COOKIE, require_web_user

REMINDER_RESEND_FENCE = ResendFence(
    consequences=(ReminderLog, EmailOutbox, AuditEvent),
    authority_models=(
        AuthSession,
        Party,
        PartyPerson,
        AcademyAccountProfile,
        Role,
        PartyRoleGrant,
        PlatformSetting,
    ),
    phases=(
        WritePhase(name="enqueue", inserts=frozenset({EmailOutbox})),
        WritePhase(name="audit", inserts=frozenset({AuditEvent}), updates=frozenset({ReminderLog})),
    ),
)
configure_default_fence(REMINDER_RESEND_FENCE)

if TYPE_CHECKING:

    class _ReminderOutbox(TypedDict):
        tenant_id: UUID
        idempotency_key: str
        kind: str
        recipient: str
        subject: str
        html_body: str
        text_body: str
        payload: dict[str, object]

    class _ReminderAuditWrite(TypedDict):
        tenant_id: UUID
        actor_type: str
        actor_id: str
        actor_party_id: UUID
        actor_label: str | None
        source_application: str
        action: str
        entity_type: str
        entity_id: str
        occurred_at: datetime
        is_success: bool
        details: dict[str, object]

    class _ReminderAuditReceipt(_ReminderAuditWrite):
        created_at: NotRequired[datetime | None]


def _authority(db, request, tenant_id, actor_id):
    authority_preflight(db, REMINDER_RESEND_FENCE)
    if require_tenant(request).id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    account = require_web_user(request, db)
    if account.id != actor_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return require_reminders_admin_access(request, account, db)


def resend_reminder(db: Session, *, request: Request, tenant_id: UUID, log_id: UUID) -> ReminderLog:
    """Resend exactly one log, with one canonical audit and outbox consequence."""
    from app.services.reminders import _email_for

    # Require Request itself: a detached account, actor UUID or cookie string is
    # never an alternate authorization entrypoint.
    if not isinstance(request, Request) or require_tenant(request).id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    authority_preflight(db, REMINDER_RESEND_FENCE)
    caller_configuration(db)
    account = require_web_user(request, db)
    actor_id = account.id
    _authority(db, request, tenant_id, actor_id)
    unit = None
    previous = prepare_resend(db, REMINDER_RESEND_FENCE)
    try:
        with conflict_savepoint(db):
            unit = enter_resend(db, previous)
            observed = db.execute(
                select(ReminderLog.person_id).where(
                    ReminderLog.tenant_id == tenant_id,
                    ReminderLog.id == log_id,
                )
            ).first()
            if observed is None:
                raise ValueError("reminder not found")
            recipient_id = observed.person_id
            parties = storage.lock_parties(db, tenant_id=tenant_id, party_ids=(actor_id, recipient_id))
            if {row.id for row in parties} != {actor_id, recipient_id}:
                raise ValueError("person not found")
            _authority(db, request, tenant_id, actor_id)
            if storage.lock_person(db, tenant_id=tenant_id, party_id=actor_id) is None:
                raise HTTPException(status_code=403, detail="Forbidden")
            db.scalars(
                select(AcademyAccountProfile)
                .where(
                    AcademyAccountProfile.tenant_id == tenant_id,
                    AcademyAccountProfile.party_id == actor_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            db.scalars(
                select(AuthSession)
                .where(
                    AuthSession.tenant_id == tenant_id,
                    AuthSession.party_id == actor_id,
                    AuthSession.token_hash == hash_token(cast(str, request.cookies.get(COOKIE))),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            storage.lock_roles(db, tenant_id=tenant_id)
            storage.lock_grants(db, tenant_id=tenant_id, party_ids=(actor_id,))
            _authority(db, request, tenant_id, actor_id)
            log = db.scalars(
                select(ReminderLog)
                .where(
                    ReminderLog.tenant_id == tenant_id,
                    ReminderLog.id == log_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            account = _authority(db, request, tenant_id, actor_id)
            if log is None:
                raise ValueError("reminder not found")
            if log.person_id != recipient_id:
                raise ValueError("reminder recipient changed; retry")
            person = certificate_recipient(db, tenant_id=tenant_id, person_id=recipient_id)
            if person is None:
                raise ValueError("person not found")
            if not person.email:
                raise ValueError("person email is missing")
            before = values(log)
            prior_logs = {
                row.id: values(row)
                for row in db.scalars(
                    select(ReminderLog)
                    .where(
                        ReminderLog.tenant_id == tenant_id,
                    )
                    .execution_options(populate_existing=True)
                ).all()
            }
            transaction_time = db.scalar(select(func.now()))
            prior_outbox = {
                row.id: values(row)
                for row in db.scalars(
                    select(EmailOutbox)
                    .where(
                        EmailOutbox.tenant_id == tenant_id,
                    )
                    .execution_options(populate_existing=True)
                ).all()
            }
            prior_audits = {
                row.id: values(row)
                for row in db.scalars(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == tenant_id,
                    )
                    .execution_options(populate_existing=True)
                ).all()
            }
            unit.remember_rollback(
                tenant_id,
                {ReminderLog: prior_logs, EmailOutbox: prior_outbox, AuditEvent: prior_audits},
            )
            cfg = effective(db, require_store=True)
            branding = str(cfg.get("branding_name", "Dotmac Academy"))
            base_url = str(cfg.get("academy_base_url", "https://academy.dotmac.io")).rstrip("/")
            now = datetime.now(UTC)
            outbox_key = f"reminder_resend:{log.id}:{now.strftime('%Y%m%d%H%M%S')}"
            subject, html, text = _email_for(log.title, log.link, branding, base_url)
            expected_outbox: _ReminderOutbox = dict(
                tenant_id=tenant_id,
                idempotency_key=outbox_key,
                kind=f"reminder_{log.event_kind}",
                recipient=person.email,
                subject=subject,
                html_body=html,
                text_body=text,
                payload={},
            )
            unit.phase = "enqueue"
            queued = enqueue_email(db, **expected_outbox)
            unit.require()
            if type(queued) is not bool:
                raise ReminderResendError("resend enqueue receipt differs")
            expected_log = deepcopy(before)
            expected_log.update(status=STATUS_SENT, outbox_key=outbox_key, sent_at=now)
            unit.phase = "audit"
            log.status, log.outbox_key, log.sent_at = STATUS_SENT, outbox_key, now
            if "updated_at" in expected_log:
                expected_log["updated_at"] = log.updated_at = now
            _authority(db, request, tenant_id, actor_id)
            expected_audit: _ReminderAuditReceipt = dict(
                tenant_id=tenant_id,
                actor_type="user",
                actor_id=str(actor_id),
                actor_party_id=actor_id,
                actor_label=account.email if len(account.email) <= 160 else None,
                source_application="dotmac_academy_app",
                action=REMINDER_RESEND_ACTION,
                entity_type="reminder_log",
                entity_id=str(log.id),
                occurred_at=now,
                is_success=True,
                details={"event_kind": log.event_kind},
            )
            audit = write_audit_event(db, **cast("_ReminderAuditWrite", expected_audit))
            unit.require()
            db.flush()
            unit.require()
            _authority(db, request, tenant_id, actor_id)
            outboxes = db.scalars(
                select(EmailOutbox)
                .where(
                    EmailOutbox.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
            ).all()
            matching = [row for row in outboxes if row.idempotency_key == outbox_key]
            if len(matching) != 1:
                raise ReminderResendError("resend outbox consequence is missing")
            delivery = matching[0]
            if (
                any(getattr(delivery, key) != value for key, value in expected_outbox.items())
                or queued != (delivery.id not in prior_outbox)
                or {row.id for row in outboxes} != set(prior_outbox) | {delivery.id}
                or any(values(row) != prior_outbox[row.id] for row in outboxes if row.id in prior_outbox)
            ):
                raise ReminderResendError("resend outbox consequence differs")
            if queued:
                defaults = dict(
                    status="pending",
                    attempts=0,
                    available_at=transaction_time,
                    sent_at=None,
                    last_error=None,
                    created_at=transaction_time,
                    updated_at=transaction_time,
                )
                if any(getattr(delivery, key) != value for key, value in defaults.items()):
                    raise ReminderResendError("new resend outbox defaults differ")
            expected_audit["created_at"] = transaction_time
            logs = db.scalars(
                select(ReminderLog)
                .where(
                    ReminderLog.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
            ).all()
            if {row.id for row in logs} != set(prior_logs) or any(
                values(row) != prior_logs[row.id] for row in logs if row.id != log_id
            ):
                raise ReminderResendError("resend changed an unrelated reminder")
            audits = db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                )
                .execution_options(populate_existing=True)
            ).all()
            if (
                type(audit) is not AuditEvent
                or audit.id in prior_audits
                or {row.id for row in audits} != set(prior_audits) | {audit.id}
                or any(values(row) != prior_audits[row.id] for row in audits if row.id in prior_audits)
                or any(getattr(audit, key) != value for key, value in expected_audit.items())
            ):
                raise ReminderResendError("resend audit consequence differs")
            # Include omitted optional audit fields in the fixed receipt.
            for key in ("request_id", "status_code", "ip_address", "user_agent"):
                if getattr(audit, key) is not None:
                    raise ReminderResendError("resend audit attribution differs")
            unit.pin(
                ((log, expected_log), (delivery, values(delivery)), (audit, values(audit))),
                lambda: _authority(db, request, tenant_id, actor_id),
            )
            unit.validate()
            unit.executing = False
        finish_resend(unit)
        return log
    except BaseException:
        if unit is None:
            unit = previous
        if unit is not None:
            try:
                recover_previous(unit)
            except BaseException:
                unit.failed = unit.corrupt = True
        raise
