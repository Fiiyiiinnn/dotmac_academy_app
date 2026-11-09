from app.services.grading import grade_submission

QS = [
    {"ext_id": "q1", "type": "single", "correct": ["A"], "weight": 1},
    {"ext_id": "q2", "type": "multi", "correct": ["A", "C"], "weight": 2},
    {"ext_id": "q3", "type": "truefalse", "correct": ["true"], "weight": 1},
]


def test_all_correct_passes():
    r = grade_submission({"q1": ["A"], "q2": ["C", "A"], "q3": ["true"]}, QS, 0.6, missing_question_weight=1)
    assert r.score == 4 and r.max_score == 4 and r.fraction == 1.0 and r.passed is True


def test_partial_below_threshold_fails():
    r = grade_submission({"q1": ["A"], "q2": ["A"], "q3": ["false"]}, QS, 0.6, missing_question_weight=1)
    assert r.score == 1 and r.max_score == 4 and r.passed is False
    item = next(i for i in r.per_item if i["id"] == "q2")
    assert item["correct"] is False and item["expected"] == ["A", "C"]


def test_empty_bank_is_zero_not_crash():
    r = grade_submission({}, [], 0.6, missing_question_weight=1)
    assert r.max_score == 0 and r.fraction == 0.0 and r.passed is False


# ── Beyond-MCQ question types ──────────────────────────────────────────────────


def test_numeric_exact_single_value():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": [42], "weight": 1}]
    assert grade_submission({"n1": ["42"]}, qs, 0.6, missing_question_weight=1).passed is True
    assert grade_submission({"n1": ["42.0"]}, qs, 0.6, missing_question_weight=1).passed is True
    assert grade_submission({"n1": ["43"]}, qs, 0.6, missing_question_weight=1).passed is False


def test_numeric_range_inclusive():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": [4.9, 5.1], "weight": 1}]
    assert grade_submission({"n1": ["5"]}, qs, 0.6, missing_question_weight=1).passed is True
    assert grade_submission({"n1": ["4.9"]}, qs, 0.6, missing_question_weight=1).passed is True  # inclusive
    assert grade_submission({"n1": ["5.2"]}, qs, 0.6, missing_question_weight=1).passed is False


def test_numeric_unparseable_is_wrong_not_crash():
    qs = [{"ext_id": "n1", "type": "numeric", "correct": [42], "weight": 1}]
    r = grade_submission({"n1": ["not a number"]}, qs, 0.6, missing_question_weight=1)
    assert r.passed is False and r.fraction == 0.0


def test_short_text_normalized_match():
    qs = [{"ext_id": "t1", "type": "short_text", "correct": ["Router", "gateway router"], "weight": 1}]
    assert (
        grade_submission({"t1": ["  ROUTER "]}, qs, 0.6, missing_question_weight=1).passed is True
    )  # case/space-insensitive
    assert (
        grade_submission({"t1": ["gateway   router"]}, qs, 0.6, missing_question_weight=1).passed is True
    )  # collapse ws
    assert grade_submission({"t1": ["switch"]}, qs, 0.6, missing_question_weight=1).passed is False
    assert grade_submission({"t1": []}, qs, 0.6, missing_question_weight=1).passed is False  # blank
