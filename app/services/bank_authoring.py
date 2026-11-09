"""Thin Academy composition over Learning's question-bank lifecycle.

Learning owns bank/version/question storage, lifecycle semantics, lint
execution, hashing, sitting key-stripping and audit writes; it performs
no role checks. This module is the product side of that boundary:
settings-driven role authorization, trusted ``BankAuditContext``
construction from the web session (the ``audit_context.py`` pattern),
YAML transport, bank-header discovery, and error-ready wrappers over the
public lifecycle commands. It contains no Question/QuestionBank writes;
the only direct reads are QuestionBank *headers* (id/course/chapter/
kind/current pointer) because Learning exports no bank-list read.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

import dotmac_learning as learning
import yaml
from dotmac_learning.curriculum_models import QuestionBank
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services import exam_engine
from app.services.bank_lint import encode_bank_document, parse_bank_text
from app.services.bank_loader import import_bank_as_draft
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.identity import AcademyCurrentAccount
from app.services.roles import role_slugs
from app.services.settings_store import effective


class BankPermissionDenied(PermissionError):
    """The session's roles do not allow this bank operation."""


SOURCE_APPLICATION = "dotmac_academy_app"
CLI_SOURCE_APPLICATION = "academy_cli"

ROLE_KEYS = (
    "bank_author_roles",
    "bank_publish_roles",
    "bank_repoint_roles",
    "bank_export_roles",
    "bank_retire_roles",
)


# --- authorization (Academy is the authority; Learning checks nothing) ---


def roles_allow(slugs: Iterable[str], allowed: Iterable[str]) -> bool:
    """True when any held slug is on the allowed list (empty denies)."""
    return bool(set(allowed).intersection(slugs))


def bank_role_lists(db: Session) -> dict[str, tuple[str, ...]]:
    """The five bank role lists from one strict settings snapshot."""
    cfg = effective(db, require_store=True)
    return {key: tuple(cfg[key]) for key in ROLE_KEYS}  # type: ignore[arg-type]


def require_bank_role(db: Session, *, tenant_id: UUID, person_id: UUID, key: str) -> None:
    """Refuse unless the person's roles intersect the named bank list."""
    if key not in ROLE_KEYS:
        raise ValueError(f"unknown bank role key {key!r}")
    cfg = effective(db, require_store=True)
    if not roles_allow(role_slugs(db, tenant_id, person_id), cfg[key]):  # type: ignore[arg-type]
        raise BankPermissionDenied("You do not have permission to manage question banks.")


# --- trusted audit attribution --------------------------------------------


def web_bank_audit_context(*, tenant_id: UUID, account: AcademyCurrentAccount) -> learning.BankAuditContext:
    """Build Learning attribution from the session account, never the form."""
    email = account.email or ""
    return learning.BankAuditContext(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=str(account.id),
        actor_label=email if len(email) <= 160 else None,
        actor_party_id=account.id,
        source_application=SOURCE_APPLICATION,
    )


def cli_bank_audit_context(*, tenant_id: UUID) -> learning.BankAuditContext:
    """System attribution for operator CLI import/export."""
    return learning.BankAuditContext(
        tenant_id=tenant_id,
        actor_type="system",
        source_application=CLI_SOURCE_APPLICATION,
        actor_label=CLI_SOURCE_APPLICATION,
    )


# --- bank-header discovery (reads only; no Learning list API exists) -----


@dataclass(frozen=True, slots=True)
class BankHeader:
    id: UUID
    course_id: UUID
    chapter_number: int | None
    kind: str
    version: int
    current_version_id: UUID | None


def list_course_banks(db: Session, *, tenant_id: UUID, course_id: UUID) -> list[BankHeader]:
    rows = db.scalars(
        select(QuestionBank)
        .where(QuestionBank.tenant_id == tenant_id)
        .where(QuestionBank.course_id == course_id)
        .order_by(QuestionBank.chapter_number.asc().nulls_last(), QuestionBank.kind)
    ).all()
    return [
        BankHeader(
            id=row.id,
            course_id=row.course_id,
            chapter_number=row.chapter_number,
            kind=row.kind,
            version=row.version,
            current_version_id=row.current_version_id,
        )
        for row in rows
    ]


