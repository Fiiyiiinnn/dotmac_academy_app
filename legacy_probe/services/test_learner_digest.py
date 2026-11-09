"""Weekly learner progress email — the message students have never received.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from dotmac_learning import LearnerWindowActivity
from sqlalchemy import select

from app.models.assessment import Activity, Question, QuestionBank
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.offering import CourseOffering
from app.services import learner_digest
from app.services.assessment import submit_activity
from legacy_probe.conftest import make_person_party


def _learner(db, tenant, email, *, first="Ada", prefs=None):
    p = make_person_party(db, tenant_id=tenant.id, email=email, first_name=first, last_name="Lovelace", status="active", prefs=prefs or {})
    db.add(p)
    db.flush()
    return p

def _row(db, person):
    """The product's learner row shape, as selected in learner_digest.send_weekly_digests."""
    return db.execute(
        select(Party.id, Party.email, PartyPerson.first_name, AcademyAccountProfile.prefs)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(Party.id == person.id)
    ).one()


def _live_course(db, tenant, person, *, slug, title="Network Foundation", activities=2):
    """A course the learner can actually study, with N passable activities."""
    course = Course(
        tenant_id=tenant.id,
        slug=slug,
        title=title,
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    cohort = Cohort(tenant_id=tenant.id, name=f"C-{slug}", discipline="networking", status="active")
    db.add_all([course, cohort])
    db.flush()
    db.add(
        Chapter(
            tenant_id=tenant.id,
            course_id=course.id,
            number=1,
            title="One",
            part="I",
            body_html="<p>x</p>",
            source_hash=f"h-{slug}",
            order_index=1,
        )
    )
    acts = []
    for n in range(activities):
        bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=n + 1, kind="chapter", version=1)
        db.add(bank)
        db.flush()
        db.add(
            Question(
                tenant_id=tenant.id,
                bank_id=bank.id,
                ext_id="q1",
                stem="Q?",
                type="single",
                options=["A", "B"],
                correct=["A"],
                rubric_category="recall",
                explanation="",
                weight=1,
            )
        )
        act = Activity(
            tenant_id=tenant.id,
            course_id=course.id,
            chapter_number=n + 1,
            type="mcq_test",
            bank_id=bank.id,
            title=f"Chapter {n + 1} test",
            pass_threshold=0.6, grading="auto", weight=1.0,
        )
        db.add(act)
        db.flush()
        acts.append(act)
    db.add(
        Enrollment(
            tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="active"
        )
    )
    db.add(CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id, course_id=course.id, status="active"))
    db.commit()
    return course, acts

def test_digest_reports_the_week_and_what_is_next(admin_session, tenant_a):
    now = datetime.now(UTC)
    person = _learner(admin_session, tenant_a, "ada@a.edu")
    _course, acts = _live_course(admin_session, tenant_a, person, slug="ld-week")
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=person.id, activity=acts[0], answers={"q1": ["A"]})
    admin_session.commit()

    digest = learner_digest.build_digest(
        admin_session,
        tenant_id=tenant_a.id,
        person=_row(admin_session, person),
        since=now - timedelta(days=7),
        now=now,
    )
    admin_session.rollback()

    assert digest is not None
    assert digest["name"] == "Ada"
    assert digest["activity"]["passed"] == 1
    assert "passed 1" in digest["headline"]
    line = digest["courses"][0]
    assert line["passed"] == 1 and line["total"] == 2
    assert line["pct"] == 50
    assert line["next"] == "Chapter 2 test"  # the unpassed one

def test_a_quiet_week_is_said_plainly_not_congratulated(admin_session, tenant_a):
    """An email that praises inactivity teaches people to ignore it."""
    now = datetime.now(UTC)
    person = _learner(admin_session, tenant_a, "quiet@a.edu")
    _live_course(admin_session, tenant_a, person, slug="ld-quiet")

    digest = learner_digest.build_digest(
        admin_session,
        tenant_id=tenant_a.id,
        person=_row(admin_session, person),
        since=now - timedelta(days=7),
        now=now,
    )
    admin_session.rollback()

    assert digest is not None  # a quiet week is exactly when it's worth sending
    assert "haven't studied this week" in digest["headline"]

def test_no_live_course_means_no_digest(admin_session, tenant_a):
    """Chasing someone with nothing to study is the inactivity nudge's job."""
    now = datetime.now(UTC)
    person = _learner(admin_session, tenant_a, "nocourse@a.edu")
    admin_session.commit()

    digest = learner_digest.build_digest(
        admin_session,
        tenant_id=tenant_a.id,
        person=_row(admin_session, person),
        since=now - timedelta(days=7),
        now=now,
    )
    admin_session.rollback()
    assert digest is None

def test_send_is_idempotent_within_an_iso_week(admin_session, tenant_a):
    """A retried or double-scheduled job must not double-send."""
    person = _learner(admin_session, tenant_a, "once@a.edu")
    _live_course(admin_session, tenant_a, person, slug="ld-once")

    first = learner_digest.send_weekly_digests(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io"
    )
    second = learner_digest.send_weekly_digests(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io"
    )
    admin_session.commit()
    mails = list(
        admin_session.scalars(
            select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id).where(EmailOutbox.kind == "learner_digest")
        )
    )
    admin_session.rollback()

    assert first["queued"] == 1
    assert second["queued"] == 0
    assert len(mails) == 1

def test_opt_out_is_honoured(admin_session, tenant_a):
    person = _learner(admin_session, tenant_a, "optout@a.edu", prefs={learner_digest.OPTOUT_KIND: False})
    _live_course(admin_session, tenant_a, person, slug="ld-optout")

    counts = learner_digest.send_weekly_digests(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io"
    )
    admin_session.rollback()
    assert counts["queued"] == 0
    assert counts["skipped_optout"] == 1

