"""Legacy public-schema -> SnapshotV6 transform builders (flip caller step 2).

Reads legacy Academy rows with the same representation casts the
Learning export uses (float8 as network-order hex, JSONB as opaque
``jsonb::text``), fills the post-legacy contract fields from the
module's own server defaults, synthesizes the lr_0010 bank-version
layer (one published `backfill` version per legacy bank, content hash
via Learning's canonical definition), and returns a
`LearningSnapshotV6` plus an exceptions list. Rows that cannot be
represented faithfully are reported, never auto-fitted.

Identity (kernel Party/credentials) and product-table copies are
separate caller steps; person references keep legacy UUIDs by design.
"""

from __future__ import annotations

import dataclasses
import json
import struct
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid5

from dotmac_learning.assessment_transfer_contracts import AttemptGrantRowV4
from dotmac_learning.bank_lifecycle import canonical_content_hash
from dotmac_learning.bank_transfer_contracts import (
    ActivityAttemptRowV6,
    ActivityRowV6,
    CertificateRowV6,
    CohortRowV6,
    CourseOfferingRowV6,
    CoursePrerequisiteRowV6,
    CourseRowV6,
    LearningSnapshotV6,
    QuestionBankRowV6,
    QuestionBankVersionRowV6,
    QuestionRowV6,
    TrackRowV6,
)
from dotmac_learning.curriculum_transfer_contracts import (
    ChapterBookmarkRowV2,
    ChapterNoteRowV2,
    ChapterReadRowV2,
    ChapterRowV2,
    SubtopicReadRowV2,
)
from dotmac_learning.delivery_transfer_contracts import (
    AnnouncementRowV5,
    ClassSessionRowV5,
    OfferingActivityRowV5,
    SuccessQueueEntryRowV5,
)
from dotmac_learning.participation_transfer_contracts import (
    CohortTrackRowV3,
    EnrollmentRowV3,
    TrackCourseRowV3,
)
from dotmac_learning.transfer_contracts import (
    CourseCompletionRowV1,
    Float8Bits,
    JsonbText,
    LearningEventRowV1,
    ScoreRowV1,
    SubmissionRowV1,
)
from sqlalchemy import text
from sqlalchemy.engine import Connection

_BANK_VERSION_NAMESPACE = UUID("7a1d9c2e-5b6f-4e2a-9c41-000000000601")

# snapshot field -> (legacy table, row contract)
_TABLES: dict[str, tuple[str, type]] = {
    "courses": ("courses", CourseRowV6),
    "activities": ("activities", ActivityRowV6),
    "submissions": ("submissions", SubmissionRowV1),
    "scores": ("scores", ScoreRowV1),
    "course_completions": ("course_completions", CourseCompletionRowV1),
    "certificates": ("certificates", CertificateRowV6),
    "learning_events": ("learning_events", LearningEventRowV1),
    "chapters": ("chapters", ChapterRowV2),
    "question_banks": ("question_banks", QuestionBankRowV6),
    "questions": ("questions", QuestionRowV6),
    "chapter_reads": ("chapter_reads", ChapterReadRowV2),
    "subtopic_reads": ("subtopic_reads", SubtopicReadRowV2),
    "chapter_bookmarks": ("chapter_bookmarks", ChapterBookmarkRowV2),
    "chapter_notes": ("chapter_notes", ChapterNoteRowV2),
    "cohorts": ("cohorts", CohortRowV6),
    "enrollments": ("enrollments", EnrollmentRowV3),
    "tracks": ("tracks", TrackRowV6),
    "track_courses": ("track_courses", TrackCourseRowV3),
    "cohort_tracks": ("cohort_tracks", CohortTrackRowV3),
    "course_offerings": ("course_offerings", CourseOfferingRowV6),
    "course_prerequisites": ("course_prerequisites", CoursePrerequisiteRowV6),
    "activity_attempts": ("activity_attempts", ActivityAttemptRowV6),
    "attempt_grants": ("attempt_grants", AttemptGrantRowV4),
    "offering_activities": ("offering_activities", OfferingActivityRowV5),
    "class_sessions": ("class_sessions", ClassSessionRowV5),
    "announcements": ("announcements", AnnouncementRowV5),
    "success_queue_entries": ("success_queue_entries", SuccessQueueEntryRowV5),
}

_FLOAT_ONE = Float8Bits(struct.pack(">d", 1.0).hex())


def _float_bits(value: float) -> Float8Bits:
    return Float8Bits(struct.pack(">d", value).hex())


def _wrap(contract: type, raw: dict[str, Any]) -> Any:
    values: dict[str, Any] = {}
    for f in dataclasses.fields(contract):
        v = raw[f.name]
        t = str(f.type)
        if v is not None and "Float8Bits" in t:
            v = Float8Bits(v)
        elif v is not None and "JsonbText" in t:
            v = JsonbText(v)
        values[f.name] = v
    return contract(**values)


