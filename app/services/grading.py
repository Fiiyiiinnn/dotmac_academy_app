"""Compatibility names for Learning's pure grading function and result fields."""

# Ported at new-composition genesis (admissions services slice).

from dotmac_learning import GradedResult as GradeResult
from dotmac_learning import grade_submission

__all__ = ["GradeResult", "grade_submission"]
