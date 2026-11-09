"""Academy bank adapters over Learning's versioned bank lifecycle.

YAML decoding/encoding stays product-owned (``bank_lint``). Learning owns
bank storage, lint execution and every lifecycle write in the caller's
Session; this adapter never commits or rolls back and never writes
Question/QuestionBank rows directly. The retired destructive writers
(Learning ``load_bank`` / ``import_bank_with_activity``) are fail-closed
shims upstream and are not called here.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning import BankDocumentHeader, BankFact
from dotmac_learning import decoded_doc_from_db as _decoded_doc_from_db
from pydantic import JsonValue
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.bank_lint import (  # noqa: F401 — re-exported for existing callers
    BankDoc,
    encode_bank_document,
    lint_bank,
    parse_bank,
)


class BankDocumentConversionError(ValueError):
    """A decoded YAML bank cannot form a Learning ``BankDocument``."""


def _json_scalar(value: object) -> JsonValue:
    """Normalize one decoded YAML scalar to a JSON value (dates → ISO text)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_scalar(v) for v in value]
    return str(value)


def bankdoc_to_document(doc: BankDoc, *, missing_question_weight: int) -> learning.BankDocument:
    """Convert the raw decoded bank into Learning's transport document.

    ``doc.version`` is carried only as transport metadata; Learning
    allocates version numbers at publish and ignores it for versioning.
    A question without a usable integer weight falls back to
    ``missing_question_weight`` only when the key is absent or null; a
    malformed weight refuses conversion naming the question id only.
    """
    if type(missing_question_weight) is not int or missing_question_weight <= 0:
        raise BankDocumentConversionError("missing_question_weight must be a positive integer")
    questions: list[learning.BankQuestion] = []
    for raw in doc.questions:
        if not isinstance(raw, dict):
            raise BankDocumentConversionError("bank question is not a mapping")
        ext_id = raw.get("id")
        label = ext_id if isinstance(ext_id, str) and ext_id else "<missing id>"
        for required in ("id", "stem", "type", "rubric_category"):
            value = raw.get(required)
            if not isinstance(value, str) or not value:
                raise BankDocumentConversionError(f"{label}: missing or invalid {required}")
        weight = raw.get("weight", None)
        if weight is None:
            weight = missing_question_weight
        elif isinstance(weight, bool) or not isinstance(weight, (int, float, str)):
            raise BankDocumentConversionError(f"{label}: invalid weight")
        else:
            try:
                as_float = float(weight)
            except (TypeError, ValueError):
                raise BankDocumentConversionError(f"{label}: invalid weight") from None
            if not as_float.is_integer() or as_float <= 0:
                raise BankDocumentConversionError(f"{label}: invalid weight")
            weight = int(as_float)
        questions.append(
            learning.BankQuestion(
                id=raw["id"],
                stem=raw["stem"],
                type=raw["type"],
                rubric_category=raw["rubric_category"],
                options=_json_scalar(raw.get("options") if raw.get("options") is not None else []),
                correct=_json_scalar(raw.get("correct") if raw.get("correct") is not None else []),
                category=_json_scalar(raw.get("category")),
                competency=_json_scalar(raw.get("competency")),
                explanation=str(raw.get("explanation") or ""),
                weight=weight,
            )
        )
    policy = {str(k): _json_scalar(v) for k, v in dict(doc.policy or {}).items()}
    return learning.BankDocument(
        course=doc.course,
        chapter=doc.chapter,
        kind=doc.kind,
        version=int(doc.version),
        questions=tuple(questions),
        policy=policy,
    )


def doc_from_db(db: Session, *, bank: BankDocumentHeader, course_slug: str) -> BankDoc:
    """Read the serving document for lint reporting (audit-banks)."""
    return _decoded_doc_from_db(db, bank=bank, course_slug=course_slug)


def import_bank_as_draft(
    db: Session,
    *,
    tenant_id: UUID,
    course_id: UUID,
    doc: BankDoc,
    audit_context: learning.BankAuditContext,
    replace_imported_draft: bool = False,
) -> learning.BankImportReport:
    """Land a decoded YAML bank as a Learning draft. Never publishes.

    Import never edits a published version, never repoints activities or
    entrance defaults, and never touches Activity rows; the YAML policy
    block returns in the report as unapplied proposals. A lint-failing
    bank still becomes a draft for repair. On ``BankImportRefused`` the
    refusal audit is staged in the caller's transaction and persists only
    if the caller commits — CLI/web callers must commit on refusal.
    """
    cfg = settings_store.effective(db, require_store=True)
    missing = cfg["learning_missing_question_weight"]
    document = bankdoc_to_document(doc, missing_question_weight=missing)  # type: ignore[arg-type]
    return learning.import_bank_document_as_draft(
        db,
        learning.ImportBankDocumentAsDraftCommand(
            tenant_id=tenant_id,
            course_id=course_id,
            doc=document,
            missing_question_weight=missing,  # type: ignore[arg-type]
            replace_imported_draft=replace_imported_draft,
        ),
        audit_context=audit_context,
    )


def load_bank(
    db: Session,
    *,
    tenant_id,
    course_id,
    doc: BankDoc,
    audit_context: learning.BankAuditContext | None = None,
) -> BankFact:
    """Compatibility shim: import-as-draft, returning the bank fact.

    The historical destructive replace is retired upstream. This now
    lands the document as a DRAFT (unpublished, invisible to sittings)
    and returns the bank header; publishing is an explicit admin action.
    Prefer :func:`import_bank_as_draft`, which surfaces the full report.
    """
    context = audit_context or learning.BankAuditContext(
        tenant_id=tenant_id,
        actor_type="system",
        source_application="academy_cli",
        actor_label="academy_cli",
    )
    report = import_bank_as_draft(db, tenant_id=tenant_id, course_id=course_id, doc=doc, audit_context=context)
    return learning.get_bank_fact(db, tenant_id=tenant_id, bank_id=report.bank_id)
