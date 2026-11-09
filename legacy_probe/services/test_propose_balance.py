"""`propose_balance` must only ever suggest editing a distractor.

The whole approach rests on one property: a distractor is already the wrong
answer, so appending a hedge that asserts no new fact cannot make it more or
less wrong. The same hedge on a *correct* answer would be a content change, and
on an explanation it would be a factual claim. If a future edit let this touch
anything but a distractor, the safety argument would silently stop holding —
these tests are what keeps that from happening quietly.
"""

from __future__ import annotations

from app.services.bank_lint import BankDoc, lint_bank, propose_balance


def _doc(questions: list[dict]) -> BankDoc:
    return BankDoc(course="c", kind="chapter", chapter=1, version=1, questions=questions, policy={})


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


def test_the_suggestion_is_never_the_correct_answer():
    answer = "x" * 100
    rows = propose_balance(_doc([_q("q1", answer, ["y" * 90, "z" * 40, "w" * 50])]))
    assert rows, "an answer-longest question should be proposed on"
    for row in rows:
        assert row["option"] != answer
        assert answer not in (row["suggestion"] or "")


def test_a_suggestion_actually_clears_the_answer():
    """A proposal that lands short would cost a whole review cycle to discover."""
    answer = "x" * 100
    rows = propose_balance(_doc([_q("q1", answer, ["y" * 90, "z" * 40, "w" * 50])]))
    assert len(rows[0]["suggestion"]) > len(answer)


def test_a_straddled_question_is_not_proposed_on():
    rows = propose_balance(_doc([_q("q1", "x" * 20, ["y" * 25, "z" * 15, "w" * 20])]))
    assert rows == []


def test_a_gap_beyond_the_hedge_library_is_flagged_not_guessed():
    """Better an honest 'rewrite this' than a 200-character hedge nobody would
    ship. Two of ch08's eight flagged questions landed here."""
    rows = propose_balance(_doc([_q("q1", "x" * 400, ["y" * 40, "z" * 30, "w" * 20])]))
    assert rows[0]["suggestion"] is None
    assert "rewrite" in rows[0]["note"]


def test_applying_every_suggestion_clears_the_lint():
    """End to end: the proposals must actually fix the bank, not merely look
    plausible. This is the property the whole tool is for.

    Six questions, not three: the balance rule exempts banks below
    `_MIN_BALANCE_SAMPLE` (5), where a single question would swing the share
    too far. A three-question fixture is not a broken bank, it is an exempt
    one — which is why the fixture-sanity assertion below is worth keeping.
    """
    doc = _doc(
        [
            _q("q1", "x" * 60, ["y" * 55, "z" * 50, "w" * 45]),
            _q("q2", "x" * 70, ["y" * 66, "z" * 40, "w" * 35]),
            _q("q3", "x" * 30, ["y" * 28, "z" * 25, "w" * 20]),
            _q("q4", "x" * 90, ["y" * 84, "z" * 60, "w" * 55]),
            _q("q5", "x" * 45, ["y" * 41, "z" * 38, "w" * 33]),
            _q("q6", "x" * 52, ["y" * 48, "z" * 44, "w" * 40]),
        ]
    )
    assert any("distractor" in v.lower() for v in lint_bank(doc)), "fixture not broken"

    proposals = {r["id"]: r for r in propose_balance(doc)}
    for question in doc.questions:
        row = proposals.get(question["id"])
        if not row or not row["suggestion"]:
            continue
        question["options"] = [row["suggestion"] if o == row["option"] else o for o in question["options"]]

    remaining = [v for v in lint_bank(doc) if "distractor" in v.lower()]
    assert not remaining, remaining
