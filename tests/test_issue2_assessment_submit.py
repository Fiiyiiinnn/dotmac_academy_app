"""Maintained port of legacy_probe/services/test_genuine_assessment_submit_context.py.

DISPOSITIONS: test_assessment_submit_bearer_replay_deadline_no_consequence
(app/admission_assessment_context.py, entrance_exam.py). Bodies are
the legacy genuine hosted canaries verbatim except the
PlatformSetting settings-tamper sub-case, which now writes kernel
domain_settings via app/services/settings_store. The genuine
admission_case fixture chain comes from
tests/issue2_wave2_support.py; tests run as INNER tests in a
subprocess bound to the per-test genesis database.
"""

from __future__ import annotations

import os

if os.getenv("WAVE2_INNER") != "1":
    from tests.issue2_wave2_support import assert_inner_passed, run_inner

    def test_assessment_submit_inner_suite(genesis_database):
        assert_inner_passed(run_inner(genesis_database, "tests/test_issue2_assessment_submit.py"))

else:
    import asyncio
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from urllib.parse import urlencode
    from uuid import UUID

    import pytest
    from dotmac_kernel.audit import AuditEvent, write_audit_event
    from dotmac_kernel.session_runtime import resolve_database_runtime
    from dotmac_learning import TransactionChanged
    from sqlalchemy import select, text, update
    from sqlalchemy.exc import InvalidRequestError, PendingRollbackError, ProgrammingError
    from starlette.requests import Request

    from app import admission_assessment_context as boundary
    from app.models.admissions import Applicant
    from app.models.cohort import Cohort
    from app.models.email_outbox import EmailOutbox
    from app.models.onboarding import OnboardingTask
    from app.services import admissions, applicant_email, entrance_exam
    from app.services.security import hash_token
    from tests.issue2_wave2_support import (  # noqa: F401
        admin_session,
        admission_case,
        app_user_session,
        current_account,
        roster_actors,
        sign_in_account,
        tenant_a,
        tenant_b,
    )

    assert admission_case
    assert roster_actors
    assert tenant_b

    def _request(c, *, token=None, answers=None, tenant=None, cookie=False):
        fields = [("token", c.token if token is None else token)]
        fields.extend((key, value) for key, values in (answers or {}).items() for value in values)
        body = urlencode(fields).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "https",
                "server": ("academy.example", 443),
                "path": "/apply/assessment",
                "root_path": "",
                "query_string": b"",
                "headers": [(b"content-type", b"application/x-www-form-urlencoded")]
                + (list(c.original.web.scope["headers"]) if cookie else []),
                "state": {"tenant": tenant or c.a.tenant},
            },
            receive,
        )

    @pytest.fixture
    def assessment_case(admission_case):
        c = admission_case
        row = c.a.admin.get(Applicant, c.id)
        row.assessment_started_at = datetime.now(UTC) - timedelta(minutes=15)
        row.assessment_deadline = datetime.now(UTC) + timedelta(days=1)
        token = entrance_exam.issue_token(c.a.admin, applicant=row)
        c.a.admin.get(Cohort, c.cohort_id).auto_accept_threshold = 0.7
        c.a.admin.commit()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        return SimpleNamespace(a=c.a, original=c, id=c.id, cohort_id=c.cohort_id, token=token)

    def _submit(c, **kwargs):
        return asyncio.run(boundary.submit_applicant_assessment(c.a.db, request=_request(c, **kwargs)))

    def _audit(c):
        return c.a.db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.tenant_id == c.a.tid,
                AuditEvent.entity_type == "applicant",
                AuditEvent.entity_id == str(c.id),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        ).all()

    def _effects(c):
        row = c.a.db.get(Applicant, c.id)
        c.a.db.refresh(row)
        return (
            row.status,
            row.assessment_score,
            row.assessment_taken_at,
            row.assessment_result_version,
            row.assessment_answers,
            row.onboarding_token_hash,
            tuple(r.id for r in _audit(c)),
            tuple(c.a.db.scalars(select(EmailOutbox.id).where(EmailOutbox.tenant_id == c.a.tid)).all()),
            tuple(c.a.db.scalars(select(OnboardingTask.id).where(OnboardingTask.applicant_id == c.id)).all()),
        )

    @pytest.mark.parametrize(
        "fraction,threshold,path",
        [
            (4, 0.7, ("screened", "accepted", "onboarding")),
            (3, 0.9, ("waitlisted",)),
            (0, 0.7, ()),
        ],
    )
    def test_real_answers_derive_zero_one_three_system_trace(assessment_case, fraction, threshold, path):
        c = assessment_case
        c.a.admin.get(Cohort, c.cohort_id).auto_accept_threshold = threshold
        c.a.admin.commit()
        answers = {f"q{i}": ["A" if i <= fraction else "B"] for i in range(1, 5)}
        result = _submit(c, answers=answers)
        assert result.disposition == "completed"
        assert result.applicant.assessment_score == fraction / 4
        assert result.applicant.assessment_result_version == 1
        assert result.applicant.status == (path[-1] if path else "applied")
        events = tuple(row for row, _ in boundary._ROOTS[c.a.db][0].receipts)
        assert {e.id for e in events} == {e.id for e in _audit(c)}
        assert len(events) == len(path)
        assert tuple(e.details["to_status"] for e in events) == path
        assert all(type(e.id) is UUID and e.actor_type == "system" and e.actor_party_id is None for e in events)
        assert all(e.actor_id == "assessment_policy" and e.details["source"] == "assessment_policy" for e in events)
        c.a.db.commit()  # Genuine late completion guard participates in actual commit.
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert _submit(c, answers=answers).disposition == "already_completed"
        assert len(_audit(c)) == len(path)

    def test_real_form_overrides_only_posted_autosaved_answers_and_ignores_actor_cookie(assessment_case):
        c = assessment_case
        row = c.a.admin.get(Applicant, c.id)
        row.assessment_answers = {"q1": ["A"], "q2": ["A"], "q3": ["B"], "q4": ["A"]}
        c.a.admin.commit()
        result = _submit(c, answers={"q3": ["A"], "actor_person_id": [str(c.original.actors.admin.pid)]}, cookie=True)
        assert result.applicant.assessment_score == 1
        assert result.applicant.assessment_answers is None
        assert all(e.actor_party_id is None and e.actor_type == "system" for e in _audit(c))
        c.a.db.commit()

    @pytest.mark.parametrize("case", ["invalid", "expired", "completed"])
    def test_real_bearer_replay_and_deadline_have_no_consequences(assessment_case, case):
        c = assessment_case
        row = c.a.admin.get(Applicant, c.id)
        if case == "expired":
            row.assessment_deadline = datetime.now(UTC) - timedelta(seconds=1)
        elif case == "completed":
            row.assessment_taken_at = datetime.now(UTC)
        c.a.admin.commit()
        before = _effects(c)
        result = _submit(c, token="invalid" if case == "invalid" else c.token)
        assert result.disposition == ("deadline_closed" if case == "expired" else "already_completed")
        assert _effects(c) == before
        c.a.db.commit()

    def test_forged_context_is_not_transition_authority(assessment_case):
        c = assessment_case
        fake = object.__new__(boundary._AssessmentContext)
        with pytest.raises((boundary.AssessmentSubmissionError, AttributeError)):
            admissions.apply_assessment_policy(c.a.db, applicant=c.a.db.get(Applicant, c.id), _assessment_context=fake)
        assert _audit(c) == []

    @pytest.mark.parametrize("stage", ["grade", "audit", "tasks", "token", "email"])
    def test_actual_producer_failures_roll_back_result_and_keep_prior_work(assessment_case, monkeypatch, stage):
        c = assessment_case
        before = _effects(c)
        c.a.db.get(Cohort, c.cohort_id).name = "Prior work"
        target, name = {
            "grade": (entrance_exam, "grade_and_record"),
            "audit": (boundary, "write_audit_event"),
            "tasks": (admissions.onboarding, "seed_tasks"),
            "token": (admissions, "mint_onboarding_token"),
            "email": (applicant_email, "send_onboarding_invite"),
        }[stage]
        actual = getattr(target, name)

        def failed(*args, **kwargs):
            actual(*args, **kwargs)
            raise RuntimeError("actual producer failed")

        monkeypatch.setattr(target, name, failed)
        with pytest.raises(boundary.AssessmentSubmissionError):
            _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        c.a.db.commit()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert _effects(c) == before
        assert c.a.db.get(Cohort, c.cohort_id).name == "Prior work"

    @pytest.mark.parametrize("fault", ["score", "profile", "version", "token", "outbox", "policy", "expiry"])
    def test_late_tamper_sticks_through_caught_commit_denial(assessment_case, fault):
        c = assessment_case
        result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        row = result.applicant
        if fault == "score":
            row.assessment_score = 0.125
        elif fault == "profile":
            row.assessment_profile = {"forged": 1}
        elif fault == "version":
            row.assessment_result_version += 1
        elif fault == "token":
            row.assessment_token_hash = hash_token("replacement")
        elif fault == "outbox":
            c.a.db.scalars(
                select(EmailOutbox).where(EmailOutbox.kind == "onboarding_invite", EmailOutbox.tenant_id == c.a.tid)
            ).first().html_body = "tampered"
        elif fault == "policy":
            c.a.db.execute(update(Cohort).where(Cohort.id == c.cohort_id).values(auto_accept_threshold=0.99))
        else:
            row.assessment_deadline = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(boundary.AssessmentSubmissionError):
            c.a.db.commit()
        with pytest.raises(boundary.AssessmentSubmissionError):
            c.a.db.commit()
        c.a.db.rollback()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
        assert _audit(c) == []

    def test_late_audit_update_is_refused_by_the_database(assessment_case):
        """The runtime role cannot rewrite the append-only audit trail: PostgreSQL says so."""
        c = assessment_case
        _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        _audit(c)[0].actor_id = "forged"
        with pytest.raises(ProgrammingError) as first:
            c.a.db.commit()
        assert first.value.orig.sqlstate == "42501" and "audit_events" in str(first.value.orig)
        with pytest.raises((PendingRollbackError, boundary.AssessmentSubmissionError)):
            c.a.db.commit()
        c.a.db.rollback()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert not c.a.db.scalar(
            text(
                "SELECT has_table_privilege('app_user', 'audit_events', 'UPDATE') "
                "OR has_table_privilege('app_user', 'audit_events', 'DELETE')"
            )
        )
        row = c.a.db.get(Applicant, c.id)
        assert row.assessment_taken_at is None
        assert _audit(c) == []

    def test_late_forged_audit_row_is_detected_by_the_consequence_inventory(assessment_case):
        """One extra audit row for the same applicant (INSERT only) breaks the inventory."""
        c = assessment_case
        _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        write_audit_event(
            c.a.db,
            tenant_id=c.a.tid,
            actor_type="system",
            actor_id="forged",
            actor_party_id=None,
            actor_label=None,
            source_application="dotmac_academy_app",
            action="applicant.transition",
            entity_type="applicant",
            entity_id=str(c.id),
            details={"source": "forged"},
            is_success=None,
            request_id=None,
            status_code=None,
            ip_address=None,
            user_agent=None,
        )
        with pytest.raises(boundary.AssessmentSubmissionError, match="consequence inventory"):
            c.a.db.commit()
        with pytest.raises(boundary.AssessmentSubmissionError, match="assessment private origin differs"):
            c.a.db.commit()
        c.a.db.rollback()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
        assert _audit(c) == []

    @pytest.mark.parametrize("kind", ["typed", "cancellation", "commit"])
    def test_transaction_failure_and_original_nonexception_identity(assessment_case, monkeypatch, kind):
        c = assessment_case
        actual = applicant_email.send_onboarding_invite

        class Cancelled(BaseException):
            pass

        same = TransactionChanged("actual transaction change") if kind == "typed" else Cancelled()

        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            if kind == "commit":
                c.a.db.commit()
            raise same

        monkeypatch.setattr(applicant_email, "send_onboarding_invite", interrupted)
        with pytest.raises(TransactionChanged if kind in {"typed", "commit"} else Cancelled) as caught:
            _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        if kind != "commit":
            assert caught.value is same
        c.a.db.commit()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
        assert _audit(c) == []

    def test_genuine_blank_recipient_false_enqueue_is_accepted(assessment_case):
        c = assessment_case
        c.a.admin.get(Applicant, c.id).email = ""
        c.a.admin.commit()
        result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        assert result.applicant.status == "onboarding"
        assert len(_audit(c)) == 3
        assert c.a.db.scalars(select(EmailOutbox.id).where(EmailOutbox.tenant_id == c.a.tid)).all() == []
        c.a.db.commit()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("assessment_score", 0.0),
            ("assessment_level", "beginner"),
            ("assessment_profile", {}),
            ("assessment_valid", False),
            ("assessment_invalid_reason", "near_chance"),
            ("assessment_erp_synced_at", datetime(2026, 1, 1, tzinfo=UTC)),
            ("assessment_time_exceeded", True),
        ],
    )
    def test_partial_result_content_is_held_without_erasure(assessment_case, field, value):
        c = assessment_case
        row = c.a.admin.get(Applicant, c.id)
        setattr(row, field, value)
        c.a.admin.commit()
        with pytest.raises(boundary.AssessmentSubmissionError, match="partial assessment"):
            _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        c.a.db.commit()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        row = c.a.db.get(Applicant, c.id)
        assert getattr(row, field) == value
        assert row.assessment_taken_at is None and row.assessment_result_version == 0
        assert _audit(c) == []

    def test_genuine_reset_preserves_version_and_restarted_attempt_grades_next_revision(assessment_case):
        c = assessment_case
        # Real legacy grade then real reset: no fabricated previous result/boolean.
        row = c.a.admin.get(Applicant, c.id)
        entrance_exam.grade_and_record(
            c.a.admin, tenant_id=c.a.tid, applicant=row, answers={f"q{i}": ["A"] for i in range(1, 5)}
        )
        version = row.assessment_result_version
        assert version > 0
        c.token = entrance_exam.reset_exam(c.a.admin, applicant=row)
        entrance_exam.start_exam(c.a.admin, applicant=row, now=datetime.now(UTC) - timedelta(minutes=15))
        c.a.admin.commit()
        result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        assert result.applicant.assessment_result_version == version + 1
        assert result.applicant.assessment_reset_count == 1
        assert result.applicant.assessment_score == 1
        c.a.db.commit()

    def test_tenant_request_scope_mismatch_is_not_bearer_authority(assessment_case, tenant_b):
        c = assessment_case
        before = _effects(c)
        with pytest.raises(boundary.AssessmentSubmissionError, match="tenant"):
            _submit(c, tenant=tenant_b, answers={f"q{i}": ["A"] for i in range(1, 5)})
        assert _effects(c) == before
        c.a.db.commit()

    def test_context_dto_and_source_string_cannot_submit(assessment_case):
        c = assessment_case
        with pytest.raises(boundary.AssessmentSubmissionError, match="actual Request"):
            asyncio.run(
                boundary.submit_applicant_assessment(
                    c.a.db,
                    request=SimpleNamespace(
                        token=c.token, source="assessment_policy", valid=True, actor_person_id=None
                    ),
                )
            )
        assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
        assert _audit(c) == []

    @pytest.mark.parametrize("change", ["policy", "bearer", "replay", "expiry"])
    def test_actual_blocking_wait_refreshes_policy_bearer_version_and_clock(assessment_case, change):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker

        from tests.issue2_wave2_support import _wait_for_lock

        c = assessment_case
        c.a.db.commit()
        SessionLocal = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
        locked, started, release = Event(), Event(), Event()
        backend = []

        def holder():
            with SessionLocal() as session:
                resolve_database_runtime().set_tenant(session, c.a.tid)
                model = Cohort if change == "policy" else Applicant
                key = c.cohort_id if change == "policy" else c.id
                row = session.scalars(select(model).where(model.id == key).with_for_update()).one()
                locked.set()
                assert release.wait(10)
                if change == "policy":
                    row.auto_accept_threshold = 0.99
                elif change == "bearer":
                    entrance_exam.reset_exam(session, applicant=row)
                elif change == "replay":
                    entrance_exam.grade_and_record(
                        session, tenant_id=c.a.tid, applicant=row, answers={f"q{i}": ["A"] for i in range(1, 5)}
                    )
                else:
                    row.assessment_deadline = datetime.now(UTC) - timedelta(seconds=1)
                session.commit()

        def writer():
            with SessionLocal() as session:
                resolve_database_runtime().set_tenant(session, c.a.tid)
                backend.append(session.scalar(text("SELECT pg_backend_pid()")))
                # Genuine stale ORM identity exists before actual FOR UPDATE wait.
                session.get(Applicant, c.id)
                started.set()
                result = asyncio.run(
                    boundary.submit_applicant_assessment(
                        session, request=_request(c, answers={"q1": ["A"], "q2": ["A"], "q3": ["A"], "q4": ["B"]})
                    )
                )
                if change == "policy":
                    assert result.disposition == "completed"
                    assert result.applicant.status == "waitlisted" and result.applicant.assessment_score == 0.75
                else:
                    assert result.disposition == ("deadline_closed" if change == "expiry" else "already_completed")
                session.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            leader = pool.submit(holder)
            assert locked.wait(10)
            follower = pool.submit(writer)
            assert started.wait(10)
            try:
                resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
                _wait_for_lock(c.a.db, backend[0])
            finally:
                release.set()
            leader.result(timeout=20)
            follower.result(timeout=20)
        c.a.db.expire_all()
        events = _audit(c)
        assert len(events) == (1 if change == "policy" else 0)
        row = c.a.db.get(Applicant, c.id)
        assert row.assessment_result_version == (1 if change in {"policy", "replay"} else 0)

    def test_actual_duplicate_email_false_enqueue_preserves_existing_row(assessment_case):
        c = assessment_case
        c.a.admin.get(Cohort, c.cohort_id).auto_accept_threshold = None
        row = c.a.admin.get(Applicant, c.id)
        assert applicant_email.send_results_received(c.a.admin, applicant=row) is True
        c.a.admin.commit()
        result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        assert result.applicant.status == "applied"
        outbox = c.a.db.scalars(
            select(EmailOutbox).where(
                EmailOutbox.tenant_id == c.a.tid,
                EmailOutbox.idempotency_key == f"assessment-review:{c.id}:0",
            )
        ).all()
        assert len(outbox) == 1 and outbox[0].kind == "assessment_review"
        assert _audit(c) == []
        c.a.db.commit()

    def test_actual_session_root_replacement_is_typed_and_has_no_result(assessment_case, monkeypatch):
        c = assessment_case
        actual = applicant_email.send_onboarding_invite

        def replaced(*args, **kwargs):
            result = actual(*args, **kwargs)
            c.a.db.rollback()
            # The call runs inside the owner's savepoint context manager, so SQLAlchemy itself
            # refuses to autobegin on the ended root; the product's own custody check then
            # raises TransactionChanged for the ended root.
            with pytest.raises(InvalidRequestError, match="closed transaction inside context manager"):
                resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
            return result

        monkeypatch.setattr(applicant_email, "send_onboarding_invite", replaced)
        with pytest.raises(TransactionChanged):
            _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        c.a.db.rollback()
        resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
        assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
        assert _audit(c) == []

    def test_actual_settings_change_after_return_denies_caller_commit(assessment_case):
        from app.services import settings_store as _store

        c = assessment_case
        _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
        key = "learning_exam_seconds_per_question"
        _all = _store.get_all(c.a.admin)
        existed = key in _all
        old = _all.get(key)
        c.a.admin.rollback()
        try:
            _store.set_many(c.a.admin, {key: "999"})
            c.a.admin.commit()
            with pytest.raises(boundary.AssessmentSubmissionError, match="policy"):
                c.a.db.commit()
            c.a.db.rollback()
            resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
            assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
            assert _audit(c) == []
        finally:
            c.a.admin.rollback()
            if existed:
                _store.set_many(c.a.admin, {key: old})
            else:
                _store.delete(c.a.admin, key)
            c.a.admin.commit()