def find_bank(
    db: Session, *, tenant_id: UUID, course_id: UUID, chapter_number: int | None, kind: str
) -> BankHeader | None:
    row = db.scalars(
        select(QuestionBank)
        .where(QuestionBank.tenant_id == tenant_id)
        .where(QuestionBank.course_id == course_id)
        .where(QuestionBank.chapter_number == chapter_number)
        .where(QuestionBank.kind == kind)
    ).first()
    if row is None:
        return None
    return BankHeader(
        id=row.id,
        course_id=row.course_id,
        chapter_number=row.chapter_number,
        kind=row.kind,
        version=row.version,
        current_version_id=row.current_version_id,
    )


def bank_or_404(db: Session, *, tenant_id: UUID, bank_id: UUID) -> BankHeader:
    row = db.scalars(
        select(QuestionBank).where(QuestionBank.tenant_id == tenant_id).where(QuestionBank.id == bank_id)
    ).first()
    if row is None:
        raise NotFoundError("Question bank not found.")
    return BankHeader(
        id=row.id,
        course_id=row.course_id,
        chapter_number=row.chapter_number,
        kind=row.kind,
        version=row.version,
        current_version_id=row.current_version_id,
    )


def current_version_id(db: Session, *, tenant_id: UUID, bank_id: UUID) -> UUID | None:
    """The bank's published serving version, via Learning's bank fact."""
    return learning.get_bank_fact(db, tenant_id=tenant_id, bank_id=bank_id).current_version_id


# --- lifecycle wrappers (caller owns the transaction) --------------------


def version_history(db: Session, *, tenant_id: UUID, bank_id: UUID):
    return learning.bank_version_history(db, tenant_id=tenant_id, bank_id=bank_id)


def draft_of(db: Session, *, tenant_id: UUID, bank_id: UUID):
    """The bank's single mutable draft fact, or None."""
    for fact in version_history(db, tenant_id=tenant_id, bank_id=bank_id):
        if fact.state == "draft":
            return fact
    return None


def create_draft(
    db: Session,
    *,
    tenant_id: UUID,
    course_id: UUID,
    chapter_number: int | None,
    kind: str,
    context: learning.BankAuditContext,
    from_empty: bool = False,
):
    return learning.create_bank_draft(
        db,
        learning.CreateBankDraftCommand(
            tenant_id=tenant_id,
            course_id=course_id,
            chapter_number=chapter_number,
            kind=kind,
            origin="ui",
            from_empty=from_empty,
        ),
        audit_context=context,
    )


def draft_question_from_lines(
    *,
    ext_id: str,
    stem: str,
    type_: str,
    rubric_category: str,
    options_text: str,
    correct_text: str,
    category: str,
    explanation: str,
    weight: int,
) -> learning.DraftQuestionWrite:
    """Decode the editor form (one option/answer per line) into a write."""
    options: list[JsonValue] = [line for line in (part.strip() for part in options_text.splitlines()) if line]
    correct: list[JsonValue] = [line for line in (part.strip() for part in correct_text.splitlines()) if line]
    if type(weight) is not int or weight <= 0:
        raise BadRequestError("Question weight must be a positive whole number.")
    return learning.DraftQuestionWrite(
        ext_id=ext_id.strip(),
        stem=stem,
        type=type_.strip(),
        rubric_category=rubric_category.strip(),
        options=options,
        correct=correct,
        category=category.strip() or None,
        explanation=explanation,
        weight=weight,
    )


def upsert_question(
    db: Session,
    *,
    tenant_id: UUID,
    version_id: UUID,
    question: learning.DraftQuestionWrite,
    context: learning.BankAuditContext,
):
    return learning.upsert_draft_question(
        db,
        learning.UpsertDraftQuestionCommand(tenant_id=tenant_id, version_id=version_id, question=question),
        audit_context=context,
    )


