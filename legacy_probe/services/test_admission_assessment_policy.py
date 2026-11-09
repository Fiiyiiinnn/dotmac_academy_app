"""HM-16 shared admission assessment policy: pure matrix + single-source proof.

The admissions writer and the submission verifier must never re-derive the
decision independently; both execute/verify the one pure path pinned here.
Database goldens for the executed transitions live in tests/test_auto_accept.py.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import admission_assessment_policy as policy
from app.services.admission_assessment_policy import assessment_path, below_threshold_reason

ACCEPT = ("screened", "accepted", "onboarding")


@pytest.mark.parametrize(
    ("status", "score", "valid", "threshold", "below_action", "expected"),
    [
        ("applied", 0.8, True, 0.6, "waitlisted", ACCEPT),
        ("applied", 0.6, True, 0.6, "waitlisted", ACCEPT),  # boundary: >= accepts
        ("applied", 0.6, True, 0.6, "none", ACCEPT),
        ("applied", 0.4, True, 0.6, "waitlisted", ("waitlisted",)),
        ("applied", 0.4, True, 0.6, "none", ()),
        ("applied", 0.9, False, 0.6, "waitlisted", ()),  # invalid sitting: never auto-decide
        ("applied", 0.9, True, None, "waitlisted", ()),  # no threshold: human decides
        ("applied", None, True, 0.6, "waitlisted", ()),
        ("screened", 0.9, True, 0.6, "waitlisted", ()),
        ("onboarding", 0.9, True, 0.6, "waitlisted", ()),
        ("applied", 0.0, True, 0.0, "waitlisted", ACCEPT),
    ],
)
def test_assessment_path_matrix(status, score, valid, threshold, below_action, expected):
    assert (
        assessment_path(
            status=status, score=score, valid=valid, threshold=threshold, below_action=below_action
        )
        == expected
    )


def test_below_threshold_reason_reproduces_historical_note_text():
    assert below_threshold_reason(0.4, 0.6) == "auto-waitlisted: entrance score 40% below threshold 60%"
    assert below_threshold_reason(0.05, 0.5) == "auto-waitlisted: entrance score 5% below threshold 50%"


def test_both_consumers_bind_the_one_shared_function():
    from app import admission_assessment_context
    from app.services import admissions

    assert admissions.assessment_path is policy.assessment_path
    assert admission_assessment_context.assessment_path is policy.assessment_path
    assert admissions.below_threshold_reason is policy.below_threshold_reason
    assert admission_assessment_context.below_threshold_reason is policy.below_threshold_reason


@pytest.mark.parametrize(
    ("score", "valid", "threshold", "below_action"),
    [
        (0.8, True, 0.6, "waitlisted"),
        (0.4, True, 0.6, "waitlisted"),
        (0.4, True, 0.6, "none"),
        (0.9, False, 0.6, "waitlisted"),
        (0.9, True, None, "waitlisted"),
    ],
)
def test_admissions_executes_exactly_the_shared_path(monkeypatch, score, valid, threshold, below_action):
    from app.services import admissions

    cohort = SimpleNamespace(auto_accept_threshold=threshold, auto_accept_below_action=below_action)
    applicant = SimpleNamespace(
        id=uuid4(),
        status="applied",
        cohort_id=uuid4(),
        assessment_score=score,
        assessment_valid=valid,
        notes=None,
    )
    db = SimpleNamespace(get=lambda model, ident: cohort, flush=lambda: None)
    transitions = []
    monkeypatch.setattr(
        admissions,
        "_assessment_policy_transition",
        lambda session, **kw: transitions.append((kw["to_status"], kw.get("notes"))),
    )
    monkeypatch.setattr(admissions, "mint_onboarding_token", lambda session, *, applicant: "raw-token")

    raw = admissions.apply_assessment_policy(db, applicant=applicant)
    expected = assessment_path(
        status="applied", score=score, valid=valid, threshold=threshold, below_action=below_action
    )
    assert tuple(status for status, _ in transitions) == expected
    assert (raw == "raw-token") is (expected == ACCEPT)
    if expected == ("waitlisted",):
        assert transitions == [("waitlisted", below_threshold_reason(score, threshold))]
    if expected == ACCEPT:
        assert applicant.notes == f"auto-accepted: entrance score {score:.0%} >= threshold {threshold:.0%}"
