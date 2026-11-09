"""Hosted-only production-route boundary canaries; no DB/adoption proof."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_learning import ActivityFact, AssessmentQuestion, QuestionFact, ScoreFact
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from app.web import learn


@pytest.mark.parametrize(
    ("selection", "question_count", "bank_missing", "kind", "options", "expected"),
    [
        (None, None, False, "mcq", ["A", "B"], {"q1": ["A", "B"], "q2": []}),
        (["q1"], 1, False, "mcq", ["A", "B"], {"q1": ["A", "B"]}),
        ([], 1, False, "mcq", ["A", "B"], {}),
        (None, None, True, "mcq", [], {}),
        (None, None, False, "numeric", {"unit": "ms"}, {"q1": ["A", "B"], "q2": []}),
        (
            None,
            None,
            False,
            "short_text",
            {"hint": "text"},
            {"q1": ["A", "B"], "q2": []},
        ),
    ],
    ids=[
        "normal",
        "selected-subset",
        "missing-open-attempt",
        "no-bank",
        "numeric-options",
        "text-options",
    ],
)
def test_submit_public_question_shapes(monkeypatch, selection, question_count, bank_missing, kind, options, expected):
    """Exercise submit itself, including the real result presentation template."""
    now = datetime.now(timezone.utc)
    tenant_id, person_id, course_id, activity_id = (uuid4() for _ in range(4))
    bank_id = None if bank_missing else uuid4()
    activity = ActivityFact(
        id=activity_id,
        tenant_id=tenant_id,
        course_id=course_id,
        title="Check",
        chapter_number=None,
        type="mcq_test",
        bank_id=bank_id,
        pass_threshold=0.5,
        max_attempts=3,
        assessment_mode="graded",
        grading="auto",
        weight=1.0,
        question_count=question_count,
        created_at=now,
        updated_at=now,
    )
    # These are actual public TypedDict values, deliberately ordinary mappings.
    q1 = AssessmentQuestion(ext_id="q1", type=kind, correct=["A"], options=options, weight=1)
    q2 = AssessmentQuestion(ext_id="q2", type="mcq", correct=["B"], options=["B"], weight=1)
    selected = [] if bank_missing or selection == [] else [q1] if selection else [q1, q2]
    stored = (
        ()
        if bank_missing
        else (
            QuestionFact(
                id=uuid4(),
                tenant_id=tenant_id,
                bank_id=bank_id,
                ext_id="q1",
                stem="Stored question stem",
                type=kind,
                options=options,
                correct=["A"],
                rubric_category="",
                category=None,
                explanation="Stored explanation",
                weight=1,
                created_at=now,
                updated_at=now,
            ),
            QuestionFact(
                id=uuid4(),
                tenant_id=tenant_id,
                bank_id=bank_id,
                ext_id="q2",
                stem="Unselected question stem",
                type="mcq",
                options=["B"],
                correct=["B"],
                rubric_category="",
                category=None,
                explanation="Other explanation",
                weight=1,
                created_at=now,
                updated_at=now,
            ),
        )
    )
    score = ScoreFact(
        id=uuid4(),
        tenant_id=tenant_id,
        submission_id=uuid4(),
        score=1,
        max_score=1,
        fraction=1,
        passed=True,
        per_item=[] if bank_missing else [{"id": "q1", "correct": True, "expected": ["A"]}],
        source="auto",
        override_reason=None,
        created_at=now,
        updated_at=now,
    )
    session = object()
    person = SimpleNamespace(id=person_id)
    calls = []
    captured = {}

    async def form():
        return FormData([("q1", "A"), ("q1", "B"), ("unknown", "injected")])

    request = SimpleNamespace(form=form)
    monkeypatch.setattr(learn, "require_tenant", lambda request: SimpleNamespace(id=tenant_id))
    monkeypatch.setattr(learn, "get_activity", lambda db, **kw: activity)
    monkeypatch.setattr(learn, "require_course_open", lambda *args, **kw: None)
    monkeypatch.setattr(learn, "require_activity_submittable", lambda *args, **kw: None)
    # learn.submit reads the open attempt WITHOUT closing it (Learning grades
    # against its bank-version pin) and closes it after a successful submit.
    def open_attempt(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "person_id": person_id, "activity_id": activity_id}
        calls.append("open_attempt")
        return None if selection == [] else SimpleNamespace(question_ext_ids=selection, bank_version_id=None)

    def close_attempt(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "person_id": person_id, "activity_id": activity_id}
        calls.append("close")

    monkeypatch.setattr(learn, "get_open_attempt", open_attempt)
    monkeypatch.setattr(learn, "close_open_attempt", close_attempt)
    monkeypatch.setattr(learn.learning_policy, "feedback_reveal_terms", lambda db: "terms")

    def questions(db, **kw):
        assert db is session
        assert kw == {
            "tenant_id": tenant_id,
            "bank_id": bank_id,
            "only_ext_ids": selection,
            "bank_version_id": activity.bank_version_id,
        }
        calls.append("questions")
        return selected

    def submit(db, **kw):
        assert db is session
        captured["submission"] = kw
        calls.append("submit")
        return score

    def display(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "bank_id": bank_id, "bank_version_id": activity.bank_version_id}
        calls.append("display")
        return stored

    def course(db, **kw):
        assert db is session
        calls.append("course")
        return None

    def render(request, name, context):
        assert name == "_activity_result.html"
        captured["context"] = context
        return HTMLResponse(learn.templates.env.get_template(name).render(context))

    monkeypatch.setattr(learn, "questions_for", questions)
    monkeypatch.setattr(learn, "submit_activity", submit)
    monkeypatch.setattr(learn, "bank_questions", display)
    monkeypatch.setattr(learn, "get_course", course)
    monkeypatch.setattr(learn, "attempts_used", lambda *args, **kw: 1)
    monkeypatch.setattr(learn, "reveal_feedback", lambda *args, **kw: True)
    monkeypatch.setattr(learn.templates, "TemplateResponse", render)

    response = asyncio.run(learn.submit(activity_id, request, person=person, db=session))

    assert captured["submission"]["answers"] == expected
    assert captured["submission"]["only_ext_ids"] == selection
    assert captured["submission"]["activity"] is activity
    pooled = question_count is not None
    expected_calls = (["open_attempt"] if pooled else []) + ["questions", "submit"]
    expected_calls += (["close"] if pooled else []) + ["display", "course"]
    assert calls == expected_calls
    context = captured["context"]
    assert context["score"] is score and context["reveal"] is True
    assert context["course"] is None and context["course_completion"] is None
    assert context["next_chapter"] is None
    if bank_missing:
        assert context["questions"] == {}
    else:
        assert context["questions"]["q1"] is stored[0]
        assert context["questions"]["q2"] is stored[1]  # display retains the full bank
        assert context["questions"]["q1"].options == options
        assert b"Stored question stem" in response.body
        assert b"Stored explanation" in response.body
    assert response.status_code == 200