def delete_question(
    db: Session, *, tenant_id: UUID, version_id: UUID, ext_id: str, context: learning.BankAuditContext
) -> None:
    learning.delete_draft_question(
        db,
        learning.DeleteDraftQuestionCommand(tenant_id=tenant_id, version_id=version_id, ext_id=ext_id),
        audit_context=context,
    )


def discard_draft(db: Session, *, tenant_id: UUID, version_id: UUID, context: learning.BankAuditContext) -> None:
    learning.discard_draft(
        db,
        learning.DiscardDraftCommand(tenant_id=tenant_id, version_id=version_id),
        audit_context=context,
    )


def lint_draft(db: Session, *, tenant_id: UUID, version_id: UUID, course_slug: str = ""):
    return learning.lint_bank_version(db, tenant_id=tenant_id, version_id=version_id, course_slug=course_slug)


def publish(
    db: Session,
    *,
    tenant_id: UUID,
    version_id: UUID,
    context: learning.BankAuditContext,
    course_slug: str = "",
    expected_current_version_id: UUID | None = None,
):
    """Freeze a lint-clean draft. Never repoints activities (explicit)."""
    return learning.publish_bank_version(
        db,
        learning.PublishBankVersionCommand(
            tenant_id=tenant_id,
            version_id=version_id,
            expected_current_version_id=expected_current_version_id,
        ),
        audit_context=context,
        course_slug=course_slug,
    )


def retire(db: Session, *, tenant_id: UUID, version_id: UUID, reason: str, context: learning.BankAuditContext):
    if not (reason or "").strip():
        raise BadRequestError("Retiring a bank version requires a reason.")
    return learning.retire_bank_version(
        db,
        learning.RetireBankVersionCommand(tenant_id=tenant_id, version_id=version_id, reason=reason),
        audit_context=context,
    )


def repoint_activity(
    db: Session, *, tenant_id: UUID, activity_id: UUID, version_id: UUID, context: learning.BankAuditContext
):
    return learning.repoint_activity_bank_version(
        db,
        learning.RepointActivityBankVersionCommand(tenant_id=tenant_id, activity_id=activity_id, version_id=version_id),
        audit_context=context,
    )


def version_questions(db: Session, *, tenant_id: UUID, version_id: UUID):
    """Answer-key-bearing read; only author/admin surfaces may call it."""
    return learning.bank_version_questions(db, tenant_id=tenant_id, version_id=version_id)


def export_yaml(
    db: Session, *, tenant_id: UUID, version_id: UUID, course_slug: str, context: learning.BankAuditContext
) -> str:
    """Audited export rendered to YAML. Full keys; caller gates disclosure."""
    document = learning.export_bank_version(
        db, tenant_id=tenant_id, version_id=version_id, course_slug=course_slug, audit_context=context
    )
    facts = version_questions(db, tenant_id=tenant_id, version_id=version_id)
    return encode_bank_document(
        document,
        provenance={
            "bank_id": str(facts.version.bank_id),
            "version_id": str(facts.version.id),
            "version_no": facts.version.version_no if facts.version.version_no is not None else "draft",
            "content_hash": facts.version.content_hash,
        },
    )


BANK_UPLOAD_PARSE_ERROR = "Uploaded bank file could not be parsed as a question-bank YAML document."


