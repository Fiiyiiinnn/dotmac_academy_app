"""Compatibility names for the public, canonical Learning exam engine."""

# Ported at new-composition genesis (admissions services slice).

from dotmac_learning import (
    INVALID_NEAR_CHANCE,
    INVALID_TOO_FAST,
    SelectionPolicy,
    ValidityTerms,
    chance_baseline,
    check_validity,
    duration_floor,
    near_chance_floor,
    present_options,
)
from dotmac_learning import ExamQuestion as Question
from dotmac_learning import ExamSitter as Sitter
from dotmac_learning import select_questions as select

__all__ = [
    "INVALID_NEAR_CHANCE",
    "INVALID_TOO_FAST",
    "Question",
    "SelectionPolicy",
    "Sitter",
    "ValidityTerms",
    "chance_baseline",
    "check_validity",
    "duration_floor",
    "near_chance_floor",
    "present_options",
    "select",
]
