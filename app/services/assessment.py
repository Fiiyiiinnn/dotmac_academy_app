# Ported at new-composition genesis (presentation-coupled wave).
# app/services/assessment.py
"""Academy assessment adapters over the public Learning owners."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import cast
from uuid import UUID

from dotmac_learning import (
    ActivityFact,
    AdmissionContention,
    AdmissionRefused,
    AssessmentQuestion,
    FeedbackRevealTerms,
    OverrideScoreCommand,
    ResultBadRequest,
    ScoreFact,
    SubmissionFact,
    SubmitMCQCommand,
    questions_for,
    submit_mcq_activity,
)
from dotmac_learning import (
    attempts_used as learning_attempts_used,
)
from dotmac_learning import (
    attempts_used_by_person as learning_attempts_used_by_person,
)
from dotmac_learning import (
    best_scores_by_course_for_person as learning_best_scores_by_course_for_person,
)
from dotmac_learning import (
    best_scores_for as learning_best_scores_for,
)
from dotmac_learning import (
    best_scores_for_person as learning_best_scores_for_person,
)
from dotmac_learning import (
    override_score as learning_override_score,
)
from dotmac_learning import (
    pending_grading as learning_pending_grading,
)
from dotmac_learning import (
    reveal_feedback as learning_reveal_feedback,
)
from pydantic import JsonValue
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.exceptions import BadRequestError, ConflictError

logger = logging.getLogger(__name__)


def _questions_for(
    db: Session,
    tenant_id: UUID,
    bank_id: UUID,
    only_ext_ids: list[str] | None = None,
) -> list[AssessmentQuestion]:
    return questions_for(db, tenant_id=tenant_id, bank_id=bank_id, only_ext_ids=only_ext_ids)


def submit_activity(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity: ActivityFact,
    answers: dict[str, JsonValue],
    only_ext_ids: list[str] | None = None,
) -> ScoreFact | None:
    """Delegate ordered admission, question selection, grading and persistence.

    Learning owns attempt policy, submission/score writes and completion with
    certificate issuance in this same caller transaction. Manual submissions
    await instructor grading and return no score. Academy delivers the returned
    completion effects once and retains optional first-pass notifications.
    """
    if activity.type != "mcq_test":
        raise BadRequestError(
            f"{activity.type!r} activities are not graded here; use the lab workspace to submit this activity."
        )
    try:
        result = submit_mcq_activity(
            db,
            SubmitMCQCommand(
                tenant_id=tenant_id,
                person_id=person_id,
                activity_id=activity.id,
                answers=answers,
                only_ext_ids=only_ext_ids,
                missing_question_weight=cast(
                    int, settings_store.effective(db, require_store=True)["learning_missing_question_weight"]
                ),
            ),
        )
    except AdmissionContention:
        # The caller must roll back; contention is not an ordinary refusal.
        raise
    except AdmissionRefused as exc:
        raise ConflictError(str(exc)) from exc
    except ResultBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    if result.completion is not None:
        from app.services.completion import deliver_completion_notifications

        deliver_completion_notifications(db, result.completion)
    score = result.score
    if score is None:
        return None
    # Auto-on-pass notification: best effort, must never break grading.
    try:
        from app.services.completion import _notification_recipient
        from app.services.email import notify_score_if_first_pass

        with db.begin_nested():
            person = _notification_recipient(db, tenant_id=tenant_id, person_id=person_id)
            notify_score_if_first_pass(db, score=score, activity=activity, person=person)
    except Exception as exc:
        logger.warning("auto-on-pass notification failed: %s", exc)
    return score


def pending_grading(
    db: Session,
    *,
    tenant_id: UUID,
) -> list[tuple[SubmissionFact, ActivityFact, str | None]]:
    """Detached (submission, activity, person_email) tuples, oldest first."""
    return [
        (fact.submission, fact.activity, fact.person_email)
        for fact in learning_pending_grading(db, tenant_id=tenant_id)
    ]


def attempts_used(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity_id: UUID,
) -> int:
    return learning_attempts_used(db, tenant_id=tenant_id, person_id=person_id, activity_id=activity_id)


def reveal_feedback(
    activity: ActivityFact,
    *,
    terms: FeedbackRevealTerms,
    passed: bool,
    attempts_used: int,
    attempts_granted: int = 0,
) -> bool:
    return learning_reveal_feedback(
        activity,
        terms=terms,
        passed=passed,
        attempts_used=attempts_used,
        attempts_granted=attempts_granted,
    )


def best_scores_for(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
) -> dict[UUID, ScoreFact]:
    return learning_best_scores_for(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)


def best_scores_for_person(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_ids: Sequence[UUID] | None = None,
) -> dict[UUID, ScoreFact]:
    return learning_best_scores_for_person(db, tenant_id=tenant_id, person_id=person_id, course_ids=course_ids)


def best_scores_by_course_for_person(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_ids: Sequence[UUID] | None = None,
) -> dict[UUID, dict[UUID, ScoreFact]]:
    return learning_best_scores_by_course_for_person(
        db, tenant_id=tenant_id, person_id=person_id, course_ids=course_ids
    )


def attempts_used_by_person(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
) -> dict[UUID, int]:
    return learning_attempts_used_by_person(db, tenant_id=tenant_id, person_id=person_id)


def override_score(
    db: Session,
    *,
    tenant_id: UUID,
    submission_id: UUID,
    score_value: float,
    max_score: float,
    reason: str | None,
) -> ScoreFact:
    """Delegate legacy override arithmetic and deliver returned completion once."""
    try:
        result = learning_override_score(
            db,
            OverrideScoreCommand(
                tenant_id=tenant_id,
                submission_id=submission_id,
                score_value=score_value,
                max_score=max_score,
                reason=reason,
            ),
        )
    except ResultBadRequest as exc:
        if str(exc) == "submission not found for tenant":
            raise ValueError(str(exc)) from exc
        raise BadRequestError(str(exc)) from exc
    if result.completion is not None:
        from app.services.completion import deliver_completion_notifications

        deliver_completion_notifications(db, result.completion)
    if result.score is None:
        raise ValueError("override result requires a score")
    return result.score
