"""Push course completions to dotmac_erp HR (best-effort, signed, deduped)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint, ERPImportedCompletionKey
from app.models.offering import CourseOffering
from app.services import erp_sync


class _FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        # ERP always answers with a status body; "recorded" is the happy path.
        self._body = {"status": "recorded"} if body is None else body

    def json(self):
        if self._body is _UNREADABLE:
            raise ValueError("not json")
        return self._body


_UNREADABLE = object()


def _seed(admin_session, tenant, *, status="completed", pct=1.0):
    course = Course(
        tenant_id=tenant.id,
        slug=f"c-{uuid.uuid4().hex[:6]}",
        title="Fiber Splicing",
        discipline="fiber",
        source_ref="fiber-splicing@1",
        version=1,
        status="published", listed=False,
    )
    person = Party(
        tenant_id=tenant.id,
        email=f"e{uuid.uuid4().hex[:6]}@dotmac.ng",
        display_name="E M",
        party_type=PartyType.person,
        is_active=True,
    )
    cohort = Cohort(
        tenant_id=tenant.id,
        name=f"Staff {uuid.uuid4().hex[:6]}",
        discipline="fiber",
        status="active",
    )
    admin_session.add_all([course, person, cohort])
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=person.id, first_name="E", last_name="M"))
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant.id,
        cohort_id=cohort.id,
        person_id=person.id,
        role_in_cohort="student",
        status="active",
        audience="staff",
        employee_ref="EMP-2026-0001",
    )
    offering = CourseOffering(
        tenant_id=tenant.id,
        cohort_id=cohort.id,
        course_id=course.id,
        status="active",
    )
    admin_session.add_all([enrollment, offering])
    admin_session.flush()
    comp = CourseCompletion(
        tenant_id=tenant.id,
        person_id=person.id,
        course_id=course.id,
        status=status,
        pct=pct,
        completed_at=datetime(2026, 7, 11, tzinfo=UTC) if status == "completed" else None,
    )
    admin_session.add(comp)
    admin_session.flush()
    return comp, person, course


def _marker(db, tenant, comp, name):
    checkpoint = db.get(ERPCompletionCheckpoint, (tenant.id, comp.id))
    return None if checkpoint is None else getattr(checkpoint, name)


def _configure(monkeypatch, url="https://erp.example/dotmac-academy/webhook"):
    monkeypatch.setattr(settings, "erp_webhook_url", url, raising=False)
    monkeypatch.setattr(settings, "erp_webhook_secret", "shh", raising=False)


def test_push_marks_synced_and_signs(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    captured = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content, headers=headers)
        return _FakeResp(200)

    monkeypatch.setattr(erp_sync.httpx, "post", fake_post)

    comp, _, _ = _seed(admin_session, tenant_a)
    outcome = erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)
    assert outcome == erp_sync.SYNCED
    assert admin_session.get(ERPCompletionCheckpoint, (tenant_a.id, comp.id)).erp_synced_at is not None
    assert _marker(admin_session, tenant_a, comp, "erp_synced_pct") == comp.pct
    body = json.loads(captured["content"])
    assert body["event"] == "course_completed"
    assert body["employee_ref"] == "EMP-2026-0001"
    assert "email" not in body
    assert body["academy_course_ref"] == "fiber-splicing@1"
    assert body["progress_pct"] == 100
    assert body["course_title"] == "Fiber Splicing"
    assert body["passed"] is True
    assert body["certificate_ref"] == str(comp.id)
    assert _marker(admin_session, tenant_a, comp, "erp_synced_pct") == 1.0
    assert captured["headers"]["X-Webhook-Signature-256"].startswith("sha256=")
    admin_session.rollback()


def test_inert_when_unconfigured(admin_session, tenant_a, monkeypatch):
    monkeypatch.setattr(settings, "erp_webhook_url", "", raising=False)
    comp, _, _ = _seed(admin_session, tenant_a)
    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id) == {
        erp_sync.SYNCED: 0,
        erp_sync.UNMATCHED: 0,
        erp_sync.FAILED: 0,
    }
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


def test_sync_pending_dedups(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(200))
    _seed(admin_session, tenant_a)
    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 1
    # already synced → nothing to push
    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 0
    admin_session.rollback()


def test_sync_pending_emits_changed_in_progress_percentage(
    admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    captured = []

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.append(json.loads(content))
        return _FakeResp(200)

    monkeypatch.setattr(erp_sync.httpx, "post", fake_post)
    comp, _, _ = _seed(admin_session, tenant_a, status="in_progress", pct=0.4)

    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 1
    assert captured[0]["event"] == "training_progress_updated"
    assert captured[0]["progress_pct"] == 40
    assert _marker(admin_session, tenant_a, comp, "erp_synced_pct") == 0.4
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None

    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 0
    comp.pct = 0.75
    admin_session.flush()
    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 1
    assert captured[-1]["progress_pct"] == 75
    admin_session.rollback()


def test_sync_pending_skips_external_learners(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(200))
    comp, _, _ = _seed(admin_session, tenant_a, status="in_progress", pct=0.2)
    enrollment = admin_session.scalar(
        select(Enrollment).where(
            Enrollment.tenant_id == tenant_a.id,
            Enrollment.person_id == comp.person_id,
        )
    )
    enrollment.audience = "external"
    enrollment.employee_ref = None
    admin_session.flush()

    assert erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)[erp_sync.SYNCED] == 0
    assert _marker(admin_session, tenant_a, comp, "erp_synced_pct") is None
    admin_session.rollback()


def test_failure_leaves_unsynced(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(503))
    comp, _, _ = _seed(admin_session, tenant_a)
    outcome = erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)
    assert outcome == erp_sync.FAILED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


# ---------------------------------------------------------------------------
# A 2xx is not delivery
# ---------------------------------------------------------------------------


def test_ignored_reply_is_not_recorded_as_delivered(admin_session, tenant_a, monkeypatch):
    """The defect: ERP answers an unmatched employee with HTTP 200 and
    {"status": "ignored"}, and the old code stamped erp_synced_at on any 2xx —
    recording as delivered a completion HR never received, and never retrying
    it. Never fired in production only because there were no completions."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        erp_sync.httpx,
        "post",
        lambda *a, **k: _FakeResp(200, {"status": "ignored", "reason": "no matching employee"}),
    )
    comp, _, _ = _seed(admin_session, tenant_a)

    outcome = erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)
    assert outcome == erp_sync.UNMATCHED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None  # stays in the backlog, visible

    admin_session.rollback()


