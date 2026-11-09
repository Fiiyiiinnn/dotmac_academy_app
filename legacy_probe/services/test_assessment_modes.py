"""Assessment-mode reveal policy (practice / graded / exam)."""

from __future__ import annotations

from types import SimpleNamespace

import dotmac_learning as learning

from app.services.assessment import reveal_feedback

# The product default reveal policy (Settings.learning_feedback_reveal_by_mode),
# passed explicitly because the facade no longer reads a hidden global.
_TERMS = learning.FeedbackRevealTerms(
    reveal_by_mode={"practice": "immediate", "graded": "on_pass_or_exhausted", "exam": "never"}
)


def _act(mode, max_attempts=None):
    return SimpleNamespace(assessment_mode=mode, max_attempts=max_attempts, min_attempt_interval_seconds=0)


def test_practice_always_reveals():
    assert reveal_feedback(_act("practice"), terms=_TERMS, passed=False, attempts_used=0) is True


def test_exam_never_reveals_even_when_passed():
    assert reveal_feedback(_act("exam", max_attempts=1), terms=_TERMS, passed=True, attempts_used=1) is False


def test_graded_reveals_on_pass():
    assert reveal_feedback(_act("graded", max_attempts=3), terms=_TERMS, passed=True, attempts_used=1) is True


def test_graded_withholds_until_resolved():
    assert reveal_feedback(_act("graded", max_attempts=3), terms=_TERMS, passed=False, attempts_used=1) is False


def test_graded_reveals_when_attempts_exhausted():
    assert reveal_feedback(_act("graded", max_attempts=3), terms=_TERMS, passed=False, attempts_used=3) is True


def test_graded_unlimited_withholds_until_pass():
    # No cap → "exhausted" never happens, so answers stay hidden until a pass.
    assert reveal_feedback(_act("graded", max_attempts=None), terms=_TERMS, passed=False, attempts_used=99) is False