def build_snapshot(
    conn: Connection, tenant_ids: tuple[UUID, ...]
) -> tuple[LearningSnapshotV6, list[tuple[str, str, str]]]:
    """Build SnapshotV6 from a legacy public-schema connection.

    Returns (snapshot, exceptions) where exceptions are
    (table, row_id, reason) triples for the manual queue. Contract
    validation inside the row dataclasses still applies at import.
    """
    exceptions: list[tuple[str, str, str]] = []
    raw: dict[str, list[dict[str, Any]]] = {}
    for field, (table, contract) in _TABLES.items():
        existing = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t"
                ),
                {"t": table},
            )
        }
        present = type(
            "_P",
            (),
            {"__dataclass_fields__": {f.name: f for f in dataclasses.fields(contract) if f.name in existing}},
        )
        fields = [f for f in dataclasses.fields(contract) if f.name in existing]
        selects = []
        for f in fields:
            t_ = str(f.type)
            if "Float8Bits" in t_:
                selects.append(
                    f"CASE WHEN {f.name} IS NULL THEN NULL "
                    f"ELSE pg_catalog.encode(pg_catalog.float8send({f.name}), 'hex') END AS {f.name}"
                )
            elif "JsonbText" in t_:
                selects.append(f"{f.name}::text AS {f.name}")
            else:
                selects.append(f.name)
        rows = conn.execute(
            text(f"SELECT {', '.join(selects)} FROM public.{table} WHERE tenant_id = ANY(:tenants)"),  # noqa: S608 - identifiers from compiled contracts; values bound
            {"tenants": list(tenant_ids)},
        ).mappings()
        raw[field] = [dict(r) for r in rows]
        del present

    # --- bank version synthesis (lr_0010 layer) ---
    versions: list[QuestionBankVersionRowV6] = []
    version_by_bank: dict[tuple[UUID, UUID], UUID] = {}
    questions_by_bank: dict[tuple[UUID, UUID], list[dict[str, Any]]] = {}
    for q in raw["questions"]:
        questions_by_bank.setdefault((q["tenant_id"], q["bank_id"]), []).append(q)
    for bank in raw["question_banks"]:
        key = (bank["tenant_id"], bank["id"])
        version_id = uuid5(_BANK_VERSION_NAMESPACE, str(bank["id"]))
        version_by_bank[key] = version_id
        payload = []
        for q in questions_by_bank.get(key, []):
            payload.append(
                {
                    "ext_id": q["ext_id"],
                    "stem": q["stem"],
                    "type": q["type"],
                    "options": json.loads(q["options"]) if isinstance(q["options"], str) else q["options"],
                    "correct": json.loads(q["correct"]) if isinstance(q["correct"], str) else q["correct"],
                    "rubric_category": q["rubric_category"],
                    "category": q.get("category"),
                    "explanation": q.get("explanation") or "",
                    "weight": q["weight"],
                }
            )
        versions.append(
            QuestionBankVersionRowV6(
                id=version_id,
                tenant_id=bank["tenant_id"],
                created_at=bank["created_at"],
                updated_at=bank["updated_at"],
                bank_id=bank["id"],
                version_no=bank.get("version") or 1,
                state="published",
                origin="backfill",
                content_hash=canonical_content_hash(payload),
                lint_violations=JsonbText("[]"),
                base_version_id=None,
                draft_revision=0,
                created_by_actor=None,
                published_by=None,
                published_at=bank["updated_at"],
                retired_by=None,
                retired_at=None,
                retire_reason=None,
                superseded_by_version_id=None,
            )
        )

    activity_bank = {(a["tenant_id"], a["id"]): a.get("bank_id") for a in raw["activities"]}
    course_validity = {(c["tenant_id"], c["id"]): None for c in raw["courses"]}

    def derive(field: str, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        if field == "courses":
            out.setdefault("prerequisite_composition", "all")
            out.setdefault("completion_fraction", _FLOAT_ONE.hex)
            out.setdefault("certificate_auto_issue", True)
            out.setdefault("certificate_validity_days", None)
        elif field == "activities":
            out.setdefault("scoring_policy", "best")
            out.setdefault("is_final", False)
            out.setdefault("min_attempt_interval_seconds", 0)
            out["bank_version_id"] = (
                version_by_bank.get((row["tenant_id"], row["bank_id"])) if row.get("bank_id") else None
            )
        elif field == "questions":
            out["bank_version_id"] = version_by_bank.get((row["tenant_id"], row["bank_id"]))
        elif field == "question_banks":
            out["current_version_id"] = version_by_bank.get((row["tenant_id"], row["id"]))
        elif field == "activity_attempts":
            bank_id = activity_bank.get((row["tenant_id"], row["activity_id"]))
            out["bank_version_id"] = version_by_bank.get((row["tenant_id"], bank_id)) if bank_id else None
        elif field == "certificates":
            validity = course_validity.get((row["tenant_id"], row["course_id"]))
            out["expires_at"] = row["issued_at"] + timedelta(days=validity) if validity else None
        elif field == "cohorts":
            out.setdefault("auto_accept_below_action", "waitlisted")
        elif field == "tracks":
            out.setdefault("gating_mode", "previous")
        elif field == "course_offerings":
            out.setdefault("pacing_default_open", True)
        elif field == "course_prerequisites":
            out.setdefault("condition_kind", "course_completed")
            out.setdefault("params", "{}")
            out.setdefault("group_key", "g0")
            out.setdefault("origin", "ui")
        return out

    built: dict[str, tuple[Any, ...]] = {}
    for field, (_table, contract) in _TABLES.items():
        rows_out = []
        for row in raw[field]:
            try:
                rows_out.append(_wrap(contract, derive(field, row)))
            except Exception as exc:  # contract refusal -> manual queue
                exceptions.append((field, str(row.get("id")), f"{type(exc).__name__}: {exc}"[:200]))
        built[field] = tuple(rows_out)

    snapshot = LearningSnapshotV6(
        version=6,
        tenant_ids=tenant_ids,
        question_bank_versions=tuple(versions),
        **built,
    )
    return snapshot, exceptions
