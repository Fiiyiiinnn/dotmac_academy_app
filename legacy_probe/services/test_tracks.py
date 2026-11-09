from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
from sqlalchemy import select

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.models.track import CohortTrack, Track, TrackCourse
from app.services import tracks as track_svc
from app.services.entitlements import accessible_course_ids
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.tracks import (
    add_courses_to_cohort_track,
    clear_enrollment_track,
    create_cohort_track,
    list_cohort_tracks,
    reorder_track_courses,
)
from legacy_probe.conftest import make_person_party


def _course(db, tenant, slug, title):
    course = Course(
        tenant_id=tenant.id,
        slug=slug,
        title=title,
        discipline="networking",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    db.add(course)
    db.flush()
    return course

def test_track_assignment_restricts_access_within_cohort(admin_session, tenant_a):
    cohort = Cohort(tenant_id=tenant_a.id, name="July Intake", discipline="networking", status="active")
    learner = make_person_party(admin_session, tenant_id=tenant_a.id, email="track-learner@a.edu", first_name="Track", last_name="Learner")
    course_a = _course(admin_session, tenant_a, "fiber-a", "Fiber A")
    course_b = _course(admin_session, tenant_a, "fiber-b", "Fiber B")
    admin_session.add_all([cohort, learner])
    admin_session.flush()

    track_a = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Fiber Track", course_ids=[course_a.id]
    )
    track_b = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Support Track", course_ids=[course_b.id]
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            person_id=learner.id,
            track_id=track_a.id,
            role_in_cohort="student",
            status="active",
        )
    )
    admin_session.commit()

    ids = accessible_course_ids(admin_session, tenant_id=tenant_a.id, person_id=learner.id)

    assert course_a.id in ids
    assert course_b.id not in ids
    assert admin_session.scalars(
        select(CourseOffering)
        .where(CourseOffering.tenant_id == tenant_a.id)
        .where(CourseOffering.cohort_id == cohort.id)
    ).all()
    assert track_b.id != track_a.id

def test_null_track_enrollment_keeps_existing_cohort_wide_access(admin_session, tenant_a):
    cohort = Cohort(tenant_id=tenant_a.id, name="Legacy Intake", discipline="networking", status="active")
    learner = make_person_party(admin_session, tenant_id=tenant_a.id, email="legacy-learner@a.edu", first_name="Legacy", last_name="Learner")
    course_a = _course(admin_session, tenant_a, "legacy-a", "Legacy A")
    course_b = _course(admin_session, tenant_a, "legacy-b", "Legacy B")
    admin_session.add_all([cohort, learner])
    admin_session.flush()
    admin_session.add_all(
        [
            CourseOffering(tenant_id=tenant_a.id, cohort_id=cohort.id, course_id=course_a.id, status="active"),
            CourseOffering(tenant_id=tenant_a.id, cohort_id=cohort.id, course_id=course_b.id, status="active"),
            Enrollment(
                tenant_id=tenant_a.id,
                cohort_id=cohort.id,
                person_id=learner.id,
                role_in_cohort="student",
                status="active",
            ),
        ]
    )
    admin_session.commit()

    ids = accessible_course_ids(admin_session, tenant_id=tenant_a.id, person_id=learner.id)

    assert ids == {course_a.id, course_b.id}

def test_create_cohort_track_links_courses_and_offerings(admin_session, tenant_a):
    cohort = Cohort(tenant_id=tenant_a.id, name="Build Intake", discipline="networking", status="active")
    course = _course(admin_session, tenant_a, "build-course", "Build Course")
    admin_session.add(cohort)
    admin_session.flush()

    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Build Track", course_ids=[course.id]
    )
    admin_session.commit()

    assert admin_session.scalars(select(Track).where(Track.id == track.id)).first() is not None
    assert admin_session.scalars(select(CohortTrack).where(CohortTrack.track_id == track.id)).first() is not None
    assert (
        admin_session.scalars(select(TrackCourse).where(TrackCourse.track_id == track.id)).first().course_id
        == course.id
    )
    assert (
        admin_session.scalars(select(CourseOffering).where(CourseOffering.course_id == course.id)).first() is not None
    )

