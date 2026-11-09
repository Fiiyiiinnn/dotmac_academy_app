"""Pin the exam behaviour that exists today, before it moves into the engine.

The entrance exam is live and ranking real candidates, so the refactor has to
prove it changed nothing. These tests capture the *current* selection and option
order for fixed inputs; they are written against the public functions so they
keep passing after those functions delegate to the engine.

If one of these fails during the refactor, a candidate's paper changed.
"""

from __future__ import annotations

import pytest

from app.services import entrance_exam, exam_engine


class _Q:
    def __init__(self, ext_id: str, category: str | None = None, options=None):
        self.ext_id = ext_id
        self.category = category
        self.options = options or ["alpha", "bravo", "charlie", "delta"]


class _Applicant:
    def __init__(self, id_: str):
        self.id = id_


BANK = [_Q(f"{cat}-{i:02d}", cat) for cat in ("numeracy", "reading", "safety", "logic", "technical") for i in range(18)]


@pytest.mark.parametrize("applicant_id", ["cand-alpha", "cand-bravo", "cand-charlie"])
def test_selection_is_stable_for_a_given_applicant(applicant_id):
    """A reload or resumed sitting must not redraw the paper."""
    first = [q.ext_id for q in entrance_exam.sample_for(_Applicant(applicant_id), BANK, 6)]
    again = [q.ext_id for q in entrance_exam.sample_for(_Applicant(applicant_id), BANK, 6)]
    assert first == again
    assert len(first) == 30


def test_selection_draws_the_same_shape_for_every_applicant():
    """Per-category counts are what make two candidates' profiles comparable."""
    for applicant_id in ("a", "b", "c", "d", "e", "f"):
        drawn = entrance_exam.sample_for(_Applicant(applicant_id), BANK, 6)
        counts: dict[str, int] = {}
        for q in drawn:
            counts[q.category] = counts.get(q.category, 0) + 1
        assert counts == {
            "numeracy": 6,
            "reading": 6,
            "safety": 6,
            "logic": 6,
            "technical": 6,
        }


def test_selection_differs_between_applicants():
    """Otherwise the 90-question bank leaks exactly as a fixed 30 would."""
    a = {q.ext_id for q in entrance_exam.sample_for(_Applicant("cand-alpha"), BANK, 6)}
    b = {q.ext_id for q in entrance_exam.sample_for(_Applicant("cand-bravo"), BANK, 6)}
    assert a != b


# The exact drawn set for a known applicant. This is the sharpest guard in the
# file: if the hashing or ordering changes at all, this fails, and a live
# candidate's paper would have silently changed with it.
def test_selection_is_byte_stable_against_a_recorded_draw():
    drawn = [q.ext_id for q in entrance_exam.sample_for(_Applicant("cand-alpha"), BANK, 2)]
    assert drawn == [
        "reading-08",
        "safety-09",
        "reading-06",
        "numeracy-00",
        "logic-08",
        "numeracy-04",
        "safety-08",
        "logic-10",
        "technical-09",
        "technical-04",
    ]


def test_option_order_is_stable_per_applicant_and_question():
    """Answers are posted by option text, but a moving order breaks autosave."""
    q = _Q("numeracy-00", "numeracy")
    first = entrance_exam.options_for(_Applicant("cand-alpha"), q)
    again = entrance_exam.options_for(_Applicant("cand-alpha"), q)
    assert first == again
    assert sorted(first) == sorted(q.options)


def test_option_order_differs_between_applicants():
    q = _Q("numeracy-00", "numeracy")
    orders = {
        tuple(entrance_exam.options_for(_Applicant(a), q))
        for a in ("cand-alpha", "cand-bravo", "cand-charlie", "cand-delta")
    }
    assert len(orders) > 1


def test_option_order_is_byte_stable_against_a_recorded_shuffle():
    q = _Q("numeracy-00", "numeracy")
    assert entrance_exam.options_for(_Applicant("cand-alpha"), q) == [
        "delta",
        "alpha",
        "bravo",
        "charlie",
    ]


def test_validity_gate_thresholds():
    """Near-chance and too-fast are absence of data, not weak candidates."""
    assert entrance_exam.check_validity(0.90, 900, terms=VALIDITY_TERMS) == (True, None)
    assert entrance_exam.check_validity(0.33, 900, terms=VALIDITY_TERMS)[1] == entrance_exam.INVALID_NEAR_CHANCE
    assert entrance_exam.check_validity(0.90, 60, terms=VALIDITY_TERMS)[1] == entrance_exam.INVALID_TOO_FAST
    assert entrance_exam.check_validity(0.90, None, terms=VALIDITY_TERMS)[1] == entrance_exam.INVALID_TOO_FAST


# --- the engine, used directly -------------------------------------------


def test_course_learners_now_get_a_per_learner_option_order():
    """The leak this closes: every learner previously saw the same order."""
    from app.services import exam_engine

    q = _Q("ch01-q1", None)
    orders = {tuple(exam_engine.present_options(_Applicant(f"learner-{i}"), q)) for i in range(8)}
    assert len(orders) > 1, "all learners still see one order"
    for order in orders:
        assert sorted(order) == sorted(q.options), "shuffling must not drop or add an option"


