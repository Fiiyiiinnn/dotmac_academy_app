"""`balance_targets` must propose lengths that actually satisfy the linter.

The helper exists because rebalancing by eye oscillates: trim an over-long
answer and it becomes the shortest option; pad the distractors and it is the
longest again. If the targets themselves did not converge, the helper would
just automate the oscillation.
"""

from __future__ import annotations

from app.services.bank_lint import BankDoc, balance_targets


def _doc(questions: list[dict]) -> BankDoc:
    return BankDoc(
        course="c", kind="chapter", chapter=1, version=1, questions=questions, policy={}
    )


def _q(qid: str, answer: str, distractors: list[str]) -> dict:
    return {
        "id": qid,
        "stem": "?",
        "type": "single",
        "options": [answer, *distractors],
        "correct": [answer],
        "rubric_category": "recall",
        "explanation": "",
        "weight": 1,
    }


def test_a_straddled_question_is_left_alone():
    """One distractor longer and one shorter is already the stable shape."""
    rows = balance_targets(_doc([_q("q1", "x" * 20, ["y" * 25, "z" * 15, "w" * 20])]))
    assert rows == []


def test_an_answer_longest_question_gets_targets_that_straddle_it():
    answer = "x" * 100
    rows = balance_targets(_doc([_q("q1", answer, ["y" * 40, "z" * 45, "w" * 50])]))
    assert len(rows) == 1
    targets = [d["target"] for d in rows[0]["distractors"]]
    assert max(targets) > len(answer), "nothing longer — answer stays the longest"
    assert min(targets) < len(answer), "nothing shorter — answer becomes the shortest"


def test_an_answer_shortest_question_also_gets_straddling_targets():
    """The over-correction case: a trimmed answer surrounded by long distractors."""
    answer = "x" * 40
    rows = balance_targets(_doc([_q("q1", answer, ["y" * 90, "z" * 95, "w" * 100])]))
    assert len(rows) == 1
    targets = [d["target"] for d in rows[0]["distractors"]]
    assert min(targets) < len(answer) < max(targets)


def test_targets_put_the_mean_ratio_near_one():
    """Near 1.0, not merely inside the band — that margin is what stops the
    next small edit tipping the bank back over the threshold."""
    answer = "x" * 100
    rows = balance_targets(_doc([_q("q1", answer, ["y" * 30, "z" * 30, "w" * 30])]))
    targets = [d["target"] for d in rows[0]["distractors"]]
    ratio = len(answer) / (sum(targets) / len(targets))
    assert 0.90 <= ratio <= 1.10, f"ratio {ratio:.2f} leaves no headroom"


def test_equal_length_options_are_not_reported():
    """All options the same length is ideal, not work. The linter flags only a
    *strictly* longest or shortest answer, so reporting ties would invite
    damaging a balanced question — "20% / 35% / 50% / 75%" needs nothing."""
    rows = balance_targets(_doc([_q("q1", "50%", ["20%", "35%", "75%"])]))
    assert rows == []


def test_an_answer_tied_for_shortest_is_not_reported():
    rows = balance_targets(_doc([_q("q1", "x" * 13, ["y" * 13, "z" * 16, "w" * 20])]))
    assert rows == []


def test_two_option_questions_are_skipped():
    """True/false cannot straddle, and they are 1% of flags estate-wide."""
    assert balance_targets(_doc([_q("q1", "false", ["true"])])) == []