def test_create_cohort_track_orders_courses_by_explicit_position(admin_session, tenant_a):
    """1a: a position hint controls order_index; ties break by submission
    order, and unpositioned courses append after all positioned ones."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Order Intake", discipline="networking", status="active")
    course_a = _course(admin_session, tenant_a, "order-a", "Order A")
    course_b = _course(admin_session, tenant_a, "order-b", "Order B")
    course_c = _course(admin_session, tenant_a, "order-c", "Order C")  # no position hint
    admin_session.add(cohort)
    admin_session.flush()

    # Submitted in alphabetical (a, b, c) order but B should come first.
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        name="Ordered Track",
        course_ids=[course_a.id, course_b.id, course_c.id],
        positions={course_a.id: 2, course_b.id: 1},
    )
    admin_session.commit()

    rows = admin_session.scalars(
        select(TrackCourse).where(TrackCourse.track_id == track.id).order_by(TrackCourse.order_index)
    ).all()
    assert [row.course_id for row in rows] == [course_b.id, course_a.id, course_c.id]
    assert [row.order_index for row in rows] == [1, 2, 3]  # dense, never the raw client value

def test_add_courses_to_cohort_track_orders_new_courses_by_position(admin_session, tenant_a):
    """1a: add-courses to an existing track also honors position hints for the
    newly-added batch, appended after the track's existing membership."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Add Intake", discipline="networking", status="active")
    first = _course(admin_session, tenant_a, "add-first", "Add First")
    new_a = _course(admin_session, tenant_a, "add-new-a", "Add New A")
    new_b = _course(admin_session, tenant_a, "add-new-b", "Add New B")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Add Track", course_ids=[first.id]
    )
    admin_session.flush()

    add_courses_to_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        track_id=track.id,
        course_ids=[new_a.id, new_b.id],
        positions={new_b.id: 1},
    )
    admin_session.commit()

    rows = admin_session.scalars(
        select(TrackCourse).where(TrackCourse.track_id == track.id).order_by(TrackCourse.order_index)
    ).all()
    assert [row.course_id for row in rows] == [first.id, new_b.id, new_a.id]
    assert [row.order_index for row in rows] == [1, 2, 3]

def test_reorder_track_courses_reassigns_dense_order(admin_session, tenant_a):
    """1b: an admin/instructor can reorder an EXISTING track's courses, not
    just at creation."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Reorder Intake", discipline="networking", status="active")
    course_a = _course(admin_session, tenant_a, "reorder-a", "Reorder A")
    course_b = _course(admin_session, tenant_a, "reorder-b", "Reorder B")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        name="Reorder Track",
        course_ids=[course_a.id, course_b.id],
    )
    admin_session.commit()

    reorder_track_courses(
        admin_session, tenant_id=tenant_a.id, track_id=track.id, ordered_course_ids=[course_b.id, course_a.id]
    )
    admin_session.commit()

    rows = admin_session.scalars(
        select(TrackCourse).where(TrackCourse.track_id == track.id).order_by(TrackCourse.order_index)
    ).all()
    assert [row.course_id for row in rows] == [course_b.id, course_a.id]
    assert [row.order_index for row in rows] == [1, 2]

def test_reorder_track_courses_rejects_a_course_not_in_the_track(admin_session, tenant_a):
    """1b: reorder validates the given id set matches current membership
    exactly — a course that isn't a track member is rejected."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Reorder Reject", discipline="networking", status="active")
    course_a = _course(admin_session, tenant_a, "reject-a", "Reject A")
    stranger = _course(admin_session, tenant_a, "reject-stranger", "Reject Stranger")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Reject Track", course_ids=[course_a.id]
    )
    admin_session.commit()

    with pytest.raises(BadRequestError):
        reorder_track_courses(admin_session, tenant_id=tenant_a.id, track_id=track.id, ordered_course_ids=[stranger.id])

    with pytest.raises(BadRequestError):
        # A subset (missing course_a) is also rejected — must match exactly.
        reorder_track_courses(admin_session, tenant_id=tenant_a.id, track_id=track.id, ordered_course_ids=[])

def test_reorder_track_courses_rejects_duplicate_ids(admin_session, tenant_a):
    cohort = Cohort(tenant_id=tenant_a.id, name="Reorder Dup", discipline="networking", status="active")
    course_a = _course(admin_session, tenant_a, "dup-a", "Dup A")
    course_b = _course(admin_session, tenant_a, "dup-b", "Dup B")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        name="Dup Track",
        course_ids=[course_a.id, course_b.id],
    )
    admin_session.commit()

    with pytest.raises(BadRequestError):
        reorder_track_courses(
            admin_session,
            tenant_id=tenant_a.id,
            track_id=track.id,
            ordered_course_ids=[course_a.id, course_a.id],
        )

