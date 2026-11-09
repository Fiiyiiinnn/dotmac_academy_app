"""Shared entrance-assessment pipeline policy (HM-16).

One pure decision consumed by both the admissions writer and the
submission verifier, so the two can never drift apart. The
requires-valid gate stays in code here: auto-decisions on an invalid
sitting are forbidden and are never tenant data.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations


def assessment_path(
    *,
    status: str,
    score: float | None,
    valid: bool,
    threshold: float | None,
    below_action: str,
) -> tuple[str, ...]:
    """Return the applicant status path for a graded entrance sitting."""
    if status != "applied" or score is None or valid is not True or threshold is None:
        return ()
    if score >= threshold:
        return ("screened", "accepted", "onboarding")
    if below_action == "waitlisted":
        return ("waitlisted",)
    return ()


def below_threshold_reason(score: float, threshold: float) -> str:
    """Reproduce the historical auto-waitlist note text exactly."""
    return f"auto-waitlisted: entrance score {score:.0%} below threshold {threshold:.0%}"
