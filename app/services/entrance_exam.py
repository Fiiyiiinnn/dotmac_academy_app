"""Entrance assessment — grade an applicant's exam into a competency profile.

Applicants aren't Persons, so we grade with the pure grader (``grade_submission``)
rather than ``submit_activity``, and store the result — overall score, level
band, and a per-category profile — directly on the Applicant. That profile is
the reusable candidate-level signal admissions, placement, and talent selection
all run on.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import cast
from urllib.parse import quote
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning.participation_models import Cohort
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admissions import Applicant
from app.models.entrance_defaults import TenantEntranceDefaults
from app.services import exam_engine, settings_store
from app.services.entrance_invitation_policy import entrance_invitation_policy
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.grading import grade_submission
from app.services.security import hash_token

# Overall-fraction → level band. Ordered low→high.
#
# These floors are PROVISIONAL. They are a prediction of item difficulty, not a
# measurement of it. Once a cohort has sat the exam, re-derive them from the real
# distribution with `recompute-entrance-levels` (percentile banding), which is
# immune to a mis-rated item.
LEVELS: tuple[tuple[str, float], ...] = (("beginner", 0.0), ("intermediate", 0.4), ("advanced", 0.7))


def level_for(fraction: float, levels: tuple[tuple[str, float], ...] = LEVELS) -> str:
    """Map an overall fraction (0..1) to a level band."""
    band = levels[0][0]
    for name, floor in levels:
        if fraction >= floor:
            band = name
    return band


# --- validity gate ---------------------------------------------------------
#
# A sitting that fails these is NOT a weak candidate — it is an ABSENCE OF DATA.
# Scoring it as a genuine low result pollutes the admissions ranking and, worse,
# the talent pool, which is only worth querying later if the rows in it are real.
#
# With 4 options the guessing baseline is 25%; a score at or below a third is
# indistinguishable from clicking at random. And nobody engages with a 30-question
# assessment in under six minutes.

INVALID_NEAR_CHANCE = exam_engine.INVALID_NEAR_CHANCE
INVALID_TOO_FAST = exam_engine.INVALID_TOO_FAST

check_validity = exam_engine.check_validity


def options_for(applicant: Applicant, question) -> list[str]:
    """Question options in a per-applicant order.

    A fixed bank sat by every applicant on a rolling basis leaks — candidates talk,
    and "the answer is C" spreads. Shuffling per applicant breaks that.

    The order is DETERMINISTIC in (applicant, question), so it is stable across a
    page reload or a resumed sitting — otherwise autosaved answers would appear
    against the wrong options. Safe because answers are posted by option TEXT, not
    by index, so grading is order-independent.
    """
    return exam_engine.present_options(applicant, question)


def questions_per_category(db: Session) -> int:
    """How many questions to draw from each category; 0 means the whole bank."""
    from app.services import settings_store

    raw = settings_store.effective(db).get("entrance_questions_per_category", 0)
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except ValueError:
        return 0


def sample_for(applicant: Applicant, questions: list, per_category: int) -> list:
    """A stratified per-applicant subset of the bank, or all of it.

    Thin adapter over :func:`exam_engine.select`. Kept as a named function
    because the entrance flow and its tests refer to it, but the decision now
    belongs to the engine so the course path cannot drift from it.
    """
    return exam_engine.select(applicant, questions, exam_engine.SelectionPolicy(per_category=max(per_category, 0)))


def issue_token(db: Session, *, applicant: Applicant) -> str:
    """Mint a self-serve access token for the applicant's entrance exam.

    Returns the raw token (deliver once, e.g. by email); only its hash is stored.
    """
    raw = secrets.token_urlsafe(32)
    applicant.assessment_token_hash = hash_token(raw)
    db.flush()
    return raw


def applicant_for_token(
    db: Session,
    *,
    tenant_id: UUID,
    raw: str,
    lock: bool = False,
) -> Applicant | None:
    """Resolve the applicant holding this access token (tenant-scoped by RLS)."""
    if not raw:
        return None
    stmt = (
        select(Applicant)
        .where(Applicant.tenant_id == tenant_id)
        .where(Applicant.assessment_token_hash == hash_token(raw))
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def academy_default_bank_id(db: Session, *, tenant_id: UUID) -> UUID | None:
    """The academy-wide default bank, or None if none is configured.

    The last tier of the precedence chain, and the only reader of
    `tenant_entrance_defaults` outside this module's setter. No row means no
    default — a valid state, not an error.
    """
    row = db.get(TenantEntranceDefaults, tenant_id)
    return row.default_bank_id if row is not None else None


def academy_default_time_limit(db: Session, *, tenant_id: UUID) -> int | None:
    """The academy-wide default sitting limit in minutes (None = untimed)."""
    row = db.get(TenantEntranceDefaults, tenant_id)
    return row.default_time_limit_minutes if row is not None else None


def set_academy_defaults(
    db: Session,
    *,
    tenant_id: UUID,
    bank_id: UUID,
    time_limit_minutes: int | None = None,
) -> TenantEntranceDefaults:
    """Upsert the academy-wide defaults. The ONE writer of this table.

    Kept here rather than in the CLI so the row has an owner: the service that
    resolves a default is the service that sets it.
    """
    row = db.get(TenantEntranceDefaults, tenant_id)
    if row is None:
        row = TenantEntranceDefaults(tenant_id=tenant_id)
        db.add(row)
    row.default_bank_id = bank_id
    row.default_time_limit_minutes = time_limit_minutes
    db.flush()
    return row


def resolve_bank_id(db: Session, *, applicant: Applicant) -> UUID:
    """Per-applicant snapshot, then cohort override, then Academy default."""
    if applicant.assessment_bank_id is not None:
        return applicant.assessment_bank_id
    if applicant.cohort_id is not None:
        cohort = db.get(Cohort, applicant.cohort_id)
        if cohort is not None and cohort.entrance_bank_id is not None:
            return cohort.entrance_bank_id
    default_bank_id = academy_default_bank_id(db, tenant_id=applicant.tenant_id)
    if default_bank_id is not None:
        return default_bank_id
    raise BadRequestError("No entrance assessment is configured for this cohort or academy.")


def effective_bank_version_id(db: Session, *, tenant_id: UUID, applicant: Applicant, bank_id: UUID) -> UUID | None:
    """The version this sitting serves/grades from.

    The start-time pin wins. Without a pin (legacy in-flight sitting, or
    the pre-start instruction screen) the bank's current published
    version serves. A versioned bank with no current version (retired)
    refuses new sittings; a bank with no versions at all is legacy and
    serves its unversioned rows (None).
    """
    pin = applicant.assessment_bank_version_id
    if pin is not None:
        return pin
    fact = learning.get_bank_fact(db, tenant_id=tenant_id, bank_id=bank_id)
    if fact.current_version_id is not None:
        return fact.current_version_id
    if learning.bank_version_history(db, tenant_id=tenant_id, bank_id=bank_id):
        raise BadRequestError("No published entrance assessment is currently available.")
    return None


def serving_question_facts(
    db: Session, *, tenant_id: UUID, applicant: Applicant, bank_id: UUID | None = None
) -> tuple[list, UUID | None, str | None]:
    """Server-side question facts for sampling/grading plus version state.

    Key-bearing: results must never be serialized to the candidate.
    Display goes through :func:`entrance_sitting_view`.
    """
    bank_id = bank_id or resolve_bank_id(db, applicant=applicant)
    version_id = effective_bank_version_id(db, tenant_id=tenant_id, applicant=applicant, bank_id=bank_id)
    if version_id is not None:
        bundle = learning.bank_version_questions(db, tenant_id=tenant_id, version_id=version_id)
        return list(bundle.questions), version_id, bundle.version.state
    return list(learning.bank_questions(db, tenant_id=tenant_id, bank_id=bank_id)), None, None


def sampled_facts(db: Session, *, tenant_id: UUID, applicant: Applicant) -> tuple[list, UUID | None, str | None]:
    """The deterministic paper this applicant sits (facts, version, state)."""
    facts, version_id, state = serving_question_facts(db, tenant_id=tenant_id, applicant=applicant)
    return sample_for(applicant, facts, questions_per_category(db)), version_id, state


def entrance_sitting_view(db: Session, *, tenant_id: UUID, applicant: Applicant) -> list[dict]:
    """Candidate-facing questions: stems/types/options only, never keys.

    Published versions render through Learning ``sitting_questions``
    (its dataclass has no correct/explanation fields). A pinned version
    retired mid-sitting still finishes locally with the same stripping,
    because Learning refuses retired versions in that projection.
    """
    facts, version_id, state = sampled_facts(db, tenant_id=tenant_id, applicant=applicant)
    if version_id is not None and state == "published":
        projected = learning.sitting_questions(
            db,
            tenant_id=tenant_id,
            version_id=version_id,
            sitter_id=applicant.id,
            only_ext_ids=[f.ext_id for f in facts],
        )
        return [{"ext_id": q.ext_id, "stem": q.stem, "type": q.type, "options": list(q.options)} for q in projected]
    return [{"ext_id": q.ext_id, "stem": q.stem, "type": q.type, "options": options_for(applicant, q)} for q in facts]


def has_entrance_exam(db: Session, *, applicant: Applicant) -> bool:
    """Whether an entrance exam applies to this applicant (cohort or academy default)."""
    try:
        resolve_bank_id(db, applicant=applicant)
        return True
    except BadRequestError:
        return False


# A submit within this many seconds past the limit still counts as on-time
# (network / auto-submit latency).
GRACE_SECONDS = 15


def time_limit_minutes(db: Session, *, applicant: Applicant) -> int | None:
    """Per-sitting entrance limit in minutes: cohort override, else the
    academy-wide default (None = untimed)."""
    if applicant.cohort_id is not None:
        cohort = db.get(Cohort, applicant.cohort_id)
        if cohort is not None and cohort.entrance_time_limit_minutes is not None:
            return cohort.entrance_time_limit_minutes
    return academy_default_time_limit(db, tenant_id=applicant.tenant_id)


def start_exam(db: Session, *, applicant: Applicant, now: datetime | None = None) -> dict:
    """Server-stamp the explicit start, pin the bank version, return timing.

    The pin is written only here (sitting start, never grade time) so a
    publish mid-sitting cannot change the paper under the candidate.
    """
    now = now or datetime.now(UTC)
    if applicant.assessment_started_at is None:
        bank_id = resolve_bank_id(db, applicant=applicant)
        applicant.assessment_bank_version_id = effective_bank_version_id(
            db, tenant_id=applicant.tenant_id, applicant=applicant, bank_id=bank_id
        )
        applicant.assessment_started_at = now
        applicant.assessment_elapsed_seconds = 0
        db.flush()
    return timing_info(db, applicant=applicant, now=now)


def timing_info(db: Session, *, applicant: Applicant, now: datetime | None = None) -> dict:
    """Return server-derived elapsed/remaining time without trusting the browser."""
    now = now or datetime.now(UTC)
    limit = time_limit_minutes(db, applicant=applicant)
    elapsed = 0
    if applicant.assessment_started_at is not None:
        elapsed = max(0, int((now - applicant.assessment_started_at).total_seconds()))
    remaining: int | None = None
    if limit is not None:
        remaining = max(0, int(limit * 60 - elapsed))
    return {
        "limit_minutes": limit,
        "elapsed_seconds": elapsed,
        "remaining_seconds": remaining,
        "expired": (remaining == 0) if limit is not None else False,
    }


def record_elapsed(
    db: Session,
    *,
    applicant: Applicant,
    now: datetime | None = None,
) -> None:
    """Persist a server-derived elapsed snapshot for reporting.

    Client duration is deliberately not accepted. The explicit server start
    timestamp is the sole clock owner.
    """
    if applicant.assessment_taken_at is not None or applicant.assessment_started_at is None:
        return
    elapsed = max(0, int(((now or datetime.now(UTC)) - applicant.assessment_started_at).total_seconds()))
    applicant.assessment_elapsed_seconds = max(
        int(applicant.assessment_elapsed_seconds or 0),
        elapsed,
    )
    db.flush()


# --- deadline model --------------------------------------------------------
#
# The invitation link stays valid until a fixed date rather than pinning the
# candidate to a booked slot. On unreliable connectivity that matters: they pick
# their own good moment instead of missing a window they cannot control.


def past_deadline(applicant: Applicant, now: datetime | None = None) -> bool:
    if applicant.assessment_deadline is None:
        return False
    return (now or datetime.now(UTC)) > applicant.assessment_deadline


def invite(
    db: Session,
    *,
    applicant: Applicant,
    base_url: str,
    deadline_days: int | None = None,
    send: bool = True,
) -> dict:
    """Mint the exam link, set the deadline, and email the invitation.

    This is the step that was missing: the token used to be shown once on-screen
    and then lost. Returns {token, url, deadline, emailed} — the SAME token backs
    the emailed link and the on-screen one, so neither invalidates the other.
    """
    from app.services import applicant_email  # local: avoid an import cycle

    deadline = entrance_invitation_policy(db, deadline_days=deadline_days).deadline(datetime.now(UTC))
    raw = issue_token(db, applicant=applicant)
    applicant.assessment_deadline = deadline
    db.flush()

    url = f"{base_url.rstrip('/')}/apply/assessment?token={quote(raw)}"
    emailed = False
    if send:
        emailed = applicant_email.send_exam_invite(
            db,
            applicant=applicant,
            url=url,
            minutes=time_limit_minutes(db, applicant=applicant),
        )
        db.flush()
    return {
        "token": raw,
        "url": url,
        "deadline": applicant.assessment_deadline,
        "emailed": emailed,
    }


def save_answers(db: Session, *, applicant: Applicant, answers: dict) -> None:
    """Autosave in-progress answers so an interrupted sitting can resume.

    Called as the candidate answers, not just on submit. Without this, a dropped
    connection loses every answer while the clock keeps running — the applicant
    returns to "Time is up" with no score and (before ``reset_exam``) no way back.
    Ignored once the sitting is graded.
    """
    if applicant.assessment_taken_at is not None:
        return
    applicant.assessment_answers = {k: v for k, v in (answers or {}).items() if v}
    db.flush()


def reset_exam(db: Session, *, applicant: Applicant) -> str:
    """Give an applicant a fresh sitting, and return a new access token.

    The recovery path when a sitting is lost to a dropped connection, a device
    failure, or an expired clock the candidate never actually got to use. Clears
    the timer, the autosaved answers and any recorded result, and audits the reset.
    """
    applicant.assessment_started_at = None
    applicant.assessment_bank_version_id = None  # next start re-pins current
    applicant.assessment_elapsed_seconds = 0
    applicant.assessment_taken_at = None
    applicant.assessment_answers = None
    applicant.assessment_score = None
    applicant.assessment_level = None
    applicant.assessment_profile = None
    applicant.assessment_valid = None
    applicant.assessment_invalid_reason = None
    applicant.assessment_time_exceeded = False
    applicant.assessment_deadline = None
    applicant.assessment_erp_synced_at = None
    applicant.assessment_reset_count = (applicant.assessment_reset_count or 0) + 1
    return issue_token(db, applicant=applicant)


def reset_and_invite(
    db: Session,
    *,
    applicant: Applicant,
    base_url: str,
    deadline_days: int | None = None,
) -> bool:
    """Reset a sitting and atomically queue its replacement invitation."""
    from app.services import applicant_email

    deadline = entrance_invitation_policy(db, deadline_days=deadline_days).deadline(datetime.now(UTC))
    raw = reset_exam(db, applicant=applicant)
    applicant.assessment_deadline = deadline
    url = f"{base_url.rstrip('/')}/apply/assessment?token={quote(raw)}"
    queued = applicant_email.send_exam_invite(
        db,
        applicant=applicant,
        url=url,
        minutes=time_limit_minutes(db, applicant=applicant),
    )
    db.flush()
    return queued


def _time_exceeded(db: Session, applicant: Applicant, now: datetime) -> bool:
    limit = time_limit_minutes(db, applicant=applicant)
    if limit is None or applicant.assessment_started_at is None:
        return False
    elapsed = max(0, int((now - applicant.assessment_started_at).total_seconds()))
    return elapsed > limit * 60 + GRACE_SECONDS


def grade_and_record(
    db: Session,
    *,
    tenant_id: UUID,
    applicant: Applicant,
    answers: dict,
    bank_id: UUID | None = None,
    now: datetime | None = None,
) -> dict:
    """Grade ``answers`` into a competency profile and store it on the applicant.

    One sitting only — raises if the applicant has already taken it. Returns
    {score, level, profile}.
    """
    if applicant.assessment_taken_at is not None:
        raise BadRequestError("Entrance assessment already completed.")
    now = now or datetime.now(UTC)
    db.execute(select(Applicant.id).where(Applicant.id == applicant.id).with_for_update())
    if applicant.assessment_started_at is None:
        raise BadRequestError("Entrance assessment has not been started.")
    from app.services.learning_policy import validity_terms

    terms = validity_terms(db)
    missing_question_weight = cast(
        int, settings_store.effective(db, require_store=True)["learning_missing_question_weight"]
    )
    record_elapsed(db, applicant=applicant, now=now)
    bank_id = bank_id or resolve_bank_id(db, applicant=applicant)
    applicant.assessment_bank_id = bank_id

    # Grade the pinned version's paper (start-time pin; see start_exam).
    # The pin itself is never written here. Facts stay server-side; the
    # stored/returned result remains score/level/profile only.
    questions, _version_id, _state = serving_question_facts(
        db, tenant_id=tenant_id, applicant=applicant, bank_id=bank_id
    )
    if not questions:
        raise NotFoundError("Entrance assessment has no questions.")
    # Grade the subset this applicant was actually shown, not the whole bank —
    # sample_for is deterministic, so this reproduces the same paper.
    questions = sample_for(applicant, questions, questions_per_category(db))

    q_dicts: list[learning.GradingQuestion] = [
        {"ext_id": q.ext_id, "type": q.type, "correct": q.correct, "weight": q.weight, "options": q.options}
        for q in questions
    ]
    result = grade_submission(
        answers,
        q_dicts,
        0.0,
        missing_question_weight=missing_question_weight,
    )

    cat_of = {q.ext_id: (q.category or "general") for q in questions}
    weight_of = {q.ext_id: float(q.weight) for q in questions}
    agg: dict[str, list[float]] = {}  # category -> [correct_weight, total_weight]
    for item in result.per_item:
        cat = cat_of.get(item["id"], "general")
        w = weight_of[item["id"]]
        bucket = agg.setdefault(cat, [0.0, 0.0])
        bucket[1] += w
        if item["correct"]:
            bucket[0] += w
    profile = {cat: (round(correct / total, 4) if total else 0.0) for cat, (correct, total) in agg.items()}

    overall = round(result.fraction, 4)
    level = level_for(overall)

    # Validity gate: is this a real measurement, or an absence of data?
    duration = float(applicant.assessment_elapsed_seconds)
    valid, reason = check_validity(overall, duration, terms=terms, questions=questions)

    applicant.assessment_score = overall
    applicant.assessment_level = level
    applicant.assessment_profile = profile
    applicant.assessment_valid = valid
    applicant.assessment_invalid_reason = reason
    applicant.assessment_time_exceeded = _time_exceeded(db, applicant, now)
    applicant.assessment_taken_at = now
    applicant.assessment_result_version = (applicant.assessment_result_version or 0) + 1
    applicant.assessment_erp_synced_at = None
    applicant.assessment_answers = None  # graded; drop the autosave scratchpad
    db.flush()
    return {
        "score": overall,
        "level": level,
        "profile": profile,
        "valid": valid,
        "invalid_reason": reason,
    }
