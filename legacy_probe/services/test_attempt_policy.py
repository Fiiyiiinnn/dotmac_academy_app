"""The attempt entitlement, and the grant that changes it.

The arithmetic is tested without a database wherever it can be, because the
whole point of the module is that five callers share one expression — a pure
function they can all reach is what makes that true.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import attempt_policy


class _Activity:
    """Only what the entitlement reads."""

    def __init__(self, max_attempts):
        self.max_attempts = max_attempts
        self.min_attempt_interval_seconds = 0


def test_uncapped_activity_is_never_exhausted():
    ent = attempt_policy.entitlement(_Activity(None), used=99)
    assert ent.exhausted is False
    # None, not a large number: callers render "unlimited", and a number would
    # invite them to count down from it.
    assert ent.remaining is None


def test_capped_activity_counts_down_and_then_exhausts():
    act = _Activity(3)
    assert attempt_policy.entitlement(act, used=0).remaining == 3
    assert attempt_policy.entitlement(act, used=2).remaining == 1
    assert attempt_policy.entitlement(act, used=2).exhausted is False
    assert attempt_policy.entitlement(act, used=3).exhausted is True


def test_remaining_never_goes_negative():
    """A grant withdrawn by lowering max_attempts must not render as -1 left."""
    assert attempt_policy.entitlement(_Activity(2), used=5).remaining == 0


def test_a_grant_raises_the_limit_for_that_learner_only():
    """The whole reason grants exist: max_attempts alone reopens it for everyone."""
    act = _Activity(2)
    assert attempt_policy.entitlement(act, used=2).exhausted is True
    assert attempt_policy.entitlement(act, used=2, granted=1).exhausted is False
    assert attempt_policy.entitlement(act, used=2, granted=1).remaining == 1


def test_grants_accumulate():
    """Two separate grants of one are three attempts, not two."""
    assert attempt_policy.entitlement(_Activity(1), used=1, granted=2).remaining == 2


def test_a_grant_on_an_uncapped_activity_changes_nothing():
    ent = attempt_policy.entitlement(_Activity(None), used=4, granted=3)
    assert ent.limit is None and ent.exhausted is False


@pytest.mark.parametrize(
    "max_attempts, used, granted, limit, remaining, exhausted",
    [
        (3, 2, 0, 3, 1, False),
        (3, 3, 0, 3, 0, True),
        (2, 2, 1, 3, 1, False),
        (1, 1, 2, 3, 2, False),
        (2, 5, 0, 2, 0, True),
        (None, 99, 3, None, None, False),
    ],
)
def test_entitlement_with_grants_reads_bound_total_once(max_attempts, used, granted, limit, remaining, exhausted):
    class Reader:
        calls = 0

        def total(self):
            self.calls += 1
            return granted

    reader = Reader()
    ent = attempt_policy.entitlement_with_grants(_Activity(max_attempts), used=used, reader=reader)

    assert ent == attempt_policy.Entitlement(used=used, granted=granted, limit=limit)
    assert ent.remaining == remaining
    assert ent.exhausted is exhausted
    assert reader.calls == 1


def test_entitlement_with_grants_propagates_reader_failure():
    failure = RuntimeError("grant read failed")

    class Reader:
        def total(self):
            raise failure

    with pytest.raises(RuntimeError) as raised:
        attempt_policy.entitlement_with_grants(_Activity(2), used=1, reader=Reader())

    assert raised.value is failure


def test_for_learner_uses_supplied_session_and_exact_grant_scope(monkeypatch):
    db = object()
    tenant_id, person_id, activity_id = uuid4(), uuid4(), uuid4()
    activity = _Activity(2)
    activity.id = activity_id
    calls = []
    expected = learning.Entitlement(used=4, granted=3, limit=5)

    def public_for_learner(supplied_db, *, tenant_id, person_id, activity, used):
        calls.append((supplied_db, tenant_id, person_id, activity, used))
        return expected

    monkeypatch.setattr(learning, "for_learner", public_for_learner)

    ent = attempt_policy.for_learner(db, tenant_id=tenant_id, person_id=person_id, activity=activity, used=4)

    assert len(calls) == 1
    supplied_db, supplied_tenant, supplied_person, supplied_activity, supplied_used = calls[0]
    assert supplied_db is db
    assert supplied_tenant is tenant_id
    assert supplied_person is person_id
    assert supplied_activity is activity
    assert supplied_activity.id is activity_id
    assert supplied_used == 4
    assert ent is expected
    assert ent == attempt_policy.Entitlement(used=4, granted=3, limit=5)
    assert ent.remaining == 1
    assert ent.exhausted is False


class _ValidationOnlySession:
    """Supply the required active caller root; forbid all persistence actions."""

    def __init__(self):
        self.root = SimpleNamespace(is_active=True)

    def get_transaction(self):
        return self.root

    def get_nested_transaction(self):
        return None

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("grant validation reached SQL or mutation")

    execute = scalar = scalars = add = flush = begin = commit = rollback = _forbidden


def test_grant_requires_a_reason():
    """A grant reopens a graded assessment for one person; the next
    administrator to look needs to know whether that was a power cut or a favour."""
    tenant_id, person_id, activity_id, actor_id = (uuid4() for _ in range(4))
    context = learning.AttemptAuditContext(tenant_id, "user", "academy.test", actor_id=str(actor_id))
    with pytest.raises(ValueError, match="why"):
        attempt_policy.grant_extra_attempt(
            _ValidationOnlySession(),
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            reason="   ",
            granted_by=actor_id,
            audit_context=context,
        )


def test_grant_must_add_at_least_one_attempt():
    tenant_id, person_id, activity_id, actor_id = (uuid4() for _ in range(4))
    context = learning.AttemptAuditContext(tenant_id, "user", "academy.test", actor_id=str(actor_id))
    with pytest.raises(ValueError, match="at least one"):
        attempt_policy.grant_extra_attempt(
            _ValidationOnlySession(),
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            reason="power cut",
            extra_attempts=0,
            granted_by=actor_id,
            audit_context=context,
        )


def test_reveal_feedback_does_not_leak_the_key_before_a_granted_retake():
    """The failure this prevents.

    On a graded activity the answer key is revealed once attempts are spent.
    Granting a retake after that would hand the learner the answers to the
    attempt they are about to sit — so the reveal has to consult the same
    entitlement the submit path does, grants included.
    """
    import dotmac_learning as learning

    from app.services.assessment import reveal_feedback

    act = _Activity(1)
    act.assessment_mode = "graded"
    terms = learning.FeedbackRevealTerms(
        reveal_by_mode={
            "practice": "immediate",
            "graded": "on_pass_or_exhausted",
            "exam": "never",
        }
    )

    # Spent, not passed: the key is shown, which is the point of the mode.
    assert reveal_feedback(act, terms=terms, passed=False, attempts_used=1) is True
    # ...but once a retake is granted, it is no longer spent, and the key closes.
    assert reveal_feedback(act, terms=terms, passed=False, attempts_used=1, attempts_granted=1) is False


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
