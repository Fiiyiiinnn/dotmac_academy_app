"""Entrance assessment: grade an applicant's exam into a competency profile."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.admissions import Applicant
from app.models.assessment import Question, QuestionBank
from app.models.cohort import Cohort
from app.models.course import Course
from app.models.track import CohortTrack, Track
from app.services import admissions, entrance_exam, onboarding
from app.services.exceptions import BadRequestError
from legacy_probe.services.admission_request_fixtures import admission_admin_request


def _bank_with_questions(admin_session, tenant):
    course = Course(
        tenant_id=tenant.id,
        slug=f"c-{uuid.uuid4().hex[:6]}",
        title="Intake",
        discipline="fiber",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    for ext, cat in [("q1", "numeracy"), ("q2", "numeracy"), ("q3", "safety"), ("q4", "safety")]:
        admin_session.add(
            Question(
                tenant_id=tenant.id,
                bank_id=bank.id,
                ext_id=ext,
                stem="?",
                type="single",
                options=["A", "B", "C", "D"],
                correct=["A"],
                rubric_category="recall",
                category=cat,
                explanation="",
                weight=1,
            )
        )
    admin_session.flush()
    return bank


def _cohort(admin_session, tenant, bank=None):
    c = Cohort(
        tenant_id=tenant.id,
        name="FA2",
        discipline="fiber",
        status="active",
        entrance_bank_id=(bank.id if bank else None),
    )
    admin_session.add(c)
    admin_session.flush()
    return c


def _applicant(admin_session, tenant, cohort, *, started=True):
    a = Applicant(
        tenant_id=tenant.id,
        email=f"a{uuid.uuid4().hex[:6]}@x.ex",
        first_name="A",
        last_name="B",
        status="applied",
        cohort_id=cohort.id,
        assessment_started_at=(datetime.now(UTC) - timedelta(minutes=15) if started else None),
    )
    admin_session.add(a)
    admin_session.flush()
    return a


def test_grade_records_profile_and_level(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    # numeracy both correct -> 1.0; safety one correct, one wrong -> 0.5; overall 3/4
    answers = {"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["B"]}
    result = entrance_exam.grade_and_record(admin_session, tenant_id=tenant_a.id, applicant=applicant, answers=answers)
    assert result["score"] == 0.75
    assert result["profile"] == {"numeracy": 1.0, "safety": 0.5}
    assert result["level"] == "advanced"
    assert applicant.assessment_taken_at is not None
    assert applicant.assessment_level == "advanced"
    admin_session.rollback()


def test_level_bands():
    assert entrance_exam.level_for(0.0) == "beginner"
    assert entrance_exam.level_for(0.39) == "beginner"
    assert entrance_exam.level_for(0.4) == "intermediate"
    assert entrance_exam.level_for(0.69) == "intermediate"
    assert entrance_exam.level_for(0.7) == "advanced"
    assert entrance_exam.level_for(1.0) == "advanced"


def test_single_sitting(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    entrance_exam.grade_and_record(admin_session, tenant_id=tenant_a.id, applicant=applicant, answers={"q1": ["A"]})
    with pytest.raises(BadRequestError):
        entrance_exam.grade_and_record(admin_session, tenant_id=tenant_a.id, applicant=applicant, answers={"q1": ["A"]})
    admin_session.rollback()


def test_requires_configured_bank(admin_session, tenant_a):
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank=None))
    with pytest.raises(BadRequestError):
        entrance_exam.grade_and_record(admin_session, tenant_id=tenant_a.id, applicant=applicant, answers={})
    admin_session.rollback()


def test_grade_requires_explicit_server_start(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(
        admin_session,
        tenant_a,
        _cohort(admin_session, tenant_a, bank),
        started=False,
    )
    with pytest.raises(BadRequestError, match="has not been started"):
        entrance_exam.grade_and_record(
            admin_session,
            tenant_id=tenant_a.id,
            applicant=applicant,
            answers={"q1": ["A"]},
        )
    admin_session.rollback()


def test_completed_assessment_satisfies_onboarding_task(admin_session, tenant_a):
    actor_request = admission_admin_request(admin_session, tenant_a)
    bank = _bank_with_questions(admin_session, tenant_a)
    cohort = _cohort(admin_session, tenant_a, bank)
    track = Track(
        tenant_id=tenant_a.id,
        slug=f"fiber-{uuid.uuid4().hex[:6]}",
        name="Fiber",
        status="active",
    )
    admin_session.add(track)
    admin_session.flush()
    admin_session.add(
        CohortTrack(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            track_id=track.id,
            status="active",
        )
    )
    admin_session.flush()
    applicant = _applicant(admin_session, tenant_a, cohort)
    applicant.track_id = track.id
    applicant.program = track.name
    admin_session.flush()
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["A"]},
    )
    for nxt in ("screened", "accepted", "onboarding"):
        admissions.transition_api_applicant(
            admin_session, request=actor_request, applicant_id=applicant.id, to_status=nxt
        )
    tasks = onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=applicant.id)
    entrance = next(t for t in tasks if t.key == "entrance_assessment")
    assert entrance.status == "done"
    admin_session.rollback()


def test_list_ranks_candidates_by_score(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    cohort = _cohort(admin_session, tenant_a, bank)
    high = _applicant(admin_session, tenant_a, cohort)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=high,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["A"]},  # 1.0
    )
    low = _applicant(admin_session, tenant_a, cohort)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=low,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["B"], "q4": ["B"]},  # 0.5 — low, but real signal
    )
    ranked = admissions.list_applicants(admin_session, cohort_id=cohort.id, rank_by_score=True)
    assert [a.id for a in ranked] == [high.id, low.id]
    admin_session.rollback()


def test_ranking_excludes_no_signal_sittings(admin_session, tenant_a):
    """A near-chance sitting must not be ranked.

    Ranking it would seat — or reject — a person on the strength of random clicking,
    and would drop a junk row into the talent pool, which is only worth querying
    later if the rows in it are real.
    """
    bank = _bank_with_questions(admin_session, tenant_a)
    cohort = _cohort(admin_session, tenant_a, bank)
    genuine = _applicant(admin_session, tenant_a, cohort)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=genuine,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["B"]},  # 0.75
    )
    clicker = _applicant(admin_session, tenant_a, cohort)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=clicker,
        answers={"q1": ["A"], "q2": ["B"], "q3": ["B"], "q4": ["B"]},  # 0.25 — at chance
    )
    assert clicker.assessment_valid is False

    ranked = admissions.list_applicants(admin_session, cohort_id=cohort.id, rank_by_score=True)
    assert [a.id for a in ranked] == [genuine.id]  # the clicker is gone

    # ...but an admin can still see them, e.g. to decide who needs a reset
    everyone = admissions.list_applicants(admin_session, cohort_id=cohort.id, rank_by_score=True, include_invalid=True)
    assert clicker.id in [a.id for a in everyone]
    admin_session.rollback()


def _timed(admin_session, tenant, minutes=30):
    bank = _bank_with_questions(admin_session, tenant)
    cohort = _cohort(admin_session, tenant, bank)
    cohort.entrance_time_limit_minutes = minutes
    admin_session.flush()
    return _applicant(admin_session, tenant, cohort, started=False)


def test_start_exam_stamps_once_and_keeps_server_clock_running(admin_session, tenant_a):
    applicant = _timed(admin_session, tenant_a, minutes=30)
    t0 = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    info = entrance_exam.start_exam(admin_session, applicant=applicant, now=t0)
    assert info["limit_minutes"] == 30 and info["remaining_seconds"] == 1800
    assert info["elapsed_seconds"] == 0
    assert applicant.assessment_started_at == t0

    # Re-opening later derives elapsed time from the original server timestamp.
    info2 = entrance_exam.start_exam(admin_session, applicant=applicant, now=t0 + timedelta(minutes=5))
    assert applicant.assessment_started_at == t0
    assert info2["elapsed_seconds"] == 300
    assert info2["remaining_seconds"] == 1500
    admin_session.rollback()


def test_elapsed_snapshot_and_timing_are_server_derived(admin_session, tenant_a):
    applicant = _timed(admin_session, tenant_a, minutes=30)
    t0 = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    entrance_exam.start_exam(admin_session, applicant=applicant, now=t0)
    entrance_exam.record_elapsed(
        admin_session,
        applicant=applicant,
        now=t0 + timedelta(minutes=5),
    )
    assert applicant.assessment_elapsed_seconds == 300

    info = entrance_exam.start_exam(admin_session, applicant=applicant, now=t0 + timedelta(hours=1))
    assert info["elapsed_seconds"] == 3600
    assert info["remaining_seconds"] == 0
    admin_session.rollback()


def test_elapsed_time_never_moves_backwards(admin_session, tenant_a):
    applicant = _timed(admin_session, tenant_a, minutes=30)
    t0 = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    entrance_exam.start_exam(admin_session, applicant=applicant, now=t0)
    entrance_exam.record_elapsed(
        admin_session,
        applicant=applicant,
        now=t0 + timedelta(minutes=5),
    )
    entrance_exam.record_elapsed(
        admin_session,
        applicant=applicant,
        now=t0 + timedelta(minutes=2),
    )
    assert applicant.assessment_elapsed_seconds == 300
    admin_session.rollback()


def test_grade_flags_time_exceeded(admin_session, tenant_a):
    applicant = _timed(admin_session, tenant_a, minutes=30)
    t0 = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    entrance_exam.start_exam(admin_session, applicant=applicant, now=t0)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"]},
        now=t0 + timedelta(minutes=40),
    )
    assert applicant.assessment_time_exceeded is True
    admin_session.rollback()


def test_grade_within_limit_not_exceeded(admin_session, tenant_a):
    applicant = _timed(admin_session, tenant_a, minutes=30)
    t0 = datetime(2026, 7, 11, 10, 0, tzinfo=UTC)
    entrance_exam.start_exam(admin_session, applicant=applicant, now=t0)
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"]},
        now=t0 + timedelta(minutes=20),
    )
    assert applicant.assessment_time_exceeded is False
    admin_session.rollback()


def test_untimed_cohort_never_exceeds(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    info = entrance_exam.start_exam(admin_session, applicant=applicant)
    assert info["limit_minutes"] is None and info["remaining_seconds"] is None
    entrance_exam.grade_and_record(admin_session, tenant_id=tenant_a.id, applicant=applicant, answers={"q1": ["A"]})
    assert applicant.assessment_time_exceeded is False
    admin_session.rollback()


def test_falls_back_to_tenant_default_bank(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    cohort = _cohort(admin_session, tenant_a, bank=None)  # cohort has no own bank
    applicant = _applicant(admin_session, tenant_a, cohort)
    assert entrance_exam.has_entrance_exam(admin_session, applicant=applicant) is False

    entrance_exam.set_academy_defaults(admin_session, tenant_id=tenant_a.id, bank_id=bank.id, time_limit_minutes=30)
    admin_session.flush()

    assert entrance_exam.has_entrance_exam(admin_session, applicant=applicant) is True
    assert entrance_exam.resolve_bank_id(admin_session, applicant=applicant) == bank.id
    assert entrance_exam.time_limit_minutes(admin_session, applicant=applicant) == 30
    res = entrance_exam.grade_and_record(
        admin_session, tenant_id=tenant_a.id, applicant=applicant, answers={"q1": ["A"]}
    )
    assert "numeracy" in res["profile"]
    admin_session.rollback()


def test_cohort_bank_overrides_tenant_default(admin_session, tenant_a):
    cohort_bank = _bank_with_questions(admin_session, tenant_a)
    default_bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank=cohort_bank))
    entrance_exam.set_academy_defaults(admin_session, tenant_id=tenant_a.id, bank_id=default_bank.id)
    admin_session.flush()
    assert entrance_exam.resolve_bank_id(admin_session, applicant=applicant) == cohort_bank.id
    admin_session.rollback()


# --- validity gate ---------------------------------------------------------
# A sitting that fails these carries NO SIGNAL. It is an absence of data, not a
# weak candidate — scoring it as real pollutes the ranking and the talent pool.


def test_near_chance_score_is_flagged_invalid(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(minutes=20)
    applicant.assessment_elapsed_seconds = 20 * 60
    # 1 of 4 = 0.25, at the guessing baseline for a 4-option MCQ
    result = entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"], "q2": ["B"], "q3": ["B"], "q4": ["B"]},
    )
    assert result["valid"] is False
    assert result["invalid_reason"] == entrance_exam.INVALID_NEAR_CHANCE
    assert applicant.assessment_valid is False
    admin_session.rollback()


def test_too_fast_submission_is_flagged_invalid(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(seconds=30)  # click-through
    applicant.assessment_elapsed_seconds = 30
    result = entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["A"]},  # a good score...
    )
    assert result["valid"] is False  # ...but nobody engaged in 30s
    assert result["invalid_reason"] == entrance_exam.INVALID_TOO_FAST
    admin_session.rollback()


def test_genuine_sitting_is_valid(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(minutes=18)
    applicant.assessment_elapsed_seconds = 18 * 60
    result = entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["B"]},
    )
    assert result["valid"] is True
    assert result["invalid_reason"] is None
    admin_session.rollback()


# --- autosave / resume / reset --------------------------------------------
# The drop-recovery path: without these, a network blip costs a good candidate
# their one attempt, permanently.


def test_autosave_survives_and_prefills_a_resumed_sitting(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    entrance_exam.save_answers(admin_session, applicant=applicant, answers={"q1": ["A"], "q2": []})
    assert applicant.assessment_answers == {"q1": ["A"]}  # empties dropped
    # ...connection dies, candidate re-opens: the saved answer is still there
    entrance_exam.save_answers(admin_session, applicant=applicant, answers={"q1": ["A"], "q3": ["B"]})
    assert applicant.assessment_answers == {"q1": ["A"], "q3": ["B"]}
    admin_session.rollback()


def test_autosave_is_ignored_after_grading(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(minutes=15)
    applicant.assessment_elapsed_seconds = 15 * 60
    entrance_exam.grade_and_record(
        admin_session,
        tenant_id=tenant_a.id,
        applicant=applicant,
        answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["B"]},
    )
    entrance_exam.save_answers(admin_session, applicant=applicant, answers={"q1": ["B"]})
    assert applicant.assessment_answers is None  # a graded sitting is closed
    admin_session.rollback()


def test_reset_reopens_a_lost_sitting(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    # candidate opened it, the clock ran down, they never got to submit
    entrance_exam.start_exam(admin_session, applicant=applicant)
    entrance_exam.save_answers(admin_session, applicant=applicant, answers={"q1": ["A"]})
    assert applicant.assessment_started_at is not None

    raw = entrance_exam.reset_exam(admin_session, applicant=applicant)

    assert applicant.assessment_started_at is None  # clock reset
    assert applicant.assessment_elapsed_seconds == 0
    assert applicant.assessment_answers is None
    assert applicant.assessment_taken_at is None  # they can sit it again
    assert applicant.assessment_reset_count == 1  # audited
    assert (
        entrance_exam.applicant_for_token(admin_session, tenant_id=tenant_a.id, raw=raw).id == applicant.id
    )  # fresh link works
    admin_session.rollback()


# --- option shuffling (anti-leak) -----------------------------------------


def test_options_shuffle_per_applicant_but_are_stable(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    cohort = _cohort(admin_session, tenant_a, bank)
    a1 = _applicant(admin_session, tenant_a, cohort)
    a2 = _applicant(admin_session, tenant_a, cohort)
    q = admin_session.query(Question).filter(Question.bank_id == bank.id).first()

    # stable for the same applicant — a reload/resume must not reorder the options,
    # or autosaved answers would line up against the wrong ones
    assert entrance_exam.options_for(a1, q) == entrance_exam.options_for(a1, q)
    # and every option survives the shuffle
    assert sorted(entrance_exam.options_for(a1, q)) == sorted(q.options)
    assert sorted(entrance_exam.options_for(a2, q)) == sorted(q.options)
    admin_session.rollback()


# --- the applicant flow: invitation, deadline, evaluable profile ------------
# /apply used to send NO email and render the exam link once on-screen. Close the
# tab and the token was gone forever — which is how 180 applicants produced zero
# sittings. These cover the wiring that fixes it.


def test_invite_mints_a_working_link_and_sets_a_deadline(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))

    res = entrance_exam.invite(
        admin_session,
        applicant=applicant,
        base_url="https://academy.example",
        deadline_days=7,
        send=False,  # don't touch SMTP in a test
    )

    assert res["url"].startswith("https://academy.example/apply/assessment?token=")
    assert applicant.assessment_deadline is not None
    # the SAME token backs the emailed link and the on-screen one
    found = entrance_exam.applicant_for_token(admin_session, tenant_id=tenant_a.id, raw=res["token"])
    assert found.id == applicant.id
    admin_session.rollback()


def test_past_deadline_closes_the_exam(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    assert entrance_exam.past_deadline(applicant) is False  # no deadline = open

    applicant.assessment_deadline = datetime.now(UTC) - timedelta(hours=1)
    assert entrance_exam.past_deadline(applicant) is True

    applicant.assessment_deadline = datetime.now(UTC) + timedelta(days=3)
    assert entrance_exam.past_deadline(applicant) is False
    admin_session.rollback()


def test_reset_clears_the_deadline_so_a_reinvite_reopens_it(admin_session, tenant_a):
    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))
    entrance_exam.invite(admin_session, applicant=applicant, base_url="https://x.example", send=False)
    applicant.assessment_deadline = datetime.now(UTC) - timedelta(days=1)  # they missed it

    entrance_exam.reset_exam(admin_session, applicant=applicant)

    assert applicant.assessment_deadline is None
    assert entrance_exam.past_deadline(applicant) is False  # reopened
    admin_session.rollback()


def test_profile_completeness_is_reported(admin_session, tenant_a):
    from datetime import date as _date

    bank = _bank_with_questions(admin_session, tenant_a)
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a, bank))

    assert applicant.profile_complete is False
    assert "date_of_birth" in applicant.missing_profile_fields  # can't evaluate this yet

    applicant.date_of_birth = _date(1998, 5, 1)
    applicant.state = "Lagos"
    applicant.city = "Ikeja"
    applicant.highest_qualification = "OND"
    applicant.years_experience = 2
    applicant.has_device = True
    applicant.has_internet = True
    admin_session.flush()

    assert applicant.profile_complete is True
    assert applicant.missing_profile_fields == []
    admin_session.rollback()


def test_reapplying_blank_does_not_wipe_a_supplied_profile(admin_session, tenant_a):
    a = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email=f"p{uuid.uuid4().hex[:6]}@x.ex",
        first_name="A",
        last_name="B",
        profile={"state": "Lagos", "years_experience": 3, "date_of_birth": date(1997, 1, 1)},
    )
    assert a.state == "Lagos" and a.years_experience == 3

    # they re-apply and leave the optional fields blank — that must not erase what
    # they already told us
    again = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email=a.email,
        first_name="A",
        last_name="B",
        profile={"state": None, "years_experience": None, "city": "Ikeja"},
    )
    assert again.id == a.id
    assert again.state == "Lagos"  # preserved
    assert again.years_experience == 3  # preserved
    assert again.city == "Ikeja"  # newly supplied
    admin_session.rollback()


# --- stratified sampling ---------------------------------------------------


class _Q:
    def __init__(self, ext_id, category):
        self.ext_id = ext_id
        self.category = category
        self.options = ["a", "b", "c", "d"]


class _A:
    def __init__(self, id_):
        self.id = id_


def _bank(per_cat=6, cats=("numeracy", "safety", "reading")):
    return [_Q(f"{c}-{i}", c) for c in cats for i in range(per_cat)]


def test_sample_for_zero_returns_the_whole_bank():
    """The pre-sampling behaviour has to survive untouched."""
    from app.services.entrance_exam import sample_for

    bank = _bank()
    assert len(sample_for(_A("app-1"), bank, 0)) == len(bank)


def test_sample_for_is_stratified_by_category():
    """Every candidate must be measured on the same shape, or profiles differ."""
    from app.services.entrance_exam import sample_for

    bank = _bank(per_cat=6)
    for applicant in ("a", "b", "c", "d", "e"):
        got = sample_for(_A(applicant), bank, 2)
        counts: dict[str, int] = {}
        for q in got:
            counts[q.category] = counts.get(q.category, 0) + 1
        assert counts == {"numeracy": 2, "safety": 2, "reading": 2}


def test_sample_for_is_deterministic_per_applicant():
    """A reload or resumed sitting must not redraw the paper."""
    from app.services.entrance_exam import sample_for

    bank = _bank()
    first = [q.ext_id for q in sample_for(_A("app-1"), bank, 3)]
    again = [q.ext_id for q in sample_for(_A("app-1"), bank, 3)]
    assert first == again


def test_sample_for_differs_between_applicants():
    """Otherwise the bank leaks exactly as a fixed paper does."""
    from app.services.entrance_exam import sample_for

    bank = _bank(per_cat=8)
    a = {q.ext_id for q in sample_for(_A("app-1"), bank, 3)}
    b = {q.ext_id for q in sample_for(_A("app-2"), bank, 3)}
    assert a != b


def test_sample_for_takes_all_of_a_short_category():
    """A category smaller than the draw must not silently vanish from the profile."""
    from app.services.entrance_exam import sample_for

    bank = [*_bank(per_cat=6), _Q("tech-0", "technical")]
    got = sample_for(_A("app-1"), bank, 4)
    assert sum(1 for q in got if q.category == "technical") == 1


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import settings_store

    original = settings_store.effective

    overrides = {
        "learning_missing_question_weight": 1,
        "learning_default_extra_attempts": 1,
        "learning_default_course_status": "published",
        "learning_default_course_listed": False,
        "learning_exam_seconds_per_question": 12.0,
        "learning_exam_near_chance_margin": 1.33,
        "learning_exam_fallback_score_floor": 1.0 / 3.0,
        "learning_exam_fallback_duration_seconds": 360.0,
    }

    def effective(db, *, require_store=False):
        if require_store:
            # A real EffectiveSettings (attribute access, other policies intact)
            # with only the learning characterization keys replaced.
            return settings_store.EffectiveSettings({**vars(original(None)), **overrides})
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