def test_reorder_track_courses_unknown_track_raises_not_found(admin_session, tenant_a):
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        reorder_track_courses(admin_session, tenant_id=tenant_a.id, track_id=uuid4(), ordered_course_ids=[])

def test_list_cohort_tracks_flags_zero_activity_courses_live(admin_session, tenant_a):
    """1c: a zero-Activity course is flagged as a warning, computed live at
    render time — not just once at track-creation time — so it also catches
    a course that had its activities removed after the track was built."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Activity Intake", discipline="networking", status="active")
    with_activity = _course(admin_session, tenant_a, "has-activity", "Has Activity")
    without_activity = _course(admin_session, tenant_a, "no-activity", "No Activity")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        name="Activity Track",
        course_ids=[with_activity.id, without_activity.id],
    )
    admin_session.add(
        Activity(
            tenant_id=tenant_a.id,
            course_id=with_activity.id,
            chapter_number=1,
            type="mcq_test",
            title="Quiz",
            pass_threshold=0.6, grading="auto", weight=1.0,
        )
    )
    admin_session.commit()

    rows = list_cohort_tracks(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id)
    item = next(row for row in rows if row["track"].id == track.id)
    assert item["zero_activity_course_ids"] == {without_activity.id}

def test_clear_enrollment_track_nulls_track_id_without_fabricating_completion(admin_session, tenant_a):
    """1d: recovery from a stuck track nulls Enrollment.track_id — it must
    NOT fabricate a CourseCompletion (that would trigger real certificate
    issuance for a course the learner never actually finished)."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Clear Intake", discipline="networking", status="active")
    learner = make_person_party(admin_session, tenant_id=tenant_a.id, email="clear-learner@a.edu", first_name="Clear", last_name="Learner")
    course = _course(admin_session, tenant_a, "clear-course", "Clear Course")
    admin_session.add_all([cohort, learner])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Clear Track", course_ids=[course.id]
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            person_id=learner.id,
            track_id=track.id,
            role_in_cohort="student",
            status="active",
        )
    )
    admin_session.commit()

    from app.models.completion import CourseCompletion

    enrollment = clear_enrollment_track(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=learner.id)
    admin_session.commit()

    assert enrollment.track_id is None
    assert admin_session.scalar(select(CourseCompletion).where(CourseCompletion.person_id == learner.id)) is None
    # Falls back to full cohort-wide access via the null-track code path.
    ids = accessible_course_ids(admin_session, tenant_id=tenant_a.id, person_id=learner.id)
    assert course.id in ids

def test_clear_enrollment_track_raises_not_found_for_missing_enrollment(admin_session, tenant_a):
    cohort = Cohort(tenant_id=tenant_a.id, name="Clear Missing", discipline="networking", status="active")
    learner = make_person_party(admin_session, tenant_id=tenant_a.id, email="clear-missing@a.edu", first_name="Clear", last_name="Missing")
    admin_session.add_all([cohort, learner])
    admin_session.commit()

    with pytest.raises(NotFoundError):
        clear_enrollment_track(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=learner.id)