def test_unmatched_is_counted_separately_from_failure(admin_session, tenant_a, monkeypatch):
    """A silent zero and a silent hundred-unmatched used to look identical."""
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(200, {"status": "ignored"}))
    _seed(admin_session, tenant_a)

    counts = erp_sync.sync_pending(admin_session, tenant_id=tenant_a.id)
    assert counts[erp_sync.UNMATCHED] == 1
    assert counts[erp_sync.SYNCED] == 0
    assert counts[erp_sync.FAILED] == 0
    admin_session.rollback()


def test_unreadable_2xx_body_is_retried_not_assumed(admin_session, tenant_a, monkeypatch):
    """A 2xx we cannot parse is not evidence of anything."""
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(200, _UNREADABLE))
    comp, _, _ = _seed(admin_session, tenant_a)

    outcome = erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)
    assert outcome == erp_sync.FAILED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


def test_recorded_updated_and_duplicate_replies_count_as_delivered(admin_session, tenant_a, monkeypatch):
    """ERP answers "updated" when re-delivering the same certificate ref."""
    _configure(monkeypatch)
    for status in ("recorded", "updated", "duplicate"):
        monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, s=status, **k: _FakeResp(200, {"status": s}))
        comp, _, _ = _seed(admin_session, tenant_a)
        assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.SYNCED
    admin_session.rollback()


def test_payload_carries_a_contract_version(admin_session, tenant_a, monkeypatch):
    """ERP dispatches on it, so a breaking change ships as version 2 beside the
    old handler rather than as a silent reshape."""
    _configure(monkeypatch)
    captured = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        captured.update(content=content)
        return _FakeResp(200)

    monkeypatch.setattr(erp_sync.httpx, "post", fake_post)
    comp, _, _ = _seed(admin_session, tenant_a)
    erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)

    assert json.loads(captured["content"])["version"] == erp_sync.CONTRACT_VERSION
    admin_session.rollback()