def test_flat_selection_draws_the_requested_total():
    from app.services import exam_engine

    drawn = exam_engine.select(_Applicant("l1"), BANK, exam_engine.SelectionPolicy(total=20))
    assert len(drawn) == 20
    assert len({q.ext_id for q in drawn}) == 20


def test_selection_policy_refuses_two_conflicting_draws():
    """per_category and total answer the same question differently."""
    from app.services import exam_engine

    with pytest.raises(ValueError):
        exam_engine.SelectionPolicy(per_category=6, total=20)


def test_empty_policy_returns_the_whole_bank():
    from app.services import exam_engine

    assert len(exam_engine.select(_Applicant("l1"), BANK, exam_engine.SelectionPolicy())) == len(BANK)


def test_a_retake_draws_a_different_paper():
    """Pooling a bank that allows retakes is pointless if attempt 2 repeats attempt 1."""
    from app.services import exam_engine

    policy = exam_engine.SelectionPolicy(total=20)
    first = [q.ext_id for q in exam_engine.select(_Applicant("l1"), BANK, policy, variant="attempt0:")]
    second = [q.ext_id for q in exam_engine.select(_Applicant("l1"), BANK, policy, variant="attempt1:")]
    assert first != second
    # ...but each remains reproducible, which is what lets a reload resume.
    assert first == [q.ext_id for q in exam_engine.select(_Applicant("l1"), BANK, policy, variant="attempt0:")]


def test_variant_does_not_disturb_the_entrance_draw():
    """The entrance exam passes no variant; its recorded paper must be untouched."""
    from app.services import exam_engine

    policy = exam_engine.SelectionPolicy(per_category=2)
    assert [q.ext_id for q in exam_engine.select(_Applicant("cand-alpha"), BANK, policy)] == [
        "reading-08",
        "safety-09",
        "reading-06",
        "numeracy-00",
        "logic-08",
        "numeracy-04",
        "safety-08",
        "logic-10",
        "technical-09",
        "technical-04",
    ]


# --- validity floors derived from the paper ------------------------------


def _paper(n, options_each=4):
    return [_Q(f"q{i:02d}", options=[f"opt{j}" for j in range(options_each)]) for i in range(n)]


def test_time_floor_tracks_the_paper_size():
    """The bug this fixes: a 60-question paper was still gated at six minutes."""
    from app.services import exam_engine

    assert exam_engine.duration_floor(_paper(30), seconds_per_question=12.0) == 360  # unchanged for the old shape
    assert exam_engine.duration_floor(_paper(60), seconds_per_question=12.0) == 720  # and now moves with it

    # 8 minutes on a 60-question paper is too fast, though it cleared the old gate.
    assert (
        exam_engine.check_validity(0.9, 480, questions=_paper(60), terms=VALIDITY_TERMS)[1]
        == exam_engine.INVALID_TOO_FAST
    )
    assert exam_engine.check_validity(0.9, 480, questions=_paper(30), terms=VALIDITY_TERMS) == (True, None)


def test_chance_floor_tracks_the_options_actually_offered():
    """A true/false paper carries a 50% guessing baseline, not 25%."""
    from app.services import exam_engine

    assert exam_engine.chance_baseline(
        _paper(10, options_each=4), fallback_baseline=(1.0 / 3.0) / 1.33
    ) == pytest.approx(0.25)
    assert exam_engine.chance_baseline(
        _paper(10, options_each=2), fallback_baseline=(1.0 / 3.0) / 1.33
    ) == pytest.approx(0.50)

    # 60% on a true/false paper is near chance; on a four-option paper it is not.
    assert (
        exam_engine.check_validity(0.60, 900, questions=_paper(10, options_each=2), terms=VALIDITY_TERMS)[1]
        == exam_engine.INVALID_NEAR_CHANCE
    )
    assert exam_engine.check_validity(0.60, 900, questions=_paper(10, options_each=4), terms=VALIDITY_TERMS) == (
        True,
        None,
    )


def test_without_a_paper_the_historical_constants_still_apply():
    """Callers that cannot supply the paper must behave exactly as before."""
    from app.services import exam_engine

    assert exam_engine.check_validity(0.90, 900, terms=VALIDITY_TERMS) == (True, None)
    assert exam_engine.check_validity(0.33, 900, terms=VALIDITY_TERMS)[1] == exam_engine.INVALID_NEAR_CHANCE
    assert exam_engine.check_validity(0.90, 300, terms=VALIDITY_TERMS)[1] == exam_engine.INVALID_TOO_FAST


VALIDITY_TERMS = exam_engine.ValidityTerms(
    seconds_per_question=12.0,
    near_chance_margin=1.33,
    fallback_score_floor=1.0 / 3.0,
    fallback_duration_seconds=360.0,
)