@pytest.mark.parametrize(
    ("writer", "command_type"),
    [
        ("create_cohort_track", learning.CreateTrackCommand),
        ("add_courses_to_cohort_track", learning.AddTrackCoursesCommand),
        ("reorder_track_courses", learning.ReorderTrackCoursesCommand),
    ],
)
def test_track_writer_public_boundary(monkeypatch, writer, command_type):
    """Boundary-only spy: no claim about database flushes or transaction authority."""
    db = object()
    tenant_id, cohort_id, track_id, first, second = (uuid4() for _ in range(5))
    submitted = [second, first, second]
    positions = {first: 7, second: 7}
    now = datetime.now(UTC)
    fact = learning.TrackFact(
        id=track_id,
        tenant_id=tenant_id,
        slug="boundary-track",
        name="Boundary Track",
        status="active",
        created_at=now,
        updated_at=now,
    )
    captured = []

    def owner(session, command):
        assert session is db
        assert type(command) is command_type
        assert command.tenant_id == tenant_id
        if writer == "reorder_track_courses":
            assert command.track_id == track_id
            assert command.ordered_course_ids == (second, first, second)
        else:
            assert command.cohort_id == cohort_id
            assert command.course_ids == (second, first, second)
            assert command.positions == {first: 7, second: 7}
            assert command.pacing_default_open is True
            if writer == "create_cohort_track":
                assert command.name == "  Boundary Track  "
                assert command.gating_mode == "previous"
            else:
                assert command.track_id == track_id
        captured.append(command)
        return fact

    from app.services.learning_policy import LearningCreationDefaults

    monkeypatch.setattr(
        track_svc,
        "creation_defaults",
        lambda session: LearningCreationDefaults(
            prerequisite_default_kind="course_completed",
            prerequisite_default_composition="all",
            track_default_gating_mode="previous",
            completion_fraction=1.0,
            certificate_auto_issue=True,
            certificate_validity_days=None,
            activity_scoring_policy="best",
            attempt_cooldown_seconds=0,
            session_promotes_delivery_mode=True,
            pacing_default_open=True,
        ),
    )
    monkeypatch.setattr(learning, writer, owner)
    kwargs = {"tenant_id": tenant_id}
    if writer == "reorder_track_courses":
        kwargs.update(track_id=track_id, ordered_course_ids=submitted)
    else:
        kwargs.update(cohort_id=cohort_id, course_ids=submitted, positions=positions)
        if writer == "create_cohort_track":
            kwargs["name"] = "  Boundary Track  "
        else:
            kwargs["track_id"] = track_id
    result = getattr(track_svc, writer)(db, **kwargs)
    assert result is fact
    assert (
        result.id,
        result.tenant_id,
        result.slug,
        result.name,
        result.status,
        result.created_at,
        result.updated_at,
    ) == (track_id, tenant_id, "boundary-track", "Boundary Track", "active", now, now)
    assert submitted == [second, first, second]
    assert positions == {first: 7, second: 7}
    submitted.clear()
    positions.clear()
    if writer == "reorder_track_courses":
        assert captured[0].ordered_course_ids == (second, first, second)
    else:
        assert captured[0].course_ids == (second, first, second)
        assert captured[0].positions == {first: 7, second: 7}

@pytest.mark.parametrize("writer", ["create_cohort_track", "add_courses_to_cohort_track", "reorder_track_courses"])
@pytest.mark.parametrize(
    ("source_type", "target_type"),
    [
        (learning.ParticipationBadRequest, BadRequestError),
        (learning.ParticipationNotFound, NotFoundError),
        (RuntimeError, RuntimeError),
    ],
)
def test_track_writer_error_boundary(monkeypatch, writer, source_type, target_type):
    db = object()
    tenant_id, cohort_id, track_id = (uuid4() for _ in range(3))
    error = source_type("owner detail")

    def owner(session, command):
        assert session is db
        assert command.tenant_id == tenant_id
        if writer == "create_cohort_track":
            assert command.name == ""
        raise error

    from app.services.learning_policy import LearningCreationDefaults

    monkeypatch.setattr(
        track_svc,
        "creation_defaults",
        lambda session: LearningCreationDefaults(
            prerequisite_default_kind="course_completed",
            prerequisite_default_composition="all",
            track_default_gating_mode="previous",
            completion_fraction=1.0,
            certificate_auto_issue=True,
            certificate_validity_days=None,
            activity_scoring_policy="best",
            attempt_cooldown_seconds=0,
            session_promotes_delivery_mode=True,
            pacing_default_open=True,
        ),
    )
    monkeypatch.setattr(learning, writer, owner)
    kwargs = {"tenant_id": tenant_id}
    if writer == "reorder_track_courses":
        kwargs.update(track_id=track_id, ordered_course_ids=[])
    else:
        kwargs.update(cohort_id=cohort_id, course_ids=[], positions=None)
        if writer == "create_cohort_track":
            kwargs["name"] = None
        else:
            kwargs["track_id"] = track_id
    with pytest.raises(target_type, match="^owner detail$") as caught:
        getattr(track_svc, writer)(db, **kwargs)
    if source_type is RuntimeError:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error

