from pathlib import Path
from uuid import uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_learning import CourseWrite, bank_questions, create_course

from app.owner_composition import compose_migration_owner_declarations
from app.services.bank_loader import lint_bank, load_bank, parse_bank

FX = Path(__file__).resolve().parent.parent / "fixtures" / "banks" / "foundation-ch3.yaml"


@pytest.fixture(autouse=True)
def _audit_registry_installed(monkeypatch):
    # The registry is process-global and installed only by the app entry points
    # (create_app / bind_academy_database_runtime); install the composed one.
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    monkeypatch.setattr("dotmac_kernel.audit_actions._active_registry", registry)


def test_lint_passes_on_balanced_bank():
    assert lint_bank(parse_bank(FX)) == []


def test_lint_flags_bad_correct():
    doc = parse_bank(FX)
    doc.questions[0]["correct"] = ["Z"]  # not an option
    assert any("not in options" in v for v in lint_bank(doc))


def test_lint_flags_answer_that_is_always_the_longest_option():
    """A bank whose answer is spottable by length measures nothing."""
    doc = parse_bank(FX)
    for q in doc.questions:
        correct = q["correct"][0]
        q["options"] = [correct + " because of the full careful reasoning", "No", "Yes", "Maybe"]
        q["correct"] = [correct + " because of the full careful reasoning"]
    violations = lint_bank(doc)
    assert any("correct answer is the longest option" in v for v in violations)
    assert any("the length of their distractors" in v for v in violations)


def test_lint_flags_answer_that_is_always_the_shortest_option():
    """Over-correcting a long-answer bank produces the same tell, inverted."""
    doc = parse_bank(FX)
    for q in doc.questions:
        q["options"] = [
            "Yes",
            "No because the reasoning runs on",
            "Maybe, on the other hand",
            "It depends on several conditions",
        ]
        q["correct"] = ["Yes"]
    violations = lint_bank(doc)
    assert any("the shortest option" in v for v in violations)
    assert any("consistently shorter" in v for v in violations)


def test_lint_ignores_length_ties():
    """Several options sharing the top length leak no signal, so they don't count."""
    doc = parse_bank(FX)
    for q in doc.questions:
        q["options"] = ["AAAA", "BBBB", "CCCC", "DDDD"]
        q["correct"] = ["AAAA"]
    assert not any("distractor balance" in v for v in lint_bank(doc))


def test_lint_ignores_truefalse_options():
    """ "false" is one character longer than "true"; that is not an authoring choice."""
    doc = parse_bank(FX)
    for q in doc.questions:
        q["type"] = "truefalse"
        q["options"] = ["true", "false"]
        q["correct"] = ["false"]
    assert not any("distractor balance" in v for v in lint_bank(doc))


def test_lint_skips_balance_check_on_small_banks():
    """Under five comparable questions, one item swings the share meaninglessly."""
    doc = parse_bank(FX)
    doc.questions = doc.questions[:4]
    for q in doc.questions:
        q["options"] = ["A much longer correct answer here", "No", "Yes", "Maybe"]
        q["correct"] = ["A much longer correct answer here"]
        q["rubric_category"] = "application"
    assert not any("distractor balance" in v for v in lint_bank(doc))


def test_lint_does_not_penalise_multi_for_containing_the_longest_option():
    """A multi question with several answers holds the longest string by construction."""
    doc = parse_bank(FX)
    for q in doc.questions:
        q["type"] = "multi"
        q["options"] = ["Alpha item here", "Beta item here", "Gamma item here", "Delta item"]
        q["correct"] = ["Alpha item here", "Beta item here", "Gamma item here"]
    assert not any("longest option" in v for v in lint_bank(doc))


def test_parse_bank_defaults_to_no_policy():
    """A bank without a policy block leaves the activity's settings alone."""
    assert parse_bank(FX).policy == {}


def test_lint_accepts_a_valid_policy():
    doc = parse_bank(FX)
    doc.policy = {"pool": 5, "max_attempts": 3, "mode": "exam"}
    assert not any("policy" in v for v in lint_bank(doc))


def test_lint_rejects_a_pool_that_holds_nothing_back():
    """A pool at or above the bank size draws everything, which is the old behaviour."""
    doc = parse_bank(FX)
    doc.policy = {"pool": len(doc.questions) + 1}
    assert any("nothing is held back" in v for v in lint_bank(doc))


def test_lint_rejects_unknown_policy_keys_and_bad_values():
    doc = parse_bank(FX)
    doc.policy = {"timed": 600, "mode": "proctored", "max_attempts": 0}
    violations = lint_bank(doc)
    assert any("unknown key(s) timed" in v for v in violations)
    assert any("mode must be one of" in v for v in violations)
    assert any("max_attempts must be a positive integer" in v for v in violations)


def _publish_draft(admin_session, *, tenant_id, course_id):
    """Publish the course bank's draft as a hosted DB test would in the UI."""
    from app.services import bank_authoring

    bank = bank_authoring.find_bank(
        admin_session, tenant_id=tenant_id, course_id=course_id, chapter_number=3, kind="chapter"
    )
    assert bank is not None
    draft = bank_authoring.draft_of(admin_session, tenant_id=tenant_id, bank_id=bank.id)
    assert draft is not None
    context = learning.BankAuditContext(
        tenant_id=tenant_id, actor_type="system", source_application="dotmac_academy_app"
    )
    result = learning.publish_bank_version(
        admin_session,
        learning.PublishBankVersionCommand(tenant_id=tenant_id, version_id=draft.id),
        audit_context=context,
        course_slug="foundation",
    )
    assert result.published is True
    return result