def test_422_with_ignored_detail_is_unmatched_not_retryable_failure(admin_session, tenant_a, monkeypatch):
    """ERP now refuses an unmatched employee with 422 and the status under
    `detail`. Reading the body before the status line keeps both the old 200 and
    the new 422 shape classified as UNMATCHED — otherwise the 422 would look
    like a transient failure and retry forever."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        erp_sync.httpx,
        "post",
        lambda *a, **k: _FakeResp(422, {"detail": {"status": "ignored", "reason": "no matching employee"}}),
    )
    comp, _, _ = _seed(admin_session, tenant_a)

    outcome = erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)
    assert outcome == erp_sync.UNMATCHED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


def test_unsupported_event_is_unmatched_not_delivered(admin_session, tenant_a, monkeypatch):
    """A version ERP cannot route is not delivery either."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        erp_sync.httpx,
        "post",
        lambda *a, **k: _FakeResp(422, {"detail": {"status": "unsupported"}}),
    )
    comp, _, _ = _seed(admin_session, tenant_a)

    assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.UNMATCHED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


def test_genuine_server_error_is_still_a_retryable_failure(admin_session, tenant_a, monkeypatch):
    """A 503 with no status body must stay FAILED — it is worth retrying."""
    _configure(monkeypatch)
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: _FakeResp(503, {"detail": "boom"}))
    comp, _, _ = _seed(admin_session, tenant_a)

    assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.FAILED
    assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None
    admin_session.rollback()


def test_checkpoint_ack_shortcircuits_and_preserves_learning_completion(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    before = admin_session.execute(
        text(
            "SELECT row_to_json(c)::text "
            "FROM mod_learning.course_completions c WHERE id=:id"
        ),
        {"id": comp.id},
    ).scalar_one()
    calls = []
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: calls.append(1) or _FakeResp(200))
    for _ in range(2):
        assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.SYNCED
    assert calls == [1]
    assert _marker(admin_session, tenant_a, comp, "erp_synced_pct") == comp.pct
    after = admin_session.execute(
        text(
            "SELECT row_to_json(c)::text "
            "FROM mod_learning.course_completions c WHERE id=:id"
        ),
        {"id": comp.id},
    ).scalar_one()
    assert before == after


def test_missing_checkpoint_legacy_ack_refuses_replay(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    admin_session.add(ERPImportedCompletionKey(tenant_id=tenant_a.id, completion_id=comp.id))
    admin_session.flush()
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("must not replay"))
    with pytest.raises(erp_sync.ERPCheckpointInvariantError, match="missing checkpoint"):
        erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)


@pytest.mark.parametrize("original_ack", [None, datetime(2026, 7, 12, 1, 2, 3, 456789, tzinfo=UTC)])
def test_missing_imported_checkpoint_refuses_after_session_restart(
    admin_engine, admin_session, tenant_a, monkeypatch, original_ack,
):
    """Disposable hosted fixture: membership survives loss of either raw ACK."""
    from sqlalchemy.orm import Session

    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    tenant_id, completion_id = tenant_a.id, comp.id
    admin_session.add(ERPImportedCompletionKey(tenant_id=tenant_id, completion_id=completion_id))
    admin_session.add(ERPCompletionCheckpoint(
        tenant_id=tenant_id, completion_id=completion_id, erp_synced_at=original_ack,
    ))
    admin_session.commit()
    with Session(admin_engine) as deletion:
        deletion.delete(deletion.get(ERPCompletionCheckpoint, (tenant_id, completion_id)))
        deletion.commit()
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("must not replay"))
    with Session(admin_engine) as restarted:
        comp = restarted.get(CourseCompletion, completion_id)
        with pytest.raises(erp_sync.ERPCheckpointInvariantError, match="missing checkpoint"):
            erp_sync.push_completion(restarted, tenant_id=tenant_id, completion=comp)
        assert restarted.get(ERPCompletionCheckpoint, (tenant_id, completion_id)) is None
        assert restarted.get(ERPImportedCompletionKey, (tenant_id, completion_id)) is not None


def test_imported_ack_preserves_raw_timestamp_without_http(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    raw = datetime(2026, 7, 12, 1, 2, 3, 456789, tzinfo=UTC)
    marker = ERPCompletionCheckpoint(tenant_id=tenant_a.id, completion_id=comp.id, erp_synced_at=raw)
    admin_session.add_all([marker, ERPImportedCompletionKey(tenant_id=tenant_a.id, completion_id=comp.id)])
    admin_session.flush()
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("must not replay"))
    assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.SYNCED
    assert marker.erp_synced_at == raw
    assert marker.erp_synced_pct == comp.pct


