"""Hosted-only attempt adapters and pure facade identity checks; no DB proof."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import attempt_policy, attempts, exam_engine, grading


def _attempt(tenant_id, person_id, activity_id, now, *, submitted=False):
    return learning.AttemptFact(
        id=uuid4(),
        tenant_id=tenant_id,
        person_id=person_id,
        activity_id=activity_id,
        question_ext_ids=["q2", "q1"],
        started_at=now,
        submitted_at=now if submitted else None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("mode", ["none", "empty", "dict-options", "null-options"])
def test_open_command_preserves_inputs_and_existing_retakes(monkeypatch, mode):
    session = object()
    tenant_id, person_id, activity_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)
    questions = (
        None
        if mode == "none"
        else []
        if mode == "empty"
        else [
            SimpleNamespace(
                ext_id="q1",
                category=None,
                options={"tolerance": 0.5, "nested": [1, None]} if mode == "dict-options" else None,
            )
        ]
    )
    first = _attempt(tenant_id, person_id, activity_id, now)
    retake = _attempt(tenant_id, person_id, activity_id, now)
    expected = [first, first, retake]
    commands = []

    def owner(db, command):
        assert db is session and isinstance(command, learning.OpenAttemptCommand)
        commands.append(command)
        return learning.AttemptWrite(expected[len(commands) - 1])

    monkeypatch.setattr(learning, "open_or_create_attempt", owner)
    results = [
        attempts.open_or_create_attempt(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            all_ext_ids=["q2", "q1"],
            count=7,
            now=now,
            questions=questions,
        )
        for _ in range(3)
    ]
    assert results == expected and results[0] is results[1] and results[2] is retake
    for command in commands:
        assert (command.tenant_id, command.person_id, command.activity_id) == (
            tenant_id,
            person_id,
            activity_id,
        )
        assert command.all_ext_ids == ["q2", "q1"] and command.count == 7 and command.now == now
        if mode == "none":
            assert command.questions is None
        elif mode == "empty":
            assert command.questions == ()
        else:
            assert len(command.questions) == 1
            assert command.questions[0].ext_id == "q1" and command.questions[0].category is None
            assert command.questions[0].options == questions[0].options


@pytest.mark.parametrize("missing", [False, True])
def test_close_returns_public_fact_or_none_unchanged(monkeypatch, missing):
    session = object()
    tenant_id, person_id, activity_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    expected = None if missing else _attempt(tenant_id, person_id, activity_id, now, submitted=True)

    def owner(db, command):
        assert db is session and isinstance(command, learning.CloseAttemptCommand)
        assert command.model_dump() == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "activity_id": activity_id,
            "now": now,
        }
        return expected

    monkeypatch.setattr(learning, "close_open_attempt", owner)
    assert (
        attempts.close_open_attempt(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            now=now,
        )
        is expected
    )


def test_grant_wrapper_keeps_explicit_context_separate_from_accountability(monkeypatch):
    session = object()
    tenant_id, person_id, activity_id, accountable_id, actor_id = (uuid4() for _ in range(5))
    now = datetime(2026, 10, 17, tzinfo=UTC)
    context = learning.AttemptAuditContext(tenant_id, "user", "academy.test", actor_id=str(actor_id))
    fact = learning.AttemptGrantFact(
        uuid4(),
        tenant_id,
        activity_id,
        person_id,
        2,
        "Power cut",
        accountable_id,
        now,
        now,
    )
    captured = []

    def owner(db, command, *, audit_context):
        assert db is session and isinstance(command, learning.GrantExtraAttemptCommand)
        assert audit_context is context and audit_context.actor_id != str(command.granted_by)
        assert command.reason == "  Power cut  " and command.extra_attempts == 2
        assert (
            command.tenant_id,
            command.person_id,
            command.activity_id,
            command.granted_by,
        ) == (tenant_id, person_id, activity_id, accountable_id)
        captured.append(command)
        return fact

    monkeypatch.setattr(learning, "grant_extra_attempt", owner)
    assert (
        attempt_policy.grant_extra_attempt(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            reason="  Power cut  ",
            granted_by=accountable_id,
            extra_attempts=2,
            audit_context=context,
        )
        is fact
    )
    with pytest.raises(TypeError, match="audit_context"):
        attempt_policy.grant_extra_attempt(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            reason="Power cut",
            granted_by=accountable_id,
        )
    assert len(captured) == 1


@pytest.mark.parametrize(
    "name",
    [
        "ActivityTerms",
        "BoundGrantReader",
        "Entitlement",
        "entitlement",
        "entitlement_with_grants",
        "granted_by_activity",
        "granted_for",
    ],
)
def test_entitlement_facade_is_the_actual_public_owner(name):
    assert getattr(attempt_policy, name) is getattr(learning, name)


@pytest.mark.parametrize(
    ("old", "public"),
    [
        ("Question", "ExamQuestion"),
        ("Sitter", "ExamSitter"),
        ("SelectionPolicy", "SelectionPolicy"),
        ("ValidityTerms", "ValidityTerms"),
        ("select", "select_questions"),
        ("present_options", "present_options"),
        ("chance_baseline", "chance_baseline"),
        ("near_chance_floor", "near_chance_floor"),
        ("duration_floor", "duration_floor"),
        ("check_validity", "check_validity"),
    ],
)
def test_engine_facade_reuses_actual_public_symbols(old, public):
    assert getattr(exam_engine, old) is getattr(learning, public)


def test_constants_and_grade_result_are_public_aliases_not_local_copies():
    expected = {
        "INVALID_NEAR_CHANCE": "near_chance",
        "INVALID_TOO_FAST": "too_fast",
    }
    for name, value in expected.items():
        assert getattr(exam_engine, name) is getattr(learning, name)
        assert getattr(exam_engine, name) == value
    for name in ("SECONDS_PER_QUESTION", "NEAR_CHANCE_MARGIN", "MIN_VALID_FRACTION", "MIN_DURATION_SECONDS"):
        assert not hasattr(exam_engine, name)
        assert not hasattr(learning, name)
    assert grading.GradeResult is learning.GradedResult
    assert grading.grade_submission is learning.grade_submission
    # Field-only consumers retain the actual public grade, including dict options.
    result = grading.grade_submission(
        {"n": ["42.2"], "t": ["Router"]},
        [
            {
                "ext_id": "n",
                "type": "numeric",
                "correct": 42,
                "options": {"tolerance": 0.5},
            },
            {
                "ext_id": "t",
                "type": "short_text",
                "correct": ["route.*"],
                "options": {"regex": True},
            },
        ],
        1.0,
        missing_question_weight=1,
    )
    assert isinstance(result, grading.GradeResult) and result.passed is True
    assert result.score == 2 and result.max_score == 2 and result.fraction == 1
    assert [item["id"] for item in result.per_item] == ["n", "t"]


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import settings_store

    original = settings_store.effective

    def effective(db, *, require_store=False):
        if require_store:
            return {
                "learning_missing_question_weight": 1,
                "learning_default_extra_attempts": 1,
                "learning_default_course_status": "published",
                "learning_default_course_listed": False,
                "learning_exam_seconds_per_question": 12.0,
                "learning_exam_near_chance_margin": 1.33,
                "learning_exam_fallback_score_floor": 1.0 / 3.0,
                "learning_exam_fallback_duration_seconds": 360.0,
            }
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