def test_load_bank_imports_as_draft_until_published(admin_session, tenant_a):
    c = create_course(
        admin_session,
        CourseWrite(
            id=uuid4(),
            tenant_id=tenant_a.id,
            slug="foundation",
            title="F",
            discipline="networking",
            source_ref="x",
            version=1,
            status="published",
            listed=False,
        ),
    )
    admin_session.flush()
    bank = load_bank(admin_session, tenant_id=tenant_a.id, course_id=c.id, doc=parse_bank(FX))
    admin_session.flush()
    # Import lands as an unpublished draft: sittings see nothing yet.
    assert len(bank_questions(admin_session, tenant_id=tenant_a.id, bank_id=bank.id)) == 0
    _publish_draft(admin_session, tenant_id=tenant_a.id, course_id=c.id)
    admin_session.flush()
    assert len(bank_questions(admin_session, tenant_id=tenant_a.id, bank_id=bank.id)) == 10
    admin_session.rollback()


def test_reimport_identical_after_publish_is_noop(admin_session, tenant_a):
    c = create_course(
        admin_session,
        CourseWrite(
            id=uuid4(),
            tenant_id=tenant_a.id,
            slug="foundation",
            title="F",
            discipline="networking",
            source_ref="x",
            version=1,
            status="published",
            listed=False,
        ),
    )
    admin_session.flush()
    bank1 = load_bank(admin_session, tenant_id=tenant_a.id, course_id=c.id, doc=parse_bank(FX))
    admin_session.flush()
    _publish_draft(admin_session, tenant_id=tenant_a.id, course_id=c.id)
    admin_session.flush()
    bank2 = load_bank(admin_session, tenant_id=tenant_a.id, course_id=c.id, doc=parse_bank(FX))
    admin_session.flush()
    assert bank1.id == bank2.id
    assert len(bank_questions(admin_session, tenant_id=tenant_a.id, bank_id=bank2.id)) == 10
    admin_session.rollback()


def test_lint_checks_balance_within_each_competency_category():
    """A bank can look balanced overall while one competency is fully guessable."""
    doc = parse_bank(FX)
    doc.questions = []
    for i in range(6):  # safety: answer always the longest
        doc.questions.append(
            {
                "id": f"safe-{i}",
                "type": "single",
                "category": "safety",
                "options": [f"The full careful safety answer number {i}", "No", "Yes", "Maybe"],
                "correct": [f"The full careful safety answer number {i}"],
                "rubric_category": "application",
                "weight": 1,
            }
        )
    for i in range(14):  # numeracy: balanced, and enough to mask it in the total
        doc.questions.append(
            {
                "id": f"num-{i}",
                "type": "single",
                "category": "numeracy",
                "options": ["AAAA", "BBBB", "CCCC", "DDDD"],
                "correct": ["AAAA"],
                "rubric_category": "application" if i > 3 else "recall",
                "weight": 1,
            }
        )
    violations = lint_bank(doc)
    assert any(v.startswith("safety: distractor balance") for v in violations)
    assert not any(v.startswith("numeracy: distractor balance") for v in violations)


def test_lint_skips_category_check_when_there_is_only_one():
    """A single-category bank is already covered by the bank-level check."""
    doc = parse_bank(FX)
    for q in doc.questions:
        q["category"] = "general"
    assert not any(v.startswith("general:") for v in lint_bank(doc))


def test_lint_checks_balance_within_a_competency_too():
    """A competency-tagged bank must get the same per-domain check as a category one."""
    doc = parse_bank(FX)
    doc.questions = []
    for i in range(6):
        doc.questions.append(
            {
                "id": f"safe-{i}",
                "type": "single",
                "competency": "safety",
                "options": [f"The full careful safety answer number {i}", "No", "Yes", "Maybe"],
                "correct": [f"The full careful safety answer number {i}"],
                "rubric_category": "application",
                "weight": 1,
            }
        )
    for i in range(14):
        doc.questions.append(
            {
                "id": f"net-{i}",
                "type": "single",
                "competency": "net-fundamentals",
                "options": ["AAAA", "BBBB", "CCCC", "DDDD"],
                "correct": ["AAAA"],
                "rubric_category": "application" if i > 3 else "recall",
                "weight": 1,
            }
        )
    assert any(v.startswith("safety: distractor balance") for v in lint_bank(doc))


def test_lint_reports_a_mapping_shaped_option_instead_of_crashing():
    """Option text of the form "Word: rest" parses as a dict, not a string."""
    doc = parse_bank(FX)
    doc.questions[0]["options"] = [{"Flawed": "the reasoning does not hold"}, "B", "C"]
    violations = lint_bank(doc)  # must not raise
    assert any("option is not text" in v for v in violations)
    assert any("YAML mapping" in v for v in violations)


def test_lint_accepts_a_declared_pass_threshold():
    doc = parse_bank(FX)
    doc.policy = {"pass_threshold": 0.65}
    assert not any("pass_threshold" in v for v in lint_bank(doc))


def test_lint_rejects_a_pass_threshold_outside_zero_to_one():
    """A pass mark is a fraction; 70 means 7000%, and would pass nobody."""
    doc = parse_bank(FX)
    doc.policy = {"pass_threshold": 70}
    assert any("fraction between 0 and 1" in v for v in lint_bank(doc))


def test_lint_rejects_a_non_numeric_pass_threshold():
    doc = parse_bank(FX)
    doc.policy = {"pass_threshold": "high"}
    assert any("must be a number" in v for v in lint_bank(doc))


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
