"""Tests for Feature 8: numeric and short_text question types."""
from __future__ import annotations

from app.services.bank_loader import BankDoc, lint_bank
from app.services.grading import grade_submission

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUBRIC_MIX_10 = [
    {"id": f"m{i}", "stem": "s", "type": "single", "options": ["A", "B"],
     "correct": ["A"], "rubric_category": cat, "weight": 1, "explanation": ""}
    for i, cat in enumerate(
        ["recall", "recall",
         "application", "application", "application", "application", "application",
         "analysis", "analysis"]  # 9 items; tests add 1 "analysis" to make 10
    )
]


def _doc(extra_question: dict) -> BankDoc:
    return BankDoc(
        course="test", chapter=1, kind="chapter", version=1,
        questions=[*_RUBRIC_MIX_10, extra_question],
    )


# ---------------------------------------------------------------------------
# Numeric — grading
# ---------------------------------------------------------------------------

def test_numeric_exact_match():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 42, "options": {}, "weight": 1}]
    r = grade_submission({"n1": ["42"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True
    assert r.score == 1.0


def test_numeric_within_tolerance():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 10.0, "options": {"tolerance": 0.5}, "weight": 1}]
    r = grade_submission({"n1": ["10.3"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_numeric_outside_tolerance():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 10.0, "options": {"tolerance": 0.5}, "weight": 1}]
    r = grade_submission({"n1": ["11.0"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_numeric_zero_tolerance_is_default():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 5, "options": {}, "weight": 1}]
    r = grade_submission({"n1": ["5.1"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_numeric_blank_is_wrong():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 42, "options": {}, "weight": 1}]
    r = grade_submission({"n1": []}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_numeric_nonnumeric_is_wrong():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": 42, "options": {}, "weight": 1}]
    r = grade_submission({"n1": ["hello"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_numeric_correct_as_list():
    """correct stored as [42] (list form) should also work."""
    qs = [{"ext_id": "n1", "type": "numeric", "correct": [42], "options": {}, "weight": 1}]
    r = grade_submission({"n1": ["42"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


# ---------------------------------------------------------------------------
# short_text — grading
# ---------------------------------------------------------------------------

def test_short_text_exact_match():
    qs = [{"ext_id": "s1", "type": "short_text", "correct": ["Paris"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": ["Paris"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_short_text_case_insensitive():
    qs = [{"ext_id": "s1", "type": "short_text", "correct": ["Paris"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": ["paris"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_short_text_trim():
    qs = [{"ext_id": "s1", "type": "short_text", "correct": ["Paris"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": ["  Paris  "]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_short_text_nonmatch():
    qs = [{"ext_id": "s1", "type": "short_text", "correct": ["Paris"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": ["London"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_short_text_multiple_accepted():
    qs = [{"ext_id": "s1", "type": "short_text",
           "correct": ["TCP", "Transmission Control Protocol"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": ["transmission control protocol"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_short_text_blank_is_wrong():
    qs = [{"ext_id": "s1", "type": "short_text", "correct": ["Paris"], "options": {}, "weight": 1}]
    r = grade_submission({"s1": []}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_short_text_regex_match():
    qs = [{"ext_id": "s1", "type": "short_text",
           "correct": [r"\d{3}-\d{4}"], "options": {"regex": True}, "weight": 1}]
    r = grade_submission({"s1": ["555-1234"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is True


def test_short_text_regex_no_match():
    qs = [{"ext_id": "s1", "type": "short_text",
           "correct": [r"\d{3}-\d{4}"], "options": {"regex": True}, "weight": 1}]
    r = grade_submission({"s1": ["abc"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


def test_short_text_bad_regex_counts_as_wrong():
    qs = [{"ext_id": "s1", "type": "short_text",
           "correct": ["[invalid"], "options": {"regex": True}, "weight": 1}]
    r = grade_submission({"s1": ["anything"]}, qs, 0.5, missing_question_weight=1)
    assert r.per_item[0]["correct"] is False


# ---------------------------------------------------------------------------
# Mixed bank — end-to-end fraction check
# ---------------------------------------------------------------------------

def test_mixed_bank_all_correct():
    qs = [
        {"ext_id": "q1", "type": "single", "correct": ["A"], "options": ["A", "B"], "weight": 1},
        {"ext_id": "q2", "type": "numeric", "correct": 10, "options": {}, "weight": 1},
        {"ext_id": "q3", "type": "short_text", "correct": ["TCP"], "options": {}, "weight": 1},
    ]
    r = grade_submission({"q1": ["A"], "q2": ["10"], "q3": ["tcp"]}, qs, 0.9, missing_question_weight=1)
    assert r.score == 3.0
    assert r.fraction == 1.0
    assert r.passed is True


def test_mixed_bank_partial():
    qs = [
        {"ext_id": "q1", "type": "single", "correct": ["A"], "options": ["A", "B"], "weight": 1},
        {"ext_id": "q2", "type": "numeric", "correct": 10, "options": {}, "weight": 1},
        {"ext_id": "q3", "type": "short_text", "correct": ["TCP"], "options": {}, "weight": 1},
    ]
    # q1 correct, q2 wrong (off by 1, no tolerance), q3 wrong
    r = grade_submission({"q1": ["A"], "q2": ["11"], "q3": ["UDP"]}, qs, 0.9, missing_question_weight=1)
    assert r.score == 1.0
    assert r.max_score == 3.0
    assert abs(r.fraction - 1 / 3) < 1e-9
    assert r.passed is False


# ---------------------------------------------------------------------------
# bank_loader — validation of new types
# ---------------------------------------------------------------------------

def test_bank_loader_rejects_numeric_no_parseable_target():
    q = {"id": "bad_num", "stem": "s", "type": "numeric", "correct": "notanumber",
         "rubric_category": "analysis", "weight": 1, "options": {}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert any("numeric correct" in e for e in errors)


def test_bank_loader_rejects_numeric_missing_correct():
    q = {"id": "bad_num", "stem": "s", "type": "numeric", "correct": None,
         "rubric_category": "analysis", "weight": 1, "options": {}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert any("numeric correct" in e for e in errors)


def test_bank_loader_rejects_short_text_empty_list():
    q = {"id": "bad_st", "stem": "s", "type": "short_text", "correct": [],
         "rubric_category": "analysis", "weight": 1, "options": {}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert any("short_text correct" in e for e in errors)


def test_bank_loader_rejects_short_text_non_list():
    q = {"id": "bad_st", "stem": "s", "type": "short_text", "correct": "TCP",
         "rubric_category": "analysis", "weight": 1, "options": {}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert any("short_text correct" in e for e in errors)


def test_bank_loader_accepts_valid_numeric():
    q = {"id": "good_num", "stem": "s", "type": "numeric", "correct": 42,
         "rubric_category": "analysis", "weight": 1, "options": {"tolerance": 0.5}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert not any("numeric" in e for e in errors)


def test_bank_loader_accepts_valid_short_text():
    q = {"id": "good_st", "stem": "s", "type": "short_text",
         "correct": ["TCP", "Transmission Control Protocol"],
         "rubric_category": "analysis", "weight": 1, "options": {}, "explanation": ""}
    errors = lint_bank(_doc(q))
    assert not any("short_text" in e for e in errors)