def decode_bank_text(data: bytes) -> str:
    """Decode an uploaded bank file; malformed bytes never echo content."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise BadRequestError(BANK_UPLOAD_PARSE_ERROR) from None


def import_yaml_as_draft(
    db: Session,
    *,
    tenant_id: UUID,
    course_id: UUID,
    text: str,
    context: learning.BankAuditContext,
    replace_imported_draft: bool = False,
):
    """Decode uploaded YAML and land it as a draft (never publishes).

    Malformed uploads (bad YAML, non-mapping, missing keys, wrong
    scalar types) map to a content-free ``BadRequestError`` so the web
    route renders its designed error page instead of a 500 and no file
    content or key material reaches the error message.
    """
    try:
        doc = parse_bank_text(text)
    except (yaml.YAMLError, KeyError, TypeError, ValueError):
        raise BadRequestError(BANK_UPLOAD_PARSE_ERROR) from None
    return import_bank_as_draft(
        db,
        tenant_id=tenant_id,
        course_id=course_id,
        doc=doc,
        audit_context=context,
        replace_imported_draft=replace_imported_draft,
    )


# --- sitting projection for course activities ------------------------------


def sitting_dicts(questions: Sequence[learning.SittingQuestion]) -> list[dict]:
    """Template-ready sitting rows. The source type has no key fields."""
    return [{"ext_id": q.ext_id, "stem": q.stem, "type": q.type, "options": list(q.options)} for q in questions]


def resolve_activity_version_id(db: Session, *, tenant_id: UUID, activity) -> UUID | None:
    """Activity pin, else the bank's current published version, else None."""
    pinned = getattr(activity, "bank_version_id", None)
    if pinned is not None:
        return pinned
    bank_id = getattr(activity, "bank_id", None)
    if bank_id is None:
        return None
    return current_version_id(db, tenant_id=tenant_id, bank_id=bank_id)


def activity_sitting(
    db: Session,
    *,
    tenant_id: UUID,
    activity,
    sitter,
    only_ext_ids: Sequence[str] | None = None,
    pinned_version_id: UUID | None = None,
) -> tuple[UUID | None, list[dict]]:
    """Key-free questions for one activity sitting, options per sitter.

    Versioned banks serve through Learning ``sitting_questions`` (the
    dataclass cannot carry correct/explanation). A bank with no
    published version yet (pre-backfill legacy rows) falls back to
    Learning ``bank_questions`` with local key-stripping; that fallback
    retires once every bank has a published version.

    ``pinned_version_id`` is the open attempt's bank-version pin: when
    given it wins over activity resolution, mirroring the submit path's
    ``attempt_pin``, so a pooled page re-rendered after a publish or
    repoint serves exactly the paper Learning will grade. Activity
    resolution is used only to create a new draw. A retired attempt pin
    refusing inside Learning ``sitting_questions`` is the already-declared
    B2 behavior and is deliberately not worked around here.
    """
    version_id = (
        pinned_version_id
        if pinned_version_id is not None
        else resolve_activity_version_id(db, tenant_id=tenant_id, activity=activity)
    )
    if version_id is not None:
        questions = learning.sitting_questions(
            db,
            tenant_id=tenant_id,
            version_id=version_id,
            sitter_id=sitter.id,
            only_ext_ids=list(only_ext_ids) if only_ext_ids is not None else None,
        )
        return version_id, sitting_dicts(questions)
    facts = learning.bank_questions(db, tenant_id=tenant_id, bank_id=getattr(activity, "bank_id", None))
    if only_ext_ids is not None:
        order = {ext_id: i for i, ext_id in enumerate(only_ext_ids)}
        facts = tuple(sorted((f for f in facts if f.ext_id in order), key=lambda f: order[f.ext_id]))
    return None, [
        {
            "ext_id": f.ext_id,
            "stem": f.stem,
            "type": f.type,
            "options": exam_engine.present_options(sitter, f),
        }
        for f in facts
    ]


def activity_ext_ids(db: Session, *, tenant_id: UUID, activity, sitter_id: UUID) -> tuple[UUID | None, list[str]]:
    """Serving question ids for attempt draw, without key material."""
    version_id = resolve_activity_version_id(db, tenant_id=tenant_id, activity=activity)
    if version_id is not None:
        questions = learning.sitting_questions(db, tenant_id=tenant_id, version_id=version_id, sitter_id=sitter_id)
        return version_id, [q.ext_id for q in questions]
    facts = learning.bank_questions(db, tenant_id=tenant_id, bank_id=getattr(activity, "bank_id", None))
    return None, [f.ext_id for f in facts]