def test_track_writer_validation_priority(admin_session, tenant_a):
    """Hosted DB canary; existing legacy fixtures still require canonical adoption."""
    missing_cohort, missing_track, course_id = (uuid4() for _ in range(3))
    with pytest.raises(NotFoundError, match="^cohort not found for tenant$"):
        create_cohort_track(
            admin_session,
            tenant_id=tenant_a.id,
            cohort_id=missing_cohort,
            name="",
            course_ids=[],
        )
    with pytest.raises(NotFoundError, match="^track not found for cohort$"):
        add_courses_to_cohort_track(
            admin_session,
            tenant_id=tenant_a.id,
            cohort_id=missing_cohort,
            track_id=missing_track,
            course_ids=[],
        )
    with pytest.raises(NotFoundError, match="^track not found$"):
        reorder_track_courses(
            admin_session,
            tenant_id=tenant_a.id,
            track_id=missing_track,
            ordered_course_ids=[course_id, course_id],
        )

def test_track_writer_duplicate_idempotence_and_caller_transaction(admin_session, tenant_a):
    """Hosted DB canary, pending canonical fixture adoption; never a mock DB proof."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Duplicate Intake", discipline="networking", status="active")
    first = _course(admin_session, tenant_a, "writer-first", "Writer First")
    second = _course(admin_session, tenant_a, "writer-second", "Writer Second")
    admin_session.add(cohort)
    admin_session.flush()
    root = admin_session.get_transaction()
    nested = admin_session.get_nested_transaction()
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        name="  Duplicate Track  ",
        course_ids=[first.id, second.id, first.id],
        positions={first.id: 7, second.id: 7},
    )
    added = add_courses_to_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        track_id=track.id,
        course_ids=[second.id, first.id, second.id],
        positions={second.id: 0},
    )
    reordered = reorder_track_courses(
        admin_session,
        tenant_id=tenant_a.id,
        track_id=track.id,
        ordered_course_ids=[second.id, first.id],
    )
    assert isinstance(track, learning.TrackFact)
    assert track.name == "Duplicate Track"
    assert track.slug == "duplicate-track"
    assert track.status == "active"
    assert added.id == reordered.id == track.id
    assert admin_session.get_transaction() is root
    assert admin_session.get_nested_transaction() is nested
    rows = admin_session.scalars(
        select(TrackCourse).where(TrackCourse.track_id == track.id).order_by(TrackCourse.order_index)
    ).all()
    assert [row.course_id for row in rows] == [second.id, first.id]
    assert [row.order_index for row in rows] == [1, 2]
    offerings = admin_session.scalars(select(CourseOffering).where(CourseOffering.cohort_id == cohort.id)).all()
    assert {row.course_id for row in offerings} == {first.id, second.id}
    assert len(offerings) == 2
    admin_session.rollback()
    assert admin_session.scalar(select(Track).where(Track.id == track.id)) is None

def _track_read_facts(tenant_id, cohort_id):
    """Real public value constructors; this helper performs no database work."""
    now = datetime.now(UTC)
    track = learning.TrackFact(
        id=uuid4(),
        tenant_id=tenant_id,
        slug="read-track",
        name="Read Track",
        status="inactive",
        created_at=now,
        updated_at=now,
    )
    cohort_track = learning.CohortTrackFact(
        id=uuid4(),
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        track_id=track.id,
        status="active",
        created_at=now,
        updated_at=now,
    )
    courses = tuple(
        learning.CourseFact(
            id=uuid4(),
            tenant_id=tenant_id,
            slug=slug,
            title=title,
            discipline="networking",
            source_ref="boundary",
            version=1,
            status=status,
            listed=True,
            created_at=now,
            updated_at=now,
        )
        for slug, title, status in (("z-draft", "Z Draft", "draft"), ("a-course", "A Course", "published"))
    )
    return learning.CohortTrackItem(
        cohort_track=cohort_track,
        track=track,
        courses=courses,
        zero_activity_course_ids=frozenset({courses[0].id}),
    )

def test_list_cohort_tracks_public_read_boundary(monkeypatch):
    """Only container adaptation; domain ordering/status/warnings stay with Learning."""
    db = object()
    tenant_id, cohort_id = uuid4(), uuid4()
    item = _track_read_facts(tenant_id, cohort_id)
    calls = []

    def owner(session, *, tenant_id, cohort_id):
        calls.append((session, tenant_id, cohort_id))
        return (item,)

    monkeypatch.setattr(learning, "list_cohort_tracks", owner)
    rows = track_svc.list_cohort_tracks(db, tenant_id=tenant_id, cohort_id=cohort_id)
    assert calls == [(db, tenant_id, cohort_id)]
    assert calls[0][0] is db
    assert type(rows) is list
    assert len(rows) == 1
    row = rows[0]
    assert type(row) is dict
    assert set(row) == {"cohort_track", "track", "courses", "zero_activity_course_ids"}
    assert row["cohort_track"] is item.cohort_track
    assert row["track"] is item.track
    assert type(row["courses"]) is list
    assert row["courses"] == list(item.courses)
    assert row["courses"][0] is item.courses[0]
    assert row["track"].status == "inactive"
    assert [course.status for course in row["courses"]] == ["draft", "published"]
    assert type(row["zero_activity_course_ids"]) is set
    assert row["zero_activity_course_ids"] == {item.courses[0].id}
    with pytest.raises(FrozenInstanceError):
        row["track"].name = "changed"
    with pytest.raises(FrozenInstanceError):
        row["cohort_track"].status = "changed"
    with pytest.raises(FrozenInstanceError):
        row["courses"][0].title = "changed"
    row["courses"].clear()
    row["zero_activity_course_ids"].clear()
    assert len(item.courses) == 2
    assert item.zero_activity_course_ids == frozenset({item.courses[0].id})

def test_tracks_for_cohorts_public_read_boundary(monkeypatch):
    db = object()
    tenant_id, cohort_id, empty_cohort_id = uuid4(), uuid4(), uuid4()
    first = _track_read_facts(tenant_id, cohort_id).track
    second = _track_read_facts(tenant_id, cohort_id).track
    cohort_ids = [cohort_id, empty_cohort_id, cohort_id]
    owner_rows = {cohort_id: (second, first), empty_cohort_id: ()}
    calls = []

    def owner(session, *, tenant_id, cohort_ids):
        calls.append((session, tenant_id, cohort_ids))
        return owner_rows

    monkeypatch.setattr(learning, "tracks_for_cohorts", owner)
    grouped = track_svc.tracks_for_cohorts(db, tenant_id=tenant_id, cohort_ids=cohort_ids)
    assert calls == [(db, tenant_id, cohort_ids)]
    assert calls[0][0] is db
    assert calls[0][2] is cohort_ids
    assert type(grouped) is dict
    assert grouped == {cohort_id: [second, first], empty_cohort_id: []}
    assert all(type(value) is list for value in grouped.values())
    assert grouped[cohort_id][0] is second
    with pytest.raises(FrozenInstanceError):
        grouped[cohort_id][0].name = "changed"
    grouped[cohort_id].clear()
    assert owner_rows[cohort_id] == (second, first)
    assert cohort_ids == [cohort_id, empty_cohort_id, cohort_id]

@pytest.mark.parametrize("reader", ["list_cohort_tracks", "tracks_for_cohorts"])
def test_track_read_empty_boundary(monkeypatch, reader):
    db = object()
    tenant_id, cohort_id = uuid4(), uuid4()
    kwargs = {"tenant_id": tenant_id}
    kwargs.update({"cohort_id": cohort_id} if reader == "list_cohort_tracks" else {"cohort_ids": []})
    calls = []

    def owner(session, **arguments):
        calls.append((session, arguments))
        return () if reader == "list_cohort_tracks" else {}

    monkeypatch.setattr(learning, reader, owner)
    result = getattr(track_svc, reader)(db, **kwargs)
    assert calls == [(db, kwargs)]
    assert calls[0][0] is db
    assert result == ([] if reader == "list_cohort_tracks" else {})

@pytest.mark.parametrize(
    ("reader", "source_type", "target_type"),
    [
        ("list_cohort_tracks", learning.ParticipationNotFound, NotFoundError),
        ("list_cohort_tracks", RuntimeError, RuntimeError),
        ("tracks_for_cohorts", RuntimeError, RuntimeError),
    ],
)
def test_track_read_error_boundary(monkeypatch, reader, source_type, target_type):
    db = object()
    tenant_id, cohort_id = uuid4(), uuid4()
    detail = "cohort not found for tenant"
    error = source_type(detail)
    kwargs = {"tenant_id": tenant_id}
    kwargs.update({"cohort_id": cohort_id} if reader == "list_cohort_tracks" else {"cohort_ids": [cohort_id]})

    def owner(session, **arguments):
        assert session is db
        assert arguments == kwargs
        raise error

    monkeypatch.setattr(learning, reader, owner)
    with pytest.raises(target_type, match="^cohort not found for tenant$") as caught:
        getattr(track_svc, reader)(db, **kwargs)
    if source_type is RuntimeError:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error

_PACING_DELEGATED = {"ensure_track_offerings", "assign_enrollment_track"}


def _stub_pacing_policy(monkeypatch, db):
    """The facade reads the pacing default from the composed creation defaults on the caller session.

    Stub only that policy reader (product default True) so the plain ``object()`` session still proves
    every other call is a pure delegation, and return the extra owner kwarg the facade must pass.
    """
    seen = []

    def creation_defaults(session):
        assert session is db
        seen.append(session)
        return SimpleNamespace(pacing_default_open=True)

    monkeypatch.setattr(track_svc, "creation_defaults", creation_defaults)
    return seen


@pytest.mark.parametrize(
    "operation",
    ["cohort_track_or_404", "ensure_track_offerings", "assign_enrollment_track", "clear_enrollment_track"],
)
def test_track_lifecycle_public_boundary(monkeypatch, operation):
    """Value boundary only; domain mutations and transaction checks belong to Learning."""
    db = SimpleNamespace(get_transaction=lambda: SimpleNamespace(is_active=True))
    tenant_id, cohort_id, person_id, track_id = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    kwargs = {"tenant_id": tenant_id, "cohort_id": cohort_id}
    if operation != "clear_enrollment_track":
        kwargs["track_id"] = track_id
    if operation in {"assign_enrollment_track", "clear_enrollment_track"}:
        kwargs["person_id"] = person_id
        fact = learning.EnrollmentFact(
            id=uuid4(),
            tenant_id=tenant_id,
            cohort_id=cohort_id,
            person_id=person_id,
            track_id=track_id if operation == "assign_enrollment_track" else None,
            role_in_cohort="instructor",
            status="dropped",
            audience="staff",
            employee_ref="employee-boundary",
            access_ends_at=now,
            created_at=now,
            updated_at=now,
        )
    elif operation == "cohort_track_or_404":
        fact = learning.CohortTrackFact(
            id=uuid4(),
            tenant_id=tenant_id,
            cohort_id=cohort_id,
            track_id=track_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
    else:
        fact = None
    calls = []

    def owner(session, **arguments):
        calls.append((session, arguments))
        return fact

    monkeypatch.setattr(learning, operation, owner)
    _stub_pacing_policy(monkeypatch, db)
    expected = dict(kwargs, pacing_default_open=True) if operation in _PACING_DELEGATED else kwargs
    result = getattr(track_svc, operation)(db, **kwargs)
    assert calls == [(db, expected)]
    assert calls[0][0] is db
    assert result is fact
    if operation in {"assign_enrollment_track", "clear_enrollment_track"}:
        assert type(result) is learning.EnrollmentFact
        assert result.person_id == person_id
        assert result.track_id == (track_id if operation == "assign_enrollment_track" else None)
        assert (result.role_in_cohort, result.status, result.audience, result.employee_ref, result.access_ends_at) == (
            "instructor",
            "dropped",
            "staff",
            "employee-boundary",
            now,
        )
        with pytest.raises(FrozenInstanceError):
            result.track_id = uuid4()
    elif operation == "cohort_track_or_404":
        assert type(result) is learning.CohortTrackFact
        assert result.track_id == track_id
        with pytest.raises(FrozenInstanceError):
            result.status = "inactive"
    else:
        assert result is None

@pytest.mark.parametrize(
    "operation",
    ["cohort_track_or_404", "ensure_track_offerings", "assign_enrollment_track", "clear_enrollment_track"],
)
@pytest.mark.parametrize(
    ("source_type", "target_type"),
    [
        (learning.ParticipationNotFound, NotFoundError),
        (learning.ParticipationBadRequest, learning.ParticipationBadRequest),
        (learning.TransactionChanged, learning.TransactionChanged),
        (RuntimeError, RuntimeError),
    ],
)
def test_track_lifecycle_error_boundary(monkeypatch, operation, source_type, target_type):
    db = SimpleNamespace(get_transaction=lambda: SimpleNamespace(is_active=True))
    tenant_id, cohort_id, person_id, track_id = (uuid4() for _ in range(4))
    kwargs = {"tenant_id": tenant_id, "cohort_id": cohort_id}
    if operation != "clear_enrollment_track":
        kwargs["track_id"] = track_id
    if operation in {"assign_enrollment_track", "clear_enrollment_track"}:
        kwargs["person_id"] = person_id
    detail = (
        "track not found for cohort"
        if operation in {"cohort_track_or_404", "ensure_track_offerings"}
        else "enrollment not found"
    )
    error = source_type(detail)
    calls = []

    def owner(session, **arguments):
        calls.append((session, arguments))
        raise error

    monkeypatch.setattr(learning, operation, owner)
    _stub_pacing_policy(monkeypatch, db)
    expected = dict(kwargs, pacing_default_open=True) if operation in _PACING_DELEGATED else kwargs
    with pytest.raises(target_type, match=f"^{detail}$") as caught:
        getattr(track_svc, operation)(db, **kwargs)
    assert calls == [(db, expected)]
    assert calls[0][0] is db
    if source_type is learning.ParticipationNotFound:
        assert type(caught.value) is NotFoundError
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error

@pytest.mark.parametrize("operation", ["ensure_track_offerings", "assign_enrollment_track", "clear_enrollment_track"])
def test_track_lifecycle_requires_existing_root(admin_session, tenant_a, operation):
    """Hosted real-Session canary: refuse before any query can autobegin a root."""
    tenant_id = tenant_a.id
    kwargs = {"tenant_id": tenant_id, "cohort_id": uuid4()}
    if operation != "clear_enrollment_track":
        kwargs["track_id"] = uuid4()
    if operation != "ensure_track_offerings":
        kwargs["person_id"] = uuid4()
    admin_session.rollback()
    assert admin_session.get_transaction() is None
    with pytest.raises(learning.TransactionChanged, match="already active caller root transaction"):
        getattr(track_svc, operation)(admin_session, **kwargs)
    assert admin_session.get_transaction() is None

@pytest.mark.parametrize("operation", ["ensure_track_offerings", "assign_enrollment_track", "clear_enrollment_track"])
def test_track_lifecycle_preserves_caller_root_and_savepoint(admin_session, tenant_a, operation):
    """Hosted DB canary; canonical installed-model fixture adoption remains required."""
    cohort = Cohort(tenant_id=tenant_a.id, name="Lifecycle Intake", discipline="networking", status="active")
    learner = make_person_party(admin_session, tenant_id=tenant_a.id, email="lifecycle-learner@a.edu", first_name="Track", last_name="Lifecycle")
    course = _course(admin_session, tenant_a, "lifecycle-course", "Lifecycle Course")
    admin_session.add_all([cohort, learner])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Lifecycle Track", course_ids=[course.id]
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            person_id=learner.id,
            track_id=None if operation == "assign_enrollment_track" else track.id,
            role_in_cohort="instructor",
            status="dropped",
        )
    )
    admin_session.flush()
    offering = admin_session.scalar(
        select(CourseOffering)
        .where(CourseOffering.tenant_id == tenant_a.id)
        .where(CourseOffering.cohort_id == cohort.id)
        .where(CourseOffering.course_id == course.id)
    )
    assert offering is not None
    offering.status = "inactive"
    admin_session.flush()
    kwargs = {"tenant_id": tenant_a.id, "cohort_id": cohort.id}
    if operation != "clear_enrollment_track":
        kwargs["track_id"] = track.id
    if operation != "ensure_track_offerings":
        kwargs["person_id"] = learner.id
    root = admin_session.get_transaction()
    assert root is not None and root.is_active
    with admin_session.begin_nested():
        nested = admin_session.get_nested_transaction()
        result = getattr(track_svc, operation)(admin_session, **kwargs)
        assert admin_session.get_transaction() is root
        assert admin_session.get_nested_transaction() is nested
        assert admin_session.scalar(
            select(CourseOffering.status)
            .where(CourseOffering.tenant_id == tenant_a.id)
            .where(CourseOffering.cohort_id == cohort.id)
            .where(CourseOffering.course_id == course.id)
        ) == ("inactive" if operation == "clear_enrollment_track" else "active")
        if operation == "ensure_track_offerings":
            assert result is None
        else:
            assert result.track_id == (None if operation == "clear_enrollment_track" else track.id)
            assert result.role_in_cohort == "instructor"
            assert result.status == "dropped"
    assert admin_session.get_transaction() is root
