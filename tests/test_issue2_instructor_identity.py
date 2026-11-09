"""Maintained port of legacy_probe/web/test_instructor_canonical_identity.py.

DISPOSITIONS: test_instructor_roster_canonical_identity_only
(app/web/instructor.py over kernel Party/PartyPerson). Bodies are the
legacy route-query canaries verbatim; the genuine sign-in-backed
fixture chain (current_account, _nominate_cohort) comes from
tests/issue2_wave2_support.py and runs as INNER tests in a subprocess
bound to the per-test genesis database.
"""

from __future__ import annotations

import os

if os.getenv("WAVE2_INNER") != "1":
    from tests.issue2_wave2_support import assert_inner_passed, run_inner

    def test_instructor_canonical_identity_inner_suite(genesis_database):
        assert_inner_passed(run_inner(genesis_database, "tests/test_issue2_instructor_identity.py"))

else:
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from uuid import uuid4

    import pytest
    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role
    from fastapi import HTTPException
    from sqlalchemy import delete, select, update
    from starlette.requests import Request

    from app.models.assessment import Activity, Score, Submission
    from app.models.cohort import Cohort, Enrollment
    from app.models.course import Course
    from app.models.identity_extensions import AcademyAccountProfile
    from app.web import instructor
    from tests.issue2_wave2_support import (  # noqa: F401
        _nominate_cohort,
        admin_session,
        app_user_session,
        current_account,
        sign_in_account,
        tenant_a,
        tenant_b,
    )

    assert current_account
    assert tenant_b

    def _display(a, monkeypatch):
        monkeypatch.setattr(instructor.templates, "TemplateResponse", lambda request, name, context: context)
        monkeypatch.setattr(instructor.track_svc, "list_cohort_tracks", lambda *args, **kw: [])
        return Request({"type": "http", "headers": [], "state": {"tenant": a.tenant}})

    def _instructor_grant(a):
        role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "instructor")).first()
        if role is None:
            role = Role(tenant_id=a.tid, slug="instructor", name="Instructor")
            a.admin.add(role)
            a.admin.flush()
        a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))

    @pytest.mark.parametrize(
        "party_active,profile_status,enrollment_status,role",
        [
            (False, "active", "active", "student"),
            (True, "suspended", "dropped", "student"),
            (False, "suspended", "active", "instructor"),
        ],
    )
    def test_rosters_keep_all_statuses_raw_names_and_candidate_profile_only_admission(
        current_account, monkeypatch, party_active, profile_status, enrollment_status, role
    ):
        a = current_account
        request = _display(a, monkeypatch)
        _instructor_grant(a)
        email = f"instructor-display-{uuid4().hex}@a.edu"
        a.admin.execute(update(Party).where(Party.id == a.pid).values(email=email, is_active=party_active))
        a.admin.execute(
            update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name=" <Raw> ", last_name=" Names ")
        )
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status=profile_status)
        )
        cohort = Cohort(tenant_id=a.tid, name="Display", discipline="networking", status="inactive")
        spare = Cohort(tenant_id=a.tid, name="Display", discipline="networking", status="active")
        a.admin.add_all([cohort, spare])
        a.admin.flush()
        cid, spare_id = cohort.id, spare.id
        a.admin.add(
            Enrollment(tenant_id=a.tid, cohort_id=cid, person_id=a.pid, role_in_cohort=role, status=enrollment_status)
        )
        a.admin.commit()
        # Facilitators are active nominations (#188), not instructor enrollments.
        nominated = role == "instructor" and enrollment_status == "active"
        if nominated:
            _nominate_cohort(a, cid)
        context = instructor.cohorts_list(request, person=a.login_snapshot, db=a.admin)
        ours = next(row for row in context["cohort_rows"] if row["cohort"].id == cid)
        row = next(row for row in ours["roster"] if row["person"].id == a.pid)
        person = row["person"]
        assert (person.email, person.first_name, person.last_name, person.status) == (
            email,
            " <Raw> ",
            " Names ",
            profile_status,
        )
        assert person.first_name[:1] == " " and person.last_name[:1] == " "
        with pytest.raises((AttributeError, TypeError)):
            person.status = "changed"
        assert ours["active_student_count"] == int(
            role == "student" and enrollment_status == "active" and profile_status == "active"
        )
        assert (a.pid in {item["person"].id for item in ours["facilitators"]}) is nominated
        available = next(row for row in context["cohort_rows"] if row["cohort"].id == spare_id)[
            "available_facilitators"
        ]
        assert sum(p.id == a.pid for p in available) == int(profile_status == "active")
        detail = instructor.cohort_detail(cid, request, person=a.login_snapshot, db=a.admin, limit=1, offset=0)
        assert detail["student_total"] == int(role == "student")
        if role == "student":
            assert detail["students"][0]["person"] == person
            assert detail["active_student_count"] == int(enrollment_status == "active" and profile_status == "active")
        else:
            assert detail["facilitators"][0][1] == person

    def test_human_roster_requires_product_profile_without_deleting_subtype(current_account, monkeypatch):
        a = current_account
        request = _display(a, monkeypatch)
        cohort = Cohort(tenant_id=a.tid, name="Missing profile", discipline="networking", status="active")
        a.admin.add(cohort)
        a.admin.flush()
        cid = cohort.id
        a.admin.add(
            Enrollment(tenant_id=a.tid, cohort_id=cid, person_id=a.pid, role_in_cohort="student", status="active")
        )
        a.admin.commit()
        # Without a teaching scope the account is refused (#188); an active cohort
        # nomination then grants exactly this cohort.
        with pytest.raises(HTTPException) as refused:
            instructor.cohorts_list(request, person=a.login_snapshot, db=a.admin)
        assert refused.value.status_code == 403
        _nominate_cohort(a, cid)
        context = instructor.cohorts_list(request, person=a.login_snapshot, db=a.admin)
        assert any(item["person"].id == a.pid for row in context["cohort_rows"] for item in row["roster"])
        a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
        a.admin.commit()
        assert a.admin.get(PartyPerson, a.pid) is not None
        assert a.admin.get(AcademyAccountProfile, a.pid) is None
        context = instructor.cohorts_list(request, person=a.login_snapshot, db=a.admin)
        assert all(a.pid not in {item["person"].id for item in row["roster"]} for row in context["cohort_rows"])
        detail = instructor.cohort_detail(cid, request, person=a.login_snapshot, db=a.admin, limit=12, offset=0)
        assert detail["student_total"] == 0

    def test_two_valid_tenant_rosters_and_candidates_keep_own_identity_membership(
        current_account, tenant_b, monkeypatch
    ):
        a = current_account
        request_a = _display(a, monkeypatch)
        request_b = Request({"type": "http", "headers": [], "state": {"tenant": tenant_b}})
        now = datetime.now(UTC)
        foreign = Party(
            tenant_id=tenant_b.id,
            party_type=PartyType.person,
            display_name="Foreign Human",
            email=f"foreign-{uuid4().hex}@b.edu",
            is_active=True,
        )
        a.admin.add(foreign)
        a.admin.flush()
        foreign_pid = foreign.id
        a.admin.add(PartyPerson(party_id=foreign_pid, first_name="Foreign", last_name="Human"))
        a.admin.flush()
        a.admin.add(
            AcademyAccountProfile(
                tenant_id=tenant_b.id, party_id=foreign_pid, status="active", prefs={}, created_at=now, updated_at=now
            )
        )
        _instructor_grant(a)
        foreign_role = a.admin.scalars(
            select(Role).where(Role.tenant_id == tenant_b.id, Role.slug == "instructor")
        ).first()
        if foreign_role is None:
            foreign_role = Role(tenant_id=tenant_b.id, slug="instructor", name="Instructor")
            a.admin.add(foreign_role)
            a.admin.flush()
        a.admin.add(PartyRoleGrant(tenant_id=tenant_b.id, party_id=foreign_pid, role_id=foreign_role.id))
        cohorts = [
            Cohort(tenant_id=tid, name="Same raw label", discipline="networking", status="active")
            for tid in (a.tid, tenant_b.id)
        ]
        a.admin.add_all(cohorts)
        a.admin.flush()
        cid, foreign_cid = (c.id for c in cohorts)
        a.admin.add_all(
            [
                Enrollment(tenant_id=a.tid, cohort_id=cid, person_id=a.pid, role_in_cohort="student", status="active"),
                Enrollment(
                    tenant_id=tenant_b.id,
                    cohort_id=foreign_cid,
                    person_id=foreign_pid,
                    role_in_cohort="student",
                    status="active",
                ),
            ]
        )
        a.admin.commit()
        # Positive controls for both real tenant-local graphs; no malformed FK edges.
        foreign_account = SimpleNamespace(id=foreign_pid)  # routes read only person.id
        # Teaching authority is tenant-local: each actor is refused in the other tenant.
        for request, outsider in ((request_a, foreign_account), (request_b, a.login_snapshot)):
            with pytest.raises(HTTPException) as refused:
                instructor.cohorts_list(request, person=outsider, db=a.admin)
            assert refused.value.status_code == 403
        for request, actor, own_cid, own_pid, other_cid, other_pid in (
            (request_a, a.login_snapshot, cid, a.pid, foreign_cid, foreign_pid),
            (request_b, foreign_account, foreign_cid, foreign_pid, cid, a.pid),
        ):
            context = instructor.cohorts_list(request, person=actor, db=a.admin)
            assert own_cid in {row["cohort"].id for row in context["cohort_rows"]}
            assert other_cid not in {row["cohort"].id for row in context["cohort_rows"]}
            own_row = next(row for row in context["cohort_rows"] if row["cohort"].id == own_cid)
            assert own_pid in {item["person"].id for item in own_row["roster"]}
            assert other_pid not in {item["person"].id for row in context["cohort_rows"] for item in row["roster"]}
            assert own_pid in {p.id for p in own_row["available_facilitators"]}
            assert other_pid not in {p.id for row in context["cohort_rows"] for p in row["available_facilitators"]}
            detail = instructor.cohort_detail(own_cid, request, person=actor, db=a.admin, limit=12, offset=0)
            assert own_pid in {row["person"].id for row in detail["students"]}
            assert other_pid not in {row["person"].id for row in detail["students"]}

    def test_results_tuple_keeps_inactive_suspended_human_email_and_score(current_account, monkeypatch):
        a = current_account
        request = _display(a, monkeypatch)
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
        course = Course(
            tenant_id=a.tid,
            slug=f"display-{uuid4().hex}",
            title="Display",
            discipline="networking",
            source_ref="x",
            version=1,
            status="published",
            listed=False,
        )
        a.admin.add(course)
        a.admin.flush()
        activity = Activity(
            tenant_id=a.tid,
            course_id=course.id,
            chapter_number=1,
            type="mcq_test",
            title="Result",
            pass_threshold=0.6,
            grading="auto",
            weight=1.0,
        )
        a.admin.add(activity)
        a.admin.flush()
        submission = Submission(tenant_id=a.tid, activity_id=activity.id, person_id=a.pid, answers={}, attempt_no=1)
        a.admin.add(submission)
        a.admin.flush()
        score = Score(
            tenant_id=a.tid,
            submission_id=submission.id,
            score=1,
            max_score=1,
            fraction=1.0,
            passed=True,
            source="auto",
            per_item=[],
        )
        a.admin.add(score)
        a.admin.commit()
        # Results need teaching access (#188): refused without it, global staff with it.
        with pytest.raises(HTTPException) as refused:
            instructor.results(request, person=a.login_snapshot, db=a.admin)
        assert refused.value.status_code == 403
        _instructor_grant(a)
        a.admin.commit()
        rows = instructor.results(request, person=a.login_snapshot, db=a.admin)["rows"]
        ours = [row for row in rows if row[2].submission_id == submission.id]
        assert len(ours) == 1
        email, title, result = ours[0]
        assert email == a.email and title == "Result" and result.submission_id == submission.id