@pytest.mark.parametrize(("target", "field", "value"), [
    ("enrollment", "status", "inactive"),
    ("enrollment", "audience", "external"),
    ("enrollment", "employee_ref", "  "),
    ("offering", "status", "inactive"),
])
def test_supplied_enrollment_cannot_bypass_filters(
    admin_session, tenant_a, monkeypatch, target, field, value,
):
    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    enrollment = admin_session.scalars(select(Enrollment).where(Enrollment.person_id == comp.person_id)).one()
    offering = admin_session.scalars(select(CourseOffering).where(CourseOffering.course_id == comp.course_id)).one()
    setattr(enrollment if target == "enrollment" else offering, field, value)
    admin_session.flush()
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("ineligible HTTP"))
    assert erp_sync.push_completion(
        admin_session, tenant_id=tenant_a.id, completion=comp, enrollment=enrollment,
    ) == erp_sync.FAILED


def test_staff_enrollment_without_employee_ref_is_refused_by_the_database(admin_session, tenant_a):
    # The exporter's missing-employee_ref filter can no longer be reached with a
    # stored row: the database refuses to hold one.
    comp, _, _ = _seed(admin_session, tenant_a)
    enrollment = admin_session.scalars(select(Enrollment).where(Enrollment.person_id == comp.person_id)).one()
    with pytest.raises(IntegrityError) as refused, admin_session.begin_nested():
        enrollment.employee_ref = None
        admin_session.flush()
    assert refused.value.orig.diag.constraint_name == "ck_enrollments_staff_has_employee_ref"
    admin_session.rollback()


def test_conflicting_enrollment_identity_refuses_before_http(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    comp, person, course = _seed(admin_session, tenant_a)
    cohort = Cohort(tenant_id=tenant_a.id, name="Other staff", discipline="fiber", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    admin_session.add_all([
        Enrollment(tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id,
                   role_in_cohort="student", status="active", audience="staff", employee_ref="EMP-OTHER"),
        CourseOffering(tenant_id=tenant_a.id, cohort_id=cohort.id, course_id=course.id, status="active"),
    ])
    admin_session.flush()
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("ambiguous HTTP"))
    with pytest.raises(erp_sync.ERPCheckpointInvariantError, match="conflicting"):
        erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp)


def test_timeout_and_unknown_ack_leave_checkpoint_pending(admin_session, tenant_a, monkeypatch):
    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)

    def timeout(*args, **kwargs):
        raise TimeoutError("test timeout")

    for post in (timeout, lambda *a, **k: _FakeResp(200, {"status": "unknown"})):
        monkeypatch.setattr(erp_sync.httpx, "post", post)
        assert erp_sync.push_completion(admin_session, tenant_id=tenant_a.id, completion=comp) == erp_sync.FAILED
        assert admin_session.get(ERPCompletionCheckpoint, (tenant_a.id, comp.id)).erp_synced_at is None
        assert _marker(admin_session, tenant_a, comp, "erp_synced_at") is None


def test_overlapping_first_senders_serialize_on_checkpoint(admin_engine, admin_session, tenant_a, monkeypatch):
    """Real PostgreSQL contention, including concurrent lazy checkpoint insert."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.orm import Session

    _configure(monkeypatch)
    comp, _, _ = _seed(admin_session, tenant_a)
    completion_id, tenant_id = comp.id, tenant_a.id
    admin_session.commit()
    entered, release, second_ready = Event(), Event(), Event()
    calls, second_pid = [], []

    def post(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(10), "first sender was not released"
        return _FakeResp(200)

    monkeypatch.setattr(erp_sync.httpx, "post", post)

    def sender(second=False):
        with Session(admin_engine) as db:
            row = db.get(CourseCompletion, completion_id)
            if second:
                second_pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                second_ready.set()
            result = erp_sync.push_completion(db, tenant_id=tenant_id, completion=row)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sender)
        try:
            assert entered.wait(10)
            second = pool.submit(sender, True)
            assert second_ready.wait(10)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with admin_engine.connect() as conn:
                    waiting = conn.execute(
                        text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": second_pid[0]}
                    ).scalar_one()
                if waiting == "Lock":
                    break
                time.sleep(0.02)
            else:
                pytest.fail("competing sender never waited on PostgreSQL checkpoint lock")
            assert calls == [1]
        finally:
            release.set()
        assert first.result(timeout=10) == erp_sync.SYNCED
        assert second.result(timeout=10) == erp_sync.SYNCED
    assert calls == [1]
    admin_session.expire_all()
    assert not hasattr(admin_session.get(CourseCompletion, completion_id), "erp_synced_at")