def test_instructors_do_not_receive_the_learner_digest(admin_session, tenant_a):
    person = _learner(admin_session, tenant_a, "teacher@a.edu")
    course = Course(
        tenant_id=tenant_a.id, slug="ld-teach", title="T", discipline="networking", source_ref="x", version=1,
        status="published", listed=False,
    )
    cohort = Cohort(tenant_id=tenant_a.id, name="C-teach", discipline="networking", status="active")
    admin_session.add_all([course, cohort])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            person_id=person.id,
            role_in_cohort="instructor",
            status="active",
        )
    )
    admin_session.add(CourseOffering(tenant_id=tenant_a.id, cohort_id=cohort.id, course_id=course.id, status="active"))
    admin_session.commit()

    counts = learner_digest.send_weekly_digests(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io"
    )
    admin_session.rollback()
    assert counts["queued"] == 0

def test_rendered_bodies_link_to_the_resume_point_and_escape_titles(admin_session, tenant_a):
    now = datetime.now(UTC)
    person = _learner(admin_session, tenant_a, "render@a.edu")
    _live_course(admin_session, tenant_a, person, slug="ld-render", title="<b>Nasty</b> Course")

    digest = learner_digest.build_digest(
        admin_session,
        tenant_id=tenant_a.id,
        person=_row(admin_session, person),
        since=now - timedelta(days=7),
        now=now,
    )
    html, text = learner_digest.render(digest, base_url="https://academy.dotmac.io/", branding="Dotmac Academy")
    admin_session.rollback()

    assert "/courses/ld-render/chapters/1" in html
    assert "/courses/ld-render/chapters/1" in text
    assert "//courses" not in text  # trailing slash on base_url not doubled
    assert "<b>Nasty</b>" not in html
    assert "&lt;b&gt;Nasty" in html
    assert "Account → Notifications" in text  # every send says how to stop it

@pytest.mark.parametrize("counts", [(0, 0, 0), (3, 7, 2)])
def test_week_activity_delegates_scope_and_returns_a_fresh_legacy_mapping(monkeypatch, counts):
    db = object()
    tenant_id, person_id = uuid4(), uuid4()
    since = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    observation = LearnerWindowActivity(*counts)
    calls = []

    def read_window(actual_db, *, tenant_id, person_id, since):
        calls.append((actual_db, tenant_id, person_id, since))
        return observation

    monkeypatch.setattr(learner_digest, "learner_window_activity", read_window)
    first = learner_digest.week_activity(db, tenant_id=tenant_id, person_id=person_id, since=since)
    assert first == dict(zip(("chapters", "submissions", "passed"), counts, strict=True))
    assert all(type(value) is int for value in first.values())
    first["chapters"] = 999
    second = learner_digest.week_activity(db, tenant_id=tenant_id, person_id=person_id, since=since)
    assert second == dict(zip(("chapters", "submissions", "passed"), counts, strict=True))
    assert first is not second
    assert len(calls) == 2
    for actual_db, actual_tenant, actual_person, actual_since in calls:
        assert actual_db is db
        assert actual_tenant is tenant_id
        assert actual_person is person_id
        assert actual_since is since

def test_week_activity_propagates_owner_failure(monkeypatch):
    failure = RuntimeError("window read failed")

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(learner_digest, "learner_window_activity", fail)
    with pytest.raises(RuntimeError) as caught:
        learner_digest.week_activity(object(), tenant_id=uuid4(), person_id=uuid4(), since=datetime.now(UTC))
    assert caught.value is failure

def test_build_and_render_consume_the_delegated_window_without_changing_payload(monkeypatch):
    db = object()
    tenant_id = uuid4()
    person = SimpleNamespace(id=uuid4(), first_name=" Ada ")
    now = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    since = now - timedelta(days=7)
    calls = []

    def cards(actual_db, *, tenant_id, person_id, now):
        calls.append(("cards", actual_db, tenant_id, person_id, now))
        return {
            "cards": [
                {
                    "state": "in_progress",
                    "course": SimpleNamespace(title="Network Course"),
                    "pct": 50,
                    "passed": 1,
                    "total": 2,
                    "next_activity": SimpleNamespace(title="Next chapter"),
                    "action": {"href": "/courses/network/chapters/2"},
                    "next_deadline": now,
                }
            ]
        }

    def read_window(actual_db, *, tenant_id, person_id, since):
        calls.append(("window", actual_db, tenant_id, person_id, since))
        return LearnerWindowActivity(3, 2, 1)

    monkeypatch.setattr(learner_digest.learner_dashboard, "course_cards", cards)
    monkeypatch.setattr(learner_digest, "learner_window_activity", read_window)
    digest = learner_digest.build_digest(db, tenant_id=tenant_id, person=person, since=since, now=now)
    assert calls == [("cards", db, tenant_id, person.id, now), ("window", db, tenant_id, person.id, since)]
    assert digest == {
        "name": "Ada",
        "headline": "You passed 1 piece(s) of work this week.",
        "activity": {"chapters": 3, "submissions": 2, "passed": 1},
        "courses": [
            {
                "title": "Network Course",
                "pct": 50,
                "passed": 1,
                "total": 2,
                "next": "Next chapter",
                "href": "/courses/network/chapters/2",
                "deadline": now,
            }
        ],
        "more_courses": 0,
    }
    html, text = learner_digest.render(digest, base_url="https://academy.example/", branding="Academy")
    assert "You passed 1 piece(s) of work this week." in html
    assert "You passed 1 piece(s) of work this week." in text
    assert "https://academy.example/courses/network/chapters/2" in html
    assert "https://academy.example/courses/network/chapters/2" in text
    assert "Next chapter" in html and "Next chapter" in text
