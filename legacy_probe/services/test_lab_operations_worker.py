"""Worker ownership, fencing, recovery, and admission tests."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning import ActivityWrite, CourseWrite, create_activity, create_course
from dotmac_learning.models import (
    Activity,
    Certificate,
    Course,
    CourseCompletion,
    LearningEvent,
    Score,
    Submission,
)
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import LabExecutionPolicy, settings
from app.models.email_outbox import EmailOutbox
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.models.notification import Notification
from app.services import host_lock, lab_jobs, lab_lifecycle, lab_operations, settings_store
from app.services.exceptions import ConflictError
from app.services.labengine.containerlab import ContainerlabEngine
from app.services.labengine.interface import ExecResult, LabHandle
from legacy_probe.services.test_settings_store_fresh_policy import lab_policy_rows

_STALE_CONSOLES = {"client": {"kind": "linux", "mgmt": "192.0.2.9", "port": 9001}}


def _give_stale_consoles(db, instance):
    """Give a not-yet-escalated instance recorded consoles, to prove escalation
    to ``error`` clears them (stored mgmt IPs / ports are reused elsewhere)."""
    instance.consoles = dict(_STALE_CONSOLES)
    db.commit()


def _seed(db, tenant_id, *, name: str = "one", status: str = "queued", presence: str | None = None):
    course = create_course(
        db,
        CourseWrite(
            id=uuid4(),
            tenant_id=tenant_id,
            slug=f"worker-{name}",
            title="Worker",
            discipline="networking",
            source_ref="test",
            version=1,
            status="published",
            listed=False,
        ),
    )
    activity = create_activity(
        db,
        ActivityWrite(
            id=uuid4(),
            tenant_id=tenant_id,
            course_id=course.id,
            chapter_number=1,
            type="lab",
            title="Worker lab",
            pass_threshold=0.5,
            assessment_mode="graded",
            grading="auto",
            weight=1.0,
        ),
    )
    template = LabTemplate(
        tenant_id=tenant_id,
        course_id=course.id,
        chapter_number=1,
        activity_id=activity.id,
        slug=f"worker-{name}",
        title="Worker lab",
        topology="name: placeholder",
        instructions_html="<p>test</p>",
        checks=[],
        seed_spec={},
        limits={},
        source_hash="test",
        version=1,
    )
    person = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name="Lab Worker",
        email=f"worker-{name}@example.test",
        is_active=True,
    )
    db.add_all([template, person])
    db.flush()
    db.add(PartyPerson(party_id=person.id, first_name="Lab", last_name="Worker"))
    db.flush()
    instance = LabInstance(
        tenant_id=tenant_id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name=f"dal-worker-{name}",
        seed={},
        status=status,
        consoles={},
    )
    if presence is None:
        # Preserve prior (status-derived) capacity semantics for every
        # existing caller that doesn't care about presence directly: a
        # "provisioning"/"active"/"resetting" seed is a stand-in for a
        # successfully-running lab ("present"); anything else defaults to
        # "absent". Callers exercising presence/status divergence directly
        # pass an explicit `presence=`.
        presence = "present" if status in ("provisioning", "active", "resetting") else "absent"
    instance.runtime_presence = presence
    db.add(instance)
    db.flush()
    return instance, person


def _engine(
    instance_name: str,
    policy,
):
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.deploy.return_value = LabHandle(
        instance_name=instance_name,
        nodes={},
        mgmt={},
        kinds={},
    )
    return engine


def test_duplicate_requests_collapse_to_one_open_operation(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id)
    first = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    second = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    assert first.id == second.id
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 1
    admin_session.rollback()


def test_competing_claims_claim_an_operation_once(
    admin_engine,
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id)
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation_id = operation.id
    admin_session.commit()

    factory = sessionmaker(bind=admin_engine, autoflush=False)
    first = factory()
    second = factory()
    try:
        claimed = lab_operations.claim_next(
            first,
            claimed_by="worker-one",
            policy=policy,
        )
        assert claimed is not None and claimed.id == operation_id
        # The first transaction still holds the row lock. SKIP LOCKED means the
        # competing poller observes no claimable row instead of waiting.
        assert (
            lab_operations.claim_next(
                second,
                claimed_by="worker-two",
                policy=policy,
            )
            is None
        )
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


def test_open_operation_conflict_inference_survives_forced_generic_prepared_plans(
    admin_engine,
    admin_session,
    tenant_a,
    policy,
):
    """Force actual enqueue through server-prepared generic plans from call one.

    A bound arbiter predicate cannot infer the partial index in that plan. The
    dedicated engine prevents this test's preparation settings or prepared
    statements from leaking into another test through the shared fixture pool.
    """
    first_instance, first_person = _seed(admin_session, tenant_a.id, name="generic-first")
    second_instance, second_person = _seed(admin_session, tenant_a.id, name="generic-second")
    first_instance_id, second_instance_id = first_instance.id, second_instance.id
    first_person_id, second_person_id = first_person.id, second_person.id
    admin_session.commit()
    engine = create_engine(admin_engine.url, connect_args={"prepare_threshold": 0})
    factory = sessionmaker(bind=engine, autoflush=False)
    try:
        with factory() as db:
            try:
                db.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                db.execute(text("SET LOCAL statement_timeout = '8s'"))
                first_instance = db.get(LabInstance, first_instance_id)
                second_instance = db.get(LabInstance, second_instance_id)
                first = lab_operations.enqueue(db, instance=first_instance, kind="deploy", requested_by=first_person_id)
                for _ in range(6):
                    duplicate = lab_operations.enqueue(
                        db, instance=first_instance, kind="deploy", requested_by=first_person_id
                    )
                    assert duplicate.id == first.id
                first.state = "claimed"
                first.claim_lease_seconds = policy.lease_seconds(first.kind)
                db.flush()
                duplicate = lab_operations.enqueue(
                    db, instance=first_instance, kind="deploy", requested_by=first_person_id
                )
                assert duplicate.id == first.id
                second = lab_operations.enqueue(
                    db, instance=second_instance, kind="deploy", requested_by=second_person_id
                )
                assert second.id != first.id
                first.state = "succeeded"
                db.flush()
                replacement = lab_operations.enqueue(
                    db, instance=first_instance, kind="deploy", requested_by=first_person_id
                )
                assert replacement.id != first.id
                plans = db.execute(
                    text(
                        "SELECT generic_plans, custom_plans FROM pg_prepared_statements "
                        "WHERE statement LIKE :insert AND statement LIKE :conflict"
                    ),
                    {"insert": "%INSERT INTO lab_operations%", "conflict": "%ON CONFLICT%"},
                ).all()
                assert plans, "actual enqueue INSERT was not server-prepared"
                assert sum(row.generic_plans for row in plans) >= 10
                assert all(row.custom_plans == 0 for row in plans)
                for instance_id in (first_instance_id, second_instance_id):
                    assert (
                        db.query(LabOperation)
                        .filter(
                            LabOperation.instance_id == instance_id,
                            LabOperation.state.in_(("queued", "claimed")),
                        )
                        .count()
                        == 1
                    )
                assert db.query(LabOperation).filter_by(instance_id=first_instance_id).count() == 2
            finally:
                db.rollback()
    finally:
        engine.dispose()


def test_competing_pollers_claim_each_operation_exactly_once(
    admin_engine,
    admin_session,
    tenant_a,
    policy,
):
    operation_ids = []
    for index in range(10):
        instance, person = _seed(admin_session, tenant_a.id, name=f"claim-{index}")
        operation_ids.append(
            lab_operations.enqueue(
                admin_session,
                instance=instance,
                kind="deploy",
                requested_by=person.id,
            ).id
        )
    admin_session.commit()
    factory = sessionmaker(bind=admin_engine, autoflush=False)
    start = Barrier(2)

    def _poller(number: int):
        db = factory()
        claimed_ids = []
        try:
            start.wait(timeout=5)
            while True:
                operation = lab_operations.claim_next(
                    db,
                    claimed_by=f"claim-worker-{number}",
                    policy=policy,
                )
                if operation is None:
                    db.rollback()
                    return claimed_ids
                claimed_ids.append(operation.id)
                db.commit()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(_poller, (1, 2)))

    flattened = [operation_id for worker_ids in claimed for operation_id in worker_ids]
    assert len(flattened) == 10
    assert set(flattened) == set(operation_ids)


def test_destroy_has_priority_over_older_deploy(
    admin_session,
    tenant_a,
    policy,
):
    deploy_instance, person = _seed(admin_session, tenant_a.id, name="deploy")
    destroy_instance, _ = _seed(admin_session, tenant_a.id, name="destroy", status="active")
    lab_operations.enqueue(admin_session, instance=deploy_instance, kind="deploy", requested_by=person.id)
    destroy = lab_operations.enqueue(admin_session, instance=destroy_instance, kind="destroy", requested_by=None)
    admin_session.flush()

    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by="worker",
        policy=policy,
    )
    assert claimed is not None
    assert claimed.id == destroy.id
    admin_session.rollback()


def test_check_has_priority_over_older_deploy(
    admin_session,
    tenant_a,
    policy,
):
    deploy_instance, person = _seed(admin_session, tenant_a.id, name="deploy-priority")
    check_instance, _ = _seed(admin_session, tenant_a.id, name="check-priority", status="active")
    lab_operations.enqueue(admin_session, instance=deploy_instance, kind="deploy", requested_by=person.id)
    check = lab_operations.enqueue(admin_session, instance=check_instance, kind="check", requested_by=person.id)
    admin_session.flush()

    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by="worker",
        policy=policy,
    )
    assert claimed is not None
    assert claimed.id == check.id
    admin_session.rollback()


def test_failed_destroy_keeps_instance_active_and_capacity_counted(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "failed"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert operation.state == "failed"
    assert "destroy refused" in operation.last_error


def test_failed_predeploy_destroy_is_capacity_counted_even_from_queued(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="predeploy-destroy")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("cannot prove old lab absent")

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "failed"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert operation.state == "failed"
    assert "cannot prove old lab absent" in operation.last_error
    engine.deploy.assert_not_called()


def test_failed_reset_does_not_leave_a_closed_operation_in_resetting(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="reset-transient", status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    monkeypatch.setattr(
        lab_operations,
        "_run_deploy",
        MagicMock(side_effect=RuntimeError("pre-engine failure")),
    )

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=_engine(
                instance.instance_name,
                policy=policy,
            ),
            policy=policy,
        )
        == "failed"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error == "pre-engine failure"
    assert operation.state == "failed"


@pytest.mark.parametrize("worker_fixture", ["admin_session", "lab_worker_session"])
def test_final_lab_check_completes_course_once_in_worker_transaction(
    admin_session,
    tenant_a,
    tenant_b,
    request,
    worker_fixture,
    policy,
):
    """Use the actual grader/completion/certificate writers, including worker ACLs.

    A failed final lab stays incomplete; passing it completes the course. A
    repeated check, recovered claim, and stale replay cannot issue it again.
    The second tenant deliberately has the same course slug and learner email.
    """
    worker = request.getfixturevalue(worker_fixture)
    instance, person = _seed(admin_session, tenant_a.id, name="final-lab", status="active")
    other_instance, other_person = _seed(admin_session, tenant_b.id, name="final-lab-other", status="active")
    activity = admin_session.get(Activity, instance.activity_id)
    other_activity = admin_session.get(Activity, other_instance.activity_id)
    other_person.email = person.email
    admin_session.get(Course, other_activity.course_id).slug = admin_session.get(Course, activity.course_id).slug
    template = admin_session.scalars(select(LabTemplate).where(LabTemplate.activity_id == activity.id)).one()
    template.checks = [
        {
            "id": "reach",
            "type": "probe",
            "node": "client",
            "probe": {"kind": "ping", "target": "10.0.0.1", "count": 1},
        }
    ]
    instance.consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.5"}}
    quiz = create_activity(
        admin_session,
        ActivityWrite(
            id=uuid4(),
            tenant_id=tenant_a.id,
            course_id=activity.course_id,
            chapter_number=1,
            type="mcq_test",
            title="Already passed quiz",
            pass_threshold=0.5,
            assessment_mode="graded",
            grading="auto",
            weight=1.0,
        ),
    )
    submission = Submission(
        tenant_id=tenant_a.id,
        activity_id=quiz.id,
        person_id=person.id,
        answers={},
        attempt_no=1,
    )
    admin_session.add(submission)
    admin_session.flush()
    admin_session.add(
        Score(
            tenant_id=tenant_a.id,
            submission_id=submission.id,
            score=1,
            max_score=1,
            fraction=1,
            passed=True,
            per_item=[],
            source="auto",
        )
    )
    admin_session.add_all(
        [
            CourseCompletion(
                tenant_id=tenant_a.id,
                person_id=person.id,
                course_id=activity.course_id,
                pct=0.5,
                status="in_progress",
            ),
            CourseCompletion(
                tenant_id=tenant_b.id,
                person_id=other_person.id,
                course_id=other_activity.course_id,
                pct=0,
                status="in_progress",
            ),
        ]
    )
    admin_session.commit()
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )

    def check(*, passed, recover=False):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
        operation_id = operation.id
        if recover:
            operation.state = "claimed"
            operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
            operation.claimed_by = "interrupted-worker"
            operation.claimed_at = _db_now(admin_session) - timedelta(seconds=policy.lease_seconds(operation.kind) + 60)
            operation.heartbeat_at = operation.claimed_at
        admin_session.commit()
        if recover:
            assert (
                lab_operations.reconcile_stuck(
                    worker,
                    policy=policy,
                )
                == 1
            )
            worker.commit()
        claimed = lab_operations.claim_next(
            worker,
            claimed_by="final-lab-worker",
            policy=policy,
        )
        assert claimed is not None and claimed.id == operation_id
        worker.commit()
        engine.exec.return_value = ExecResult(
            "1 packets transmitted, 1 received" if passed else "1 packets transmitted, 0 received",
            "",
            0 if passed else 1,
        )
        assert (
            lab_operations.run_claimed(
                worker,
                operation_id=operation_id,
                claimed_by="final-lab-worker",
                engine=engine,
                policy=policy,
            )
            == "succeeded"
        )
        # Query from the caller's independent admin connection after settlement.
        admin_session.expire_all()
        assert admin_session.get(LabOperation, operation_id).state == "succeeded"
        stored = admin_session.scalars(select(CourseCompletion).where(CourseCompletion.person_id == person.id)).one()
        return operation_id, stored

    _, completion = check(passed=False)
    assert completion.status == "in_progress"
    assert completion.pct == 0.5
    assert completion.completed_at is None
    certificates = admin_session.query(Certificate).filter(Certificate.tenant_id.in_((tenant_a.id, tenant_b.id)))
    assert certificates.count() == 0

    operation_id, completion = check(passed=True)
    assert completion.status == "completed"
    assert completion.pct == 1
    completed_at = completion.completed_at
    assert completed_at is not None
    certificate = certificates.one()
    certificate_identity = certificate.id, certificate.serial, certificate.issued_at
    assert certificate.tenant_id == tenant_a.id
    assert certificate.person_id == person.id
    assert certificate.course_id == activity.course_id

    for passed in (True, False):
        _, completion = check(passed=passed, recover=passed)
        assert completion.status == "completed"
        assert completion.pct == 1
        assert completion.completed_at == completed_at
    calls = engine.exec.call_count
    assert (
        lab_operations.run_claimed(
            worker,
            operation_id=operation_id,
            claimed_by="final-lab-worker",
            engine=engine,
            policy=policy,
        )
        == "stale"
    )
    assert engine.exec.call_count == calls
    assert (
        lab_operations.reconcile_stuck(
            worker,
            policy=policy,
        )
        == 0
    )
    worker.rollback()
    admin_session.expire_all()
    certificate = certificates.one()
    assert (certificate.id, certificate.serial, certificate.issued_at) == certificate_identity
    event = (
        admin_session.query(LearningEvent)
        .filter(
            LearningEvent.tenant_id.in_((tenant_a.id, tenant_b.id)),
            LearningEvent.kind == "certificate_earned",
        )
        .one()
    )
    assert (event.tenant_id, event.person_id, event.course_id) == (
        tenant_a.id,
        person.id,
        activity.course_id,
    )
    other_completion = admin_session.query(CourseCompletion).filter_by(person_id=other_person.id).one()
    assert other_completion.status == "in_progress"
    assert other_completion.pct == 0
    assert other_completion.completed_at is None
    scores = admin_session.scalars(
        select(Score)
        .join(Submission, Submission.id == Score.submission_id)
        .where(Submission.activity_id == activity.id)
    ).all()
    assert len(scores) == 4
    assert sorted(score.passed for score in scores) == [False, False, True, True]
    assert all(score.source == "auto" for score in scores)
    assert all(score.max_score == 1 for score in scores)


def test_worker_check_uses_capability_and_leaves_delivery_to_admin(
    admin_session, tenant_a, lab_worker_session, policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="capability-worker", status="active")
    template = admin_session.scalars(select(LabTemplate).where(LabTemplate.activity_id == instance.activity_id)).one()
    template.checks = [{"id": "reach", "type": "probe", "node": "client",
                        "probe": {"kind": "ping", "target": "10.0.0.1", "count": 1}}]
    instance.consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.5"}}
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
    operation_id = operation.id
    admin_session.commit()
    claimed = lab_operations.claim_next(lab_worker_session, claimed_by="capability-worker", policy=policy)
    assert claimed.id == operation_id
    lab_worker_session.commit()
    engine = _engine(instance.instance_name, policy=policy)
    engine.exec.return_value = ExecResult("1 packets transmitted, 1 received", "", 0)
    statements = []

    def observe(_conn, _cursor, statement, *_args):
        statements.append(statement.lower())

    bind = lab_worker_session.get_bind()
    event.listen(bind, "before_cursor_execute", observe)
    try:
        assert lab_operations.run_claimed(lab_worker_session, operation_id=operation_id,
                                         claimed_by="capability-worker", engine=engine, policy=policy) == "succeeded"
    finally:
        event.remove(bind, "before_cursor_execute", observe)
        lab_worker_session.rollback()
    assert sum("public.record_claimed_lab_check(" in sql for sql in statements) == 1
    assert not any("mod_learning." in sql for sql in statements)
    admin_session.expire_all()
    assert admin_session.query(Score).filter_by(tenant_id=tenant_a.id).one().passed
    assert admin_session.query(Certificate).filter_by(tenant_id=tenant_a.id).count() == 1
    assert admin_session.query(Notification).filter_by(tenant_id=tenant_a.id).count() == 0
    assert admin_session.query(EmailOutbox).filter_by(tenant_id=tenant_a.id).count() == 0
    assert admin_session.scalar(text("SELECT count(*) FROM public.lab_result_deliveries WHERE operation_id=:op"),
                                {"op": operation_id}) == 1


@pytest.mark.parametrize("threshold,passed", [(None, False), (0.5, True)])
def test_grade_heartbeats_finish_before_capability_and_sql_owns_fallback_pass(monkeypatch, threshold, passed):
    from app.services.lab_result_capability import LabGradeReceipt

    order = []
    instance = MagicMock(status="active", error=None, seed={})
    template = MagicMock(checks=[{}], limits={"pass_threshold": threshold})
    receipt = LabGradeReceipt(uuid4(), uuid4(), 1, 1, passed)

    def checks(*_args, before_each):
        before_each()
        order.append("checks")
        return {"score": 1, "max_score": 1, "per_check": []}

    def record(*_args, **_kwargs):
        order.append("capability")
        return receipt

    monkeypatch.setattr(lab_lifecycle, "run_checks", checks)
    monkeypatch.setattr(lab_lifecycle, "record_claimed_lab_check", record)
    assert lab_lifecycle.grade(MagicMock(), instance, MagicMock(), template, MagicMock(),
                               operation_id=uuid4(), claimed_by="worker",
                               before_each_check=lambda: order.append("heartbeat")) == receipt
    assert order == ["heartbeat", "checks", "capability"]
    assert instance.last_active_at is not None


@pytest.mark.parametrize("score,max_score,passed", [(0, 1, True), (1, 2, True), (1, 1, False)])
def test_grade_mismatch_refuses_before_last_active_stamp(monkeypatch, score, max_score, passed):
    from app.services.lab_result_capability import LabGradeReceipt

    instance = MagicMock(status="active", error=None, seed={}, last_active_at=None)
    template = MagicMock(checks=[], limits={"pass_threshold": 0.5})
    monkeypatch.setattr(lab_lifecycle, "run_checks", lambda *_a, **_k: {
        "score": 1, "max_score": 1, "per_check": []})
    monkeypatch.setattr(lab_lifecycle, "record_claimed_lab_check", lambda *_a, **_k:
                        LabGradeReceipt(uuid4(), uuid4(), score, max_score, passed))
    with pytest.raises(ConflictError, match="lab check result mismatch"):
        lab_lifecycle.grade(MagicMock(), instance, MagicMock(), template, MagicMock(),
                            operation_id=uuid4(), claimed_by="worker")
    assert instance.last_active_at is None



def test_failed_check_rolls_back_a_partial_submission(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="partial-check", status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="check", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    def _partial_grade(db, checked_instance, *_args, **_kwargs):
        db.add(
            Submission(
                tenant_id=checked_instance.tenant_id,
                activity_id=checked_instance.activity_id,
                person_id=checked_instance.person_id,
                answers={"instance": str(checked_instance.id)},
                attempt_no=1,
            )
        )
        db.flush()
        raise RuntimeError("score write failed")

    monkeypatch.setattr(lab_operations.lab_lifecycle, "grade", _partial_grade)

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=_engine(
                instance.instance_name,
                policy=policy,
            ),
            policy=policy,
        )
        == "failed"
    )
    admin_session.refresh(operation)
    assert operation.state == "failed"
    assert "score write failed" in operation.last_error
    assert admin_session.query(Submission).filter(Submission.person_id == person.id).count() == 0


def test_error_retry_stays_queued_when_global_capacity_is_full(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    active, _ = _seed(admin_session, tenant_a.id, name="active", status="active")
    retry, person = _seed(admin_session, tenant_a.id, name="retry", status="error")
    operation = lab_operations.enqueue(admin_session, instance=retry, kind="deploy", requested_by=person.id)
    admin_session.commit()

    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by="worker",
        claimed_host="some-host",
        claimed_epoch="111:oldboot",
        policy=policy,
    )
    assert claimed is not None and claimed.id == operation.id
    admin_session.commit()
    admin_session.refresh(operation)
    assert operation.attempts == 1
    assert operation.claimed_host == "some-host"
    assert operation.claimed_epoch == "111:oldboot"

    monkeypatch.setattr(settings, "max_concurrent_labs", 1)

    policy = LabExecutionPolicy.from_settings(settings)
    engine = _engine(
        retry.instance_name,
        policy=policy,
    )

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(retry)
    admin_session.refresh(operation)
    assert active.status == "active"
    assert retry.status == "queued"
    assert operation.state == "queued"
    assert operation.attempts == 0
    # Capacity deferral is a requeue path — the two structural claim-ownership
    # columns (migration 0059) must be cleared alongside claimed_by, exactly
    # like every other requeue path.
    assert operation.claimed_host is None
    assert operation.claimed_epoch is None
    engine.destroy.assert_not_called()
    engine.deploy.assert_not_called()


def test_absent_lab_allows_destroy_then_deploy_replay(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id)
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.destroy.return_value = None  # absent is already destroyed

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    assert instance.status == "active"
    engine.destroy.assert_called_once_with(instance.instance_name)
    engine.deploy.assert_called_once()


def test_expired_claim_is_requeued_and_stale_worker_cannot_settle(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, status="provisioning")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "stale-worker"
    operation.claimed_host = "stale-host"
    operation.claimed_epoch = "111:staleboot"
    operation.claimed_at = _db_now(admin_session) - timedelta(seconds=policy.lease_seconds(operation.kind) + 60)
    operation.heartbeat_at = _db_now(admin_session) - timedelta(seconds=policy.lease_seconds(operation.kind) + 60)
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.commit()
    admin_session.refresh(operation)
    # Ordinary lease recovery is a requeue path too — the two structural
    # claim-ownership columns (migration 0059) must be cleared alongside
    # claimed_by, exactly like every other requeue path.
    assert operation.claimed_host is None
    assert operation.claimed_epoch is None
    replacement = lab_operations.claim_next(
        admin_session,
        claimed_by="new-worker",
        policy=policy,
    )
    assert replacement is not None
    admin_session.commit()

    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="stale-worker",
            engine=engine,
            policy=policy,
        )
        == "stale"
    )
    engine.destroy.assert_not_called()
    engine.deploy.assert_not_called()
    current = admin_session.scalar(select(LabOperation).where(LabOperation.id == operation.id))
    assert current is not None and current.claimed_by == "new-worker"


def test_threaded_admission_never_exceeds_global_capacity(
    admin_engine,
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    first_instance, first_person = _seed(admin_session, tenant_a.id, name="cap-one")
    second_instance, second_person = _seed(admin_session, tenant_a.id, name="cap-two")
    lab_operations.enqueue(
        admin_session,
        instance=first_instance,
        kind="deploy",
        requested_by=first_person.id,
    )
    lab_operations.enqueue(
        admin_session,
        instance=second_instance,
        kind="deploy",
        requested_by=second_person.id,
    )
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    start = Barrier(2)
    engines = [
        _engine(
            first_instance.instance_name,
            policy=policy,
        ),
        _engine(
            second_instance.instance_name,
            policy=policy,
        ),
    ]
    factory = sessionmaker(bind=admin_engine, autoflush=False)

    def _worker(number: int) -> str:
        db = factory()
        try:
            operation = lab_operations.claim_next(
                db,
                claimed_by=f"worker-{number}",
                policy=policy,
            )
            assert operation is not None
            operation_id = operation.id
            db.commit()
            start.wait(timeout=5)
            return lab_operations.run_claimed(
                db,
                operation_id=operation_id,
                claimed_by=f"worker-{number}",
                engine=engines[number],
                policy=policy,
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_worker, (0, 1)))

    assert sorted(outcomes) == ["deferred", "succeeded"]
    admin_session.expire_all()
    statuses = admin_session.scalars(
        select(LabInstance.status).where(LabInstance.id.in_((first_instance.id, second_instance.id)))
    ).all()
    assert statuses.count("active") == 1
    assert statuses.count("queued") == 1
    assert sum(engine.deploy.call_count for engine in engines) == 1


def test_positive_per_tenant_limit_applies_inside_global_capacity(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    active, _ = _seed(admin_session, tenant_a.id, name="tenant-active", status="active")
    pending, person = _seed(admin_session, tenant_a.id, name="tenant-pending")
    operation = lab_operations.enqueue(admin_session, instance=pending, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 2)
    policy = LabExecutionPolicy.from_settings(settings)
    monkeypatch.setattr(settings, "max_concurrent_labs_per_tenant", 1)
    policy = LabExecutionPolicy.from_settings(settings)
    engine = _engine(
        pending.instance_name,
        policy=policy,
    )

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(pending)
    assert active.status == "active"
    assert pending.status == "queued"
    engine.destroy.assert_not_called()


def test_default_leases_are_longer_for_deploy_than_destroy(
    admin_session,
    tenant_a,
    policy,
):
    deploy_instance, person = _seed(admin_session, tenant_a.id, name="deploy-lease")
    destroy_instance, _ = _seed(admin_session, tenant_a.id, name="destroy-lease", status="active")
    old = _db_now(admin_session) - timedelta(seconds=700)
    deploy = lab_operations.enqueue(admin_session, instance=deploy_instance, kind="deploy", requested_by=person.id)
    destroy = lab_operations.enqueue(admin_session, instance=destroy_instance, kind="destroy", requested_by=None)
    for operation in (deploy, destroy):
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = old
        operation.heartbeat_at = old
    admin_session.flush()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    assert deploy.state == "claimed"
    assert destroy.state == "queued"
    admin_session.rollback()


def test_expired_claim_stops_after_kind_attempt_ceiling(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="attempt-ceiling")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    operation.heartbeat_at = operation.claimed_at
    operation.attempts = policy.lab_operation_max_attempts_by_kind["deploy"]
    admin_session.flush()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    assert operation.state == "failed"
    assert operation.finished_at is not None
    assert "after 3 attempts" in operation.last_error
    assert instance.status == "active"
    assert instance.error == operation.last_error
    admin_session.rollback()


def test_wrong_host_refusal_restores_state_and_refunds_attempt(
    admin_session,
    tenant_a,
    tmp_path,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="wrong-host")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    operation.attempts = 1
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 20)
    policy = LabExecutionPolicy.from_settings(settings)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=ContainerlabEngine(
            str(tmp_path),
            lab_host_role="web",
            execution_policy=policy,
        ),
        policy=policy,
    )

    assert outcome == "failed"
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "queued"
    assert instance.error is None
    assert operation.state == "failed"
    assert operation.attempts == 0


def test_worker_identity_includes_one_process_boot_token(
    policy,
):
    identity = lab_operations.worker_identity()
    host, pid, token = identity.rsplit(":", 2)
    assert host
    assert pid.isdigit()
    assert len(token) == 8


def test_capacity_reservation_survives_worker_crash_before_settlement(
    admin_engine,
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="crash")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = KeyboardInterrupt("simulated hard stop")

    try:
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
    except KeyboardInterrupt:
        admin_session.rollback()

    factory = sessionmaker(bind=admin_engine, autoflush=False)
    check = factory()
    try:
        stored = check.get(LabInstance, instance.id)
        claim = check.get(LabOperation, operation.id)
        assert stored is not None and stored.status == "provisioning"
        assert claim is not None and claim.state == "claimed"
    finally:
        check.close()


def test_enqueue_retries_after_conflicting_operation_settles_mid_race(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """A conflicting insert whose row settles before the re-select must retry
    the whole insert exactly once instead of raising ``NoResultFound``."""
    instance, person = _seed(admin_session, tenant_a.id, name="race")
    existing = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.flush()

    original_select = lab_operations._select_open_operation
    calls = {"n": 0}

    def _settle_conflict_then_select(db, inst):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the conflicting operation settling out from under the
            # re-select: it leaves OPEN_STATES right here, so the retried
            # insert below observes no conflict and legitimately succeeds.
            settled = db.get(LabOperation, existing.id)
            settled.state = "succeeded"
            settled.finished_at = datetime.now(UTC)
            db.flush()
            return None
        return original_select(db, inst)

    monkeypatch.setattr(lab_operations, "_select_open_operation", _settle_conflict_then_select)

    retried = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)

    assert retried.id != existing.id
    assert retried.state == "queued"
    assert calls["n"] == 1  # the retried insert succeeded; no second re-select
    admin_session.rollback()


def test_enqueue_raises_conflict_error_when_retry_also_races(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """If the retried insert ALSO conflicts and the re-select ALSO finds
    nothing, enqueue must raise ConflictError rather than crash."""
    instance, person = _seed(admin_session, tenant_a.id, name="double-race")
    lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.flush()

    # The real conflicting row is never settled, so both the initial insert
    # and the retried insert genuinely conflict; forcing the re-select to
    # always report "nothing found" reproduces the vanishingly-unlikely
    # double-vanish case deterministically.
    monkeypatch.setattr(lab_operations, "_select_open_operation", lambda db, inst: None)

    with pytest.raises(ConflictError):
        lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.rollback()


def test_destroy_commits_unknown_presence_and_cleared_consoles_before_engine_work(
    admin_session,
    tenant_a,
    policy,
):
    """The web tier must stop proxying as soon as destroy begins, not when it ends."""
    from sqlalchemy.orm import Session

    instance, _ = _seed(admin_session, tenant_a.id, name="destroy-visible", status="active", presence="present")
    _give_stale_consoles(admin_session, instance)
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1
    admin_session.commit()
    instance_id = instance.id

    seen: dict = {}

    def _destroy(name):
        # An independent connection only sees committed state.
        with Session(admin_session.get_bind()) as other:
            row = other.get(LabInstance, instance_id)
            seen["presence"], seen["consoles"] = row.runtime_presence, row.consoles
        raise RuntimeError("destroy refused")

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = _destroy
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )
    assert outcome == "failed"
    assert seen == {"presence": "unknown", "consoles": {}}
    admin_session.refresh(instance)
    assert instance.runtime_presence == "unknown"
    assert instance.consoles == {}


def test_five_consecutive_automatic_destroy_failures_escalate_instance_to_error(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="escalate", status="active")
    _give_stale_consoles(admin_session, instance)
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")

    for attempt in range(1, policy.lab_operation_max_attempts_by_kind["destroy"] + 1):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        operation.attempts = 1  # matches what claim_next() would have set
        admin_session.commit()

        outcome = lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        assert outcome == "failed"
        admin_session.refresh(instance)
        admin_session.refresh(operation)
        assert operation.state == "failed"
        if attempt < policy.lab_operation_max_attempts_by_kind["destroy"]:
            assert instance.status == "active"
        else:
            assert instance.status == "error"
            assert instance.consoles == {}
            assert instance.error == ("automatic destroy failed 5 times; manual intervention required")


def test_escalated_instance_stops_being_selected_by_idle_reaper(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="escalate-reaper", status="active")
    instance.last_active_at = datetime.now(UTC) - timedelta(hours=2)
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")

    for _ in range(policy.lab_operation_max_attempts_by_kind["destroy"]):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        operation.attempts = 1  # matches what claim_next() would have set
        admin_session.commit()
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
    admin_session.refresh(instance)
    assert instance.status == "error"

    requested = lab_jobs.request_idle_reaps(admin_session)

    assert requested == 0
    admin_session.refresh(instance)
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id, state="queued").count() == 0


def test_user_initiated_deploy_survives_escalation_and_resets_failure_window(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="escalate-then-redeploy", status="active")
    destroy_engine = MagicMock(
        execution_policy=policy,
    )
    destroy_engine.destroy.side_effect = RuntimeError("destroy refused")
    for _ in range(policy.lab_operation_max_attempts_by_kind["destroy"]):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        operation.attempts = 1  # matches what claim_next() would have set
        admin_session.commit()
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=destroy_engine,
            policy=policy,
        )
    admin_session.refresh(instance)
    assert instance.status == "error"

    # (c) a user-attributed deploy can still be enqueued and completes even
    # after automatic-destroy escalation put the instance into "error".
    deploy_op = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    deploy_op.state = "claimed"
    deploy_op.claim_lease_seconds = policy.lease_seconds(deploy_op.kind)
    deploy_op.claimed_by = "worker"
    deploy_op.claimed_at = _db_now(admin_session)
    deploy_op.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    deploy_outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=deploy_op.id,
        claimed_by="worker",
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert deploy_outcome == "succeeded"
    admin_session.refresh(instance)
    assert instance.status == "active"
    assert instance.error is None

    # (d) a fresh first automatic destroy failure after that successful
    # redeploy must NOT immediately re-escalate: the failure-count window
    # reset by the successful deploy must be respected.
    post_redeploy_destroy = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    post_redeploy_destroy.state = "claimed"
    post_redeploy_destroy.claim_lease_seconds = policy.lease_seconds(post_redeploy_destroy.kind)
    post_redeploy_destroy.claimed_by = "worker"
    post_redeploy_destroy.claimed_at = _db_now(admin_session)
    post_redeploy_destroy.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    second_destroy_engine = MagicMock(
        execution_policy=policy,
    )
    second_destroy_engine.destroy.side_effect = RuntimeError("destroy refused again")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=post_redeploy_destroy.id,
        claimed_by="worker",
        engine=second_destroy_engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "active"
    assert instance.error is None


def test_host_lock_contention_defers_without_charging_attempt_or_altering_state(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="host-lock", status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_host = "some-host"
    operation.claimed_epoch = "111:oldboot"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = host_lock.HostLockUnavailable("host lock held")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )

    assert outcome == "deferred"
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    assert operation.state == "queued"
    assert operation.claimed_by is None
    # Host-lock contention is a requeue path — the two structural claim-
    # ownership columns (migration 0059) must be cleared alongside claimed_by,
    # exactly like every other requeue path.
    assert operation.claimed_host is None
    assert operation.claimed_epoch is None
    assert operation.attempts == 0


def test_deploy_failure_detected_even_when_provision_leaves_status_active(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """Stream B's ``provision`` may conservatively leave status "active" on a
    failed deploy; ``_run_deploy`` must still detect the failure via ``error``.
    """
    instance, person = _seed(admin_session, tenant_a.id, name="conservative-fail")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    def _conservative_failure(db, inst, engine, template):
        inst.status = "active"
        inst.error = "runtime creation began but deploy failed"
        db.flush()
        return inst

    monkeypatch.setattr(lab_operations.lab_lifecycle, "provision", _conservative_failure)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )

    assert outcome == "failed"
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error == "runtime creation began but deploy failed"
    assert operation.state == "failed"
    assert "runtime creation began but deploy failed" in operation.last_error


def test_deploy_failure_after_proven_destroy_preserves_absent_presence(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """A pre-deploy-invocation failure inside provision() (topology prep)
    that follows a successful destroy proves the runtime absent. The generic
    failure handler in run_claimed must preserve that, not regress it to
    "unknown" and permanently strand a capacity slot.
    """
    instance, person = _seed(admin_session, tenant_a.id, name="destroyed-then-preflight-fail", presence="present")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    def _proven_absent_failure(db, inst, engine, template):
        inst.status = "error"
        inst.error = "bad topology template"
        inst.runtime_presence = "absent"
        db.flush()
        return inst

    monkeypatch.setattr(lab_operations.lab_lifecycle, "provision", _proven_absent_failure)

    engine = _engine(
        instance.instance_name,
        policy=policy,
    )  # destroy() has no side_effect, so it succeeds
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )

    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "error"
    assert instance.runtime_presence == "absent"
    engine.deploy.assert_not_called()

    # And: the now-proven-absent instance must not still consume capacity —
    # another tenant instance should be admissible even at a cap of 1.
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)
    another, _ = _seed(admin_session, tenant_a.id, name="another", presence="absent")
    assert (
        lab_operations._capacity_available(
            admin_session,
            another,
            policy=policy,
        )
        is True
    )


def test_wrong_host_refusals_do_not_contribute_to_destroy_escalation_count(
    admin_session,
    tenant_a,
    tmp_path,
    policy,
):
    """WrongLabHostError refusals are operator/placement misconfiguration, not
    genuine destroy execution failures, and must not count toward the
    cumulative automatic-destroy escalation threshold."""
    instance, _ = _seed(admin_session, tenant_a.id, name="wrong-host-destroy", status="active")
    refusing_engine = ContainerlabEngine(
        str(tmp_path),
        lab_host_role="web",
        execution_policy=policy,
    )
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]

    for _ in range(threshold + 2):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        admin_session.commit()

        outcome = lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=refusing_engine,
            policy=policy,
        )
        assert outcome == "failed"

    admin_session.refresh(instance)
    assert instance.status == "active"
    assert instance.error is None


def test_legacy_unmarked_wrong_host_rows_do_not_contribute_to_escalation_count(
    admin_session,
    tenant_a,
    policy,
):
    """A row that failed via WrongLabHostError before the operator-refusal
    marker existed (bare ``str(exc)``, no ``[operator-refusal]`` prefix — the
    shape base PR #148 code would have written) must still be excluded from
    the cumulative escalation count, via the stable WrongLabHostError message
    substring rather than the marker alone."""
    instance, _ = _seed(admin_session, tenant_a.id, name="legacy-wrong-host", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]

    for _ in range(threshold - 1):
        legacy_operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        legacy_operation.state = "failed"
        legacy_operation.finished_at = datetime.now(UTC)
        legacy_operation.last_error = "containerlab operations require LAB_HOST_ROLE=lab (got 'web')"
        admin_session.commit()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    # If the legacy rows above wrongly counted, this single genuine failure
    # would be the (threshold-1)+1'th and would escalate; the fix keeps it at
    # count 1 since none of the legacy wrong-host rows are genuine failures.
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )

    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "active"
    assert instance.error is None


def test_stuck_destroy_claim_escalates_instance_via_reconcile_stuck(
    admin_session,
    tenant_a,
    policy,
):
    """A destroy that exhausts its attempts via lease expiry (worker
    crash/hang), not a synchronous exception, must escalate the same way a
    synchronously-failing destroy does — otherwise a hanging engine lets the
    idle reaper re-enqueue destroys for this instance forever."""
    instance, _ = _seed(admin_session, tenant_a.id, name="stuck-destroy-escalate", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")

    for _ in range(threshold - 1):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        admin_session.commit()
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
    admin_session.refresh(instance)
    assert instance.status == "active"
    _give_stale_consoles(admin_session, instance)  # isolates the escalation write

    stuck_operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    stuck_operation.state = "claimed"
    stuck_operation.claim_lease_seconds = policy.lease_seconds(stuck_operation.kind)
    stuck_operation.claimed_by = "stale-worker"
    stuck_operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.heartbeat_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(instance)
    admin_session.refresh(stuck_operation)
    assert stuck_operation.state == "failed"
    assert instance.status == "error"
    assert instance.error == (f"automatic destroy failed {threshold} times; manual intervention required")
    assert instance.consoles == {}


def test_stuck_repair_deploy_claim_escalates_instance_via_reconcile_stuck(
    admin_session,
    tenant_a,
    policy,
):
    """Mirrors ``test_stuck_destroy_claim_escalates_instance_via_reconcile_
    stuck`` for the deploy side of the same problem: a conditional
    (``origin="runtime_repair"``, ``requested_by=None``) repair deploy that
    exhausts its attempts via lease expiry (worker crash/hang), not a
    synchronous exception, must escalate the same way a synchronously-failing
    repair deploy does — otherwise a hanging engine lets ``reconcile_
    runtime``'s ``missing_runtime`` branch re-enqueue repair deploys for this
    instance forever."""
    instance, _ = _seed(admin_session, tenant_a.id, name="stuck-repair-deploy-escalate", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["deploy"]
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.status.return_value = "absent"
    engine.deploy_if_absent.side_effect = RuntimeError("containerlab wedged")

    for _ in range(threshold - 1):
        operation = lab_operations.enqueue(
            admin_session,
            instance=instance,
            kind="deploy",
            requested_by=None,
            origin="runtime_repair",
            runtime_precondition="absent",
        )
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        admin_session.commit()
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
    admin_session.refresh(instance)
    assert instance.status == "active"
    _give_stale_consoles(admin_session, instance)  # isolates the escalation write

    stuck_operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    stuck_operation.state = "claimed"
    stuck_operation.claim_lease_seconds = policy.lease_seconds(stuck_operation.kind)
    stuck_operation.claimed_by = "stale-worker"
    stuck_operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.heartbeat_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(instance)
    admin_session.refresh(stuck_operation)
    assert stuck_operation.state == "failed"
    assert instance.status == "error"
    assert instance.error == (f"automatic repair deploy failed {threshold} times; manual intervention required")
    assert instance.consoles == {}


def test_single_stuck_destroy_row_escalates_via_reconcile_stuck_alone(
    admin_session,
    tenant_a,
    policy,
):
    """Escalation sums attempts, not distinct failed rows: a single operation
    that hangs and burns its full attempt budget via repeated lease-expiry/
    reclaim cycles on the SAME row already represents a full attempt budget
    and must escalate on its own, with no other prior failed rows needed."""
    instance, _ = _seed(admin_session, tenant_a.id, name="single-row-escalate", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]

    stuck_operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    stuck_operation.state = "claimed"
    stuck_operation.claim_lease_seconds = policy.lease_seconds(stuck_operation.kind)
    stuck_operation.claimed_by = "stale-worker"
    stuck_operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.heartbeat_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(instance)
    admin_session.refresh(stuck_operation)
    assert stuck_operation.state == "failed"
    assert instance.status == "error"
    assert instance.error == (f"automatic destroy failed {threshold} times; manual intervention required")


def test_host_lock_during_redeploy_provision_does_not_falsely_restore_active(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """If the destroy half of a deploy's destroy-then-provision sequence
    already succeeded (the real runtime is torn down) and it is the
    subsequent provision/deploy call that hits HostLockUnavailable, restoring
    the pre-op "active" status would falsely claim a working lab. The
    instance must stay in the mid-flight "resetting"/"provisioning" state
    _run_deploy already left it in."""
    instance, person = _seed(admin_session, tenant_a.id, name="host-lock-redeploy", status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1
    admin_session.commit()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.return_value = None  # the old runtime is genuinely torn down

    def _provision_hits_host_lock(db, inst, eng, template):
        raise host_lock.HostLockUnavailable("host lock held during redeploy")

    monkeypatch.setattr(lab_operations.lab_lifecycle, "provision", _provision_hits_host_lock)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )

    assert outcome == "deferred"
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "resetting"
    assert operation.state == "queued"
    assert operation.claimed_by is None
    assert operation.attempts == 0


# --- runtime_presence: capacity accounting ----------------------------------


def test_present_presence_consumes_capacity_regardless_of_lifecycle_status(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """An "error" instance whose presence is "present" must still be counted
    against global capacity — capacity is keyed off runtime_presence, not
    lifecycle status."""
    present_but_errored, _ = _seed(admin_session, tenant_a.id, name="present-error", status="error", presence="present")
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    fresh, person = _seed(admin_session, tenant_a.id, name="present-blocked")
    operation = lab_operations.enqueue(admin_session, instance=fresh, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    admin_session.commit()

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            fresh.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "deferred"
    admin_session.refresh(fresh)
    assert fresh.status == "queued"
    admin_session.refresh(present_but_errored)
    assert present_but_errored.runtime_presence == "present"


def test_unknown_presence_consumes_capacity_regardless_of_lifecycle_status(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """A "queued" instance whose presence is "unknown" (e.g. a pre-cutover
    repair row) must still be counted against global capacity."""
    unknown_but_queued, _ = _seed(
        admin_session, tenant_a.id, name="unknown-queued", status="queued", presence="unknown"
    )
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    fresh, person = _seed(admin_session, tenant_a.id, name="unknown-blocked")
    operation = lab_operations.enqueue(admin_session, instance=fresh, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    admin_session.commit()

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            fresh.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "deferred"
    admin_session.refresh(fresh)
    assert fresh.status == "queued"


def test_absent_presence_does_not_consume_capacity(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """An "active"-status instance whose presence is "absent" (contradictory
    in practice, but proves the accounting is presence-keyed) must NOT count
    against the cap."""
    active_but_absent, _ = _seed(admin_session, tenant_a.id, name="active-absent", status="active", presence="absent")
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    fresh, person = _seed(admin_session, tenant_a.id, name="absent-not-blocked")
    operation = lab_operations.enqueue(admin_session, instance=fresh, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    admin_session.commit()

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            fresh.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "succeeded"
    admin_session.refresh(fresh)
    assert fresh.status == "active"
    admin_session.refresh(active_but_absent)
    assert active_but_absent.runtime_presence == "absent"  # untouched by this pass


def test_capacity_deferral_preserves_presence(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """A deferred admission (capacity full) must not mutate the deferred
    instance's presence — only its status/operation are requeued.

    The deferred candidate must genuinely NOT already be capacity-counted
    (presence="absent"): `_capacity_available`'s `already_consuming` check
    treats "present"/"unknown" as already-counted-so-admit-regardless-of-cap,
    so seeding this instance with either of those values would make it
    ineligible for deferral in the first place, contradicting the very
    scenario this test means to exercise.
    """
    blocker, _ = _seed(admin_session, tenant_a.id, name="deferral-blocker", status="active")
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    retry, person = _seed(admin_session, tenant_a.id, name="deferral-retry", status="error", presence="absent")
    operation = lab_operations.enqueue(admin_session, instance=retry, kind="deploy", requested_by=person.id)
    admin_session.commit()
    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by="worker",
        policy=policy,
    )
    assert claimed is not None and claimed.id == operation.id
    admin_session.commit()

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            retry.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "deferred"
    admin_session.refresh(retry)
    assert retry.status == "queued"
    assert retry.runtime_presence == "absent"  # untouched — never became capacity-counted


def test_conditional_deploy_capacity_deferral_preserves_conditional_shape(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The `is_conditional_deploy` branch's own capacity check
    (`if not _capacity_available(db, instance): _requeue_for_capacity(...)`)
    reuses the same shared helper already covered for the ORDINARY deploy
    path, but had no dedicated test for a conditional repair deploy. A
    capacity deferral must never touch the conditional shape (`origin`/
    `runtime_precondition`) on the requeued row."""
    blocker, _ = _seed(admin_session, tenant_a.id, name="cond-deferral-blocker", status="active")
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)

    retry, person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deferral-retry",
        status="active",
        presence="absent",
    )
    operation = lab_operations.enqueue(
        admin_session,
        instance=retry,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    admin_session.commit()
    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by="worker",
        policy=policy,
    )
    assert claimed is not None and claimed.id == operation.id
    admin_session.commit()
    admin_session.refresh(operation)
    attempts_at_claim = operation.attempts

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            retry.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "deferred"
    admin_session.refresh(retry)
    admin_session.refresh(operation)
    assert retry.status == "queued"
    assert operation.state == "queued"
    assert operation.attempts == max(attempts_at_claim - 1, 0)
    # A capacity deferral never touches the conditional shape.
    assert operation.origin == "runtime_repair"
    assert operation.runtime_precondition == "absent"


def test_already_consuming_presence_is_admitted_even_when_cap_is_full(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The inverse of the deferral test above: an instance whose OWN presence
    is already "present"/"unknown" is the `already_consuming` short-circuit's
    actual job to admit regardless of the cap — it is already occupying a
    capacity slot, so refusing it would not free any capacity, only stall a
    redeploy of an instance that's already counted."""
    for presence in ("present", "unknown"):
        blocker, _ = _seed(admin_session, tenant_a.id, name=f"already-consuming-blocker-{presence}", status="active")
        admin_session.commit()
        monkeypatch.setattr(settings, "max_concurrent_labs", 1)
        policy = LabExecutionPolicy.from_settings(settings)

        candidate, person = _seed(
            admin_session,
            tenant_a.id,
            name=f"already-consuming-{presence}",
            status="active",
            presence=presence,
        )
        operation = lab_operations.enqueue(admin_session, instance=candidate, kind="deploy", requested_by=person.id)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = operation.claimed_at
        admin_session.commit()

        outcome = lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=_engine(
                candidate.instance_name,
                policy=policy,
            ),
            policy=policy,
        )
        # The global cap (1) is already fully occupied by `blocker` alone —
        # admission only succeeds because `candidate` was already counted.
        assert outcome == "succeeded", (
            f"presence={presence!r} candidate should be admitted regardless of "
            "the cap via the already_consuming short-circuit"
        )


def test_deploy_reservation_sets_unknown_before_external_work(
    admin_engine,
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The durable deploy reservation (committed before slow external work)
    must set presence to "unknown" in the same commit — a crash right after
    must leave "unknown" durably visible to another session."""
    instance, person = _seed(admin_session, tenant_a.id, name="reservation-crash")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 1)
    policy = LabExecutionPolicy.from_settings(settings)
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = KeyboardInterrupt("simulated hard stop")

    try:
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
    except KeyboardInterrupt:
        admin_session.rollback()

    factory = sessionmaker(bind=admin_engine, autoflush=False)
    check = factory()
    try:
        stored = check.get(LabInstance, instance.id)
        assert stored is not None
        assert stored.runtime_presence == "unknown"
    finally:
        check.close()


def test_failed_deploy_persists_unknown_presence(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="deploy-fail-presence")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    def _conservative_failure(db, inst, engine, template):
        inst.status = "active"
        inst.error = "deploy failed"
        db.flush()
        return inst

    monkeypatch.setattr(lab_operations.lab_lifecycle, "provision", _conservative_failure)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "unknown"


def test_failed_destroy_persists_unknown_presence_manual(
    admin_session,
    tenant_a,
    policy,
):
    """A manual (user requested_by) destroy failure also sets "unknown" —
    the presence assignment is unconditional on requested_by, unlike the
    escalation check."""
    instance, person = _seed(admin_session, tenant_a.id, name="manual-destroy-fail", status="active")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = RuntimeError("destroy refused")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "unknown"
    assert instance.status == "active"  # manual failures are never escalated


def test_wrong_host_refusal_restores_initial_presence(
    admin_session,
    tenant_a,
    tmp_path,
    monkeypatch,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="wrong-host-presence", presence="present")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = operation.claimed_at
    operation.attempts = 1
    admin_session.commit()
    monkeypatch.setattr(settings, "max_concurrent_labs", 20)
    policy = LabExecutionPolicy.from_settings(settings)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=ContainerlabEngine(
            str(tmp_path),
            lab_host_role="web",
            execution_policy=policy,
        ),
        policy=policy,
    )

    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "present"


def test_host_lock_non_deploy_restores_initial_presence(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="host-lock-presence", status="active", presence="present")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.side_effect = host_lock.HostLockUnavailable("host lock held")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )
    assert outcome == "deferred"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "present"


def test_host_lock_post_destroy_deploy_retains_unknown(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """Host-lock contention hit AFTER the destroy half of a deploy already
    succeeded must retain "unknown", not restore the pre-op presence — the
    old runtime may genuinely be gone."""
    instance, person = _seed(
        admin_session, tenant_a.id, name="host-lock-deploy-presence", status="active", presence="present"
    )
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1
    admin_session.commit()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.return_value = None  # the old runtime is genuinely torn down

    def _provision_hits_host_lock(db, inst, eng, template):
        raise host_lock.HostLockUnavailable("host lock held during redeploy")

    monkeypatch.setattr(lab_operations.lab_lifecycle, "provision", _provision_hits_host_lock)

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )

    assert outcome == "deferred"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "unknown"


def test_lease_expiry_over_ceiling_retains_unknown(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="lease-ceiling-presence", presence="present")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    operation.heartbeat_at = operation.claimed_at
    operation.attempts = policy.lab_operation_max_attempts_by_kind["deploy"]
    admin_session.flush()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    assert instance.runtime_presence == "unknown"
    admin_session.rollback()


def test_lease_expiry_under_ceiling_sets_unknown_on_requeue(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(
        admin_session, tenant_a.id, name="lease-requeue-presence", status="provisioning", presence="present"
    )
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session) - timedelta(seconds=policy.lease_seconds(operation.kind) + 60)
    operation.heartbeat_at = operation.claimed_at
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(instance)
    assert instance.status == "queued"
    assert instance.runtime_presence == "unknown"


def test_stuck_destroy_escalation_retains_unknown(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="stuck-destroy-presence", status="active", presence="present")
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]
    stuck_operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    stuck_operation.state = "claimed"
    stuck_operation.claim_lease_seconds = policy.lease_seconds(stuck_operation.kind)
    stuck_operation.claimed_by = "stale-worker"
    stuck_operation.claimed_at = _db_now(admin_session) - timedelta(hours=1)
    stuck_operation.heartbeat_at = stuck_operation.claimed_at
    stuck_operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(instance)
    assert instance.status == "error"
    assert instance.runtime_presence == "unknown"  # escalation never forces "absent"


def test_successful_deploy_becomes_present(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="deploy-success-presence")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "succeeded"
    admin_session.refresh(instance)
    assert instance.runtime_presence == "present"


def test_successful_destroy_becomes_absent(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(
        admin_session, tenant_a.id, name="destroy-success-presence", status="active", presence="present"
    )
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    admin_session.commit()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.destroy.return_value = None

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )
    assert outcome == "succeeded"
    admin_session.refresh(instance)
    assert instance.status == "reaped"
    assert instance.runtime_presence == "absent"


# --- Stage 2: structural claim ownership and restart reclaim ---------------


def test_claim_next_persists_structural_ownership_alongside_unchanged_claimed_by(
    admin_session,
    tenant_a,
    policy,
):
    """``claimed_by`` stays byte-for-byte what ``worker_identity()`` already
    produces; ``claimed_host``/``claimed_epoch`` are persisted alongside it
    from the same ``WorkerIdentity``."""
    instance, person = _seed(admin_session, tenant_a.id, name="claim-persist")
    lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.commit()

    identity = lab_operations.worker_identity_parts()
    claimed = lab_operations.claim_next(
        admin_session,
        claimed_by=identity.claimed_by,
        claimed_host=identity.host,
        claimed_epoch=identity.epoch,
        policy=policy,
    )
    assert claimed is not None
    assert claimed.claimed_by == lab_operations.worker_identity()
    assert claimed.claimed_host == identity.host
    assert claimed.claimed_epoch == identity.epoch
    admin_session.rollback()


def _claim_row(
    op,
    *,
    claimed_by,
    claimed_host,
    claimed_epoch,
    age=None,
    policy,
    db,
):
    age = age or timedelta(seconds=0)
    op.state = "claimed"
    op.claim_lease_seconds = policy.lease_seconds(op.kind)
    op.claimed_by = claimed_by
    op.claimed_host = claimed_host
    op.claimed_epoch = claimed_epoch
    op.claimed_at = _db_now(db) - age
    op.heartbeat_at = _db_now(db) - age


def test_reclaim_previous_epoch_reclaims_same_host_different_epoch_claim(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-basic", status="provisioning")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = 1
    admin_session.commit()

    reclaimed = lab_operations.reclaim_previous_epoch(
        admin_session,
        host="host-a",
        epoch="222:newboot",
        policy=policy,
    )
    admin_session.commit()

    assert reclaimed == 1
    admin_session.refresh(operation)
    assert operation.state == "queued"
    assert operation.claimed_by is None
    assert operation.claimed_host is None
    assert operation.claimed_epoch is None
    assert operation.claimed_at is None
    assert operation.heartbeat_at is None
    # No refund: attempts stays exactly what claim_next set it to, identical
    # to reconcile_stuck's own under-ceiling requeue.
    assert operation.attempts == 1
    assert "restart reclaim" in operation.last_error
    assert "111:oldboot" in operation.last_error
    assert "222:newboot" in operation.last_error
    assert "host-a" in operation.last_error

    admin_session.refresh(instance)
    assert instance.status == "queued"  # provisioning -> queued, same as reconcile_stuck
    assert instance.runtime_presence == "unknown"


def test_reclaim_previous_epoch_projects_resetting_to_active_for_destroy(
    admin_session,
    tenant_a,
    policy,
):
    instance, _ = _seed(admin_session, tenant_a.id, name="reclaim-resetting", status="resetting", presence="unknown")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.commit()
    admin_session.refresh(instance)
    assert instance.status == "active"
    assert instance.runtime_presence == "unknown"


def test_reclaim_previous_epoch_leaves_current_epoch_claim_untouched(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-current-epoch")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:333:thisboot",
        claimed_host="host-a",
        claimed_epoch="333:thisboot",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="333:thisboot",
            policy=policy,
        )
        == 0
    )
    admin_session.refresh(operation)
    assert operation.state == "claimed"
    assert operation.claimed_by == "host-a:333:thisboot"
    assert operation.claimed_host == "host-a"
    assert operation.claimed_epoch == "333:thisboot"
    admin_session.rollback()


def test_reclaim_previous_epoch_leaves_different_host_claim_untouched(
    admin_session,
    tenant_a,
    policy,
):
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-other-host")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-b:111:oldboot",
        claimed_host="host-b",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 0
    )
    admin_session.refresh(operation)
    assert operation.state == "claimed"
    assert operation.claimed_host == "host-b"
    admin_session.rollback()


def test_reclaim_previous_epoch_leaves_legacy_null_ownership_claim_untouched(
    admin_session,
    tenant_a,
    policy,
):
    """A row claimed by a pre-0059 worker (or a caller that never supplied
    structural ownership) has null ``claimed_host``/``claimed_epoch`` and must
    remain lease-expiry-only — exactly as before this function existed."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-legacy")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host=None,
        claimed_epoch=None,
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 0
    )
    admin_session.refresh(operation)
    assert operation.state == "claimed"
    assert operation.claimed_by == "host-a:111:oldboot"
    admin_session.rollback()


def test_reclaim_previous_epoch_does_not_refund_an_attempt(
    admin_session,
    tenant_a,
    policy,
):
    """An under-ceiling restart reclaim must leave ``attempts`` exactly as
    ``claim_next`` last set it — no refund — identical to
    ``reconcile_stuck``'s own under-ceiling requeue. A refund here would let a
    worker that repeatedly crashes before ever settling the same row cycle
    claim (+1) then reclaim (-1) forever without ``attempts`` ever
    net-advancing, silently defeating ``MAX_ATTEMPTS_BY_KIND`` for exactly the
    failure mode restart-reclaim exists to handle."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-zero-floor")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = 0
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(operation)
    assert operation.attempts == 0  # unchanged, not decremented below zero

    other_instance, other_person = _seed(admin_session, tenant_a.id, name="reclaim-nonzero")
    other_operation = lab_operations.enqueue(
        admin_session, instance=other_instance, kind="deploy", requested_by=other_person.id
    )
    _claim_row(
        other_operation,
        claimed_by="host-a:333:oldboot2",
        claimed_host="host-a",
        claimed_epoch="333:oldboot2",
        policy=policy,
        db=admin_session,
    )
    other_operation.attempts = 2
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="444:newboot2",
            policy=policy,
        )
        == 1
    )
    admin_session.refresh(other_operation)
    assert other_operation.attempts == 2  # unchanged, not decremented


def test_repeated_crash_before_settling_accumulates_to_ceiling_not_forgiven(
    admin_session,
    tenant_a,
    policy,
):
    """A worker that repeatedly crashes before ever settling the SAME
    operation must have its attempts accumulate toward the kind's ceiling
    across restarts, exactly like an ordinary repeated lease-timeout would —
    not be unconditionally forgiven by a refund on every restart-reclaim.
    Cycles claim_next -> commit -> reclaim_previous_epoch -> commit through
    the full attempt ceiling and asserts the operation lands "failed" with
    attempts at the ceiling, instead of oscillating between 0 and 1 forever."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-crash-loop")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    admin_session.commit()

    threshold = policy.lab_operation_max_attempts_by_kind["deploy"]
    for cycle in range(threshold):
        old_epoch = f"{cycle}:oldboot"
        new_epoch = f"{cycle + 1}:newboot"
        claimed = lab_operations.claim_next(
            admin_session,
            claimed_by=f"host-a:{old_epoch}",
            claimed_host="host-a",
            claimed_epoch=old_epoch,
            policy=policy,
        )
        assert claimed is not None and claimed.id == operation.id
        admin_session.commit()

        reclaimed = lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch=new_epoch,
            policy=policy,
        )
        admin_session.commit()
        assert reclaimed == 1

    admin_session.refresh(operation)
    assert operation.state == "failed"
    assert operation.attempts == threshold
    assert "attempt ceiling" in operation.last_error

    # The row is settled terminal — no further claim is possible.
    assert (
        lab_operations.claim_next(
            admin_session,
            claimed_by="host-a:should-not-claim",
            policy=policy,
        )
        is None
    )


def test_reclaim_previous_epoch_rollback_leaves_original_claim_intact(
    admin_session,
    tenant_a,
    policy,
):
    """Simulates an interrupted reclaim transaction (e.g. a SIGTERM arriving
    mid-reclaim): if the caller rolls back instead of committing, the
    original claim must be left completely intact — not partially modified.
    """
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-rollback")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = 1
    admin_session.commit()
    operation_id = operation.id

    reclaimed = lab_operations.reclaim_previous_epoch(
        admin_session,
        host="host-a",
        epoch="222:newboot",
        policy=policy,
    )
    assert reclaimed == 1
    admin_session.rollback()

    current = admin_session.scalar(select(LabOperation).where(LabOperation.id == operation_id))
    assert current.state == "claimed"
    assert current.claimed_by == "host-a:111:oldboot"
    assert current.claimed_host == "host-a"
    assert current.claimed_epoch == "111:oldboot"
    assert current.attempts == 1
    assert current.last_error is None


def test_reclaimed_old_claimed_by_cannot_heartbeat_or_settle_after_a_new_claim(
    admin_session,
    tenant_a,
    policy,
):
    """After restart reclaim requeues a row and a new worker claims it, the
    OLD ``claimed_by`` must be rejected by the existing exact-string fence
    alone — a regression test proving structural fields were never needed in
    that fence, and it still works unchanged."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-fence", status="provisioning")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    old_claimed_by = "host-a:111:oldboot"
    _claim_row(
        operation,
        claimed_by=old_claimed_by,
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.commit()

    new_claimed_by = "host-a:222:newboot"
    new_claim = lab_operations.claim_next(
        admin_session,
        claimed_by=new_claimed_by,
        claimed_host="host-a",
        claimed_epoch="222:newboot",
        policy=policy,
    )
    assert new_claim is not None and new_claim.id == operation.id
    admin_session.commit()

    assert lab_operations.heartbeat(admin_session, operation_id=operation.id, claimed_by=old_claimed_by) is False
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by=old_claimed_by,
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "stale"


def test_structural_ownership_fields_play_no_part_in_any_fencing_predicate(
    admin_session,
    tenant_a,
    policy,
):
    """``claimed_host``/``claimed_epoch`` are informational only — every
    fencing predicate (``heartbeat``, ``_refresh_claim``/``run_claimed``,
    ``_settle``) compares ``claimed_by`` alone. A deliberately mismatched/
    garbage pair of structural fields must not affect whether a claim's real
    owner can heartbeat or settle it — planting a defect that added either
    field to a fencing predicate's WHERE clause would make this fail."""
    instance, person = _seed(admin_session, tenant_a.id, name="fencing-guard")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="worker",
        claimed_host="deliberately-wrong-host",
        claimed_epoch="deliberately-wrong-epoch",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    assert lab_operations.heartbeat(admin_session, operation_id=operation.id, claimed_by="worker") is True

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=_engine(
            instance.instance_name,
            policy=policy,
        ),
        policy=policy,
    )
    assert outcome == "succeeded"


# --- reclaim_previous_epoch: attempt-ceiling and lock-wait hardening --------


def test_reclaim_previous_epoch_stops_after_kind_attempt_ceiling(
    admin_session,
    tenant_a,
    policy,
):
    """A restart-reclaimed row already at its kind's attempt ceiling must be
    marked "failed", not requeued with a refunded attempt — otherwise a
    worker process that repeatedly crashes before ever settling this same
    claim would unconditionally refund the attempt `claim_next` charged on
    every restart, silently defeating `MAX_ATTEMPTS_BY_KIND` forever. Mirrors
    `test_expired_claim_stops_after_kind_attempt_ceiling`'s assertions for the
    `reconcile_stuck` path, adapted for `reclaim_previous_epoch`."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-ceiling")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = policy.lab_operation_max_attempts_by_kind["deploy"]
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.commit()
    admin_session.refresh(operation)
    admin_session.refresh(instance)

    assert operation.state == "failed"
    assert operation.finished_at is not None
    # Not refunded: an at-ceiling row must never get its attempt back.
    assert operation.attempts == policy.lab_operation_max_attempts_by_kind["deploy"]
    assert "attempt ceiling" in operation.last_error
    # Terminal settlement retains claim fields as audit provenance, exactly
    # like reconcile_stuck's own ceiling branch — never cleared here.
    assert operation.claimed_by == "host-a:111:oldboot"
    assert operation.claimed_host == "host-a"
    assert operation.claimed_epoch == "111:oldboot"

    assert instance.status == "active"
    assert instance.error == operation.last_error
    assert instance.runtime_presence == "unknown"


def test_reclaim_previous_epoch_escalates_automatic_destroy_at_ceiling(
    admin_session,
    tenant_a,
    policy,
):
    """An automatic (`requested_by=None`) destroy already at the destroy
    attempt ceiling, when matched by a restart-reclaim scan, must trigger the
    same escalation-to-"error" path `reconcile_stuck`'s own ceiling branch
    already applies — a crashing worker must not be able to dodge escalation
    just because it never lived long enough to hit `reconcile_stuck`'s
    lease-expiry check first. Mirrors
    `test_single_stuck_destroy_row_escalates_via_reconcile_stuck_alone`."""
    instance, _ = _seed(admin_session, tenant_a.id, name="reclaim-escalate", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["destroy"]
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.commit()
    admin_session.refresh(operation)
    admin_session.refresh(instance)

    assert operation.state == "failed"
    assert operation.attempts == threshold  # not refunded
    assert instance.status == "error"
    assert instance.error == (f"automatic destroy failed {threshold} times; manual intervention required")
    assert instance.runtime_presence == "unknown"


def test_reclaim_previous_epoch_escalates_automatic_repair_deploy_at_ceiling(
    admin_session,
    tenant_a,
    policy,
):
    """A conditional (``origin="runtime_repair"``, ``requested_by=None``)
    repair deploy already at the deploy attempt ceiling, when matched by a
    restart-reclaim scan, must trigger the same escalation-to-"error" path
    `reconcile_stuck`'s own ceiling branch already applies — a crashing
    worker must not be able to dodge escalation just because it never lived
    long enough to hit `reconcile_stuck`'s lease-expiry check first. Mirrors
    `test_reclaim_previous_epoch_escalates_automatic_destroy_at_ceiling`."""
    instance, _ = _seed(admin_session, tenant_a.id, name="reclaim-repair-deploy-escalate", status="active")
    threshold = policy.lab_operation_max_attempts_by_kind["deploy"]
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = threshold
    admin_session.commit()

    assert (
        lab_operations.reclaim_previous_epoch(
            admin_session,
            host="host-a",
            epoch="222:newboot",
            policy=policy,
        )
        == 1
    )
    admin_session.commit()
    admin_session.refresh(operation)
    admin_session.refresh(instance)

    assert operation.state == "failed"
    assert operation.attempts == threshold  # not refunded
    assert instance.status == "error"
    assert instance.error == (f"automatic repair deploy failed {threshold} times; manual intervention required")
    assert instance.runtime_presence == "unknown"


def test_reclaim_previous_epoch_lock_wait_is_bounded_by_lock_timeout(
    admin_engine,
    admin_session,
    tenant_a,
    policy,
):
    """`reclaim_previous_epoch`'s blocking (deliberately non-`SKIP LOCKED`)
    `FOR UPDATE` scan must not be able to hang the whole worker's startup
    indefinitely: a `lock_timeout` set on its own transaction bounds the
    wait, so a real conflicting row lock held open by another transaction
    (e.g. the separate `lab-reconcile` process, which is not covered by this
    worker's own singleton lock) makes this call raise within seconds instead
    of blocking forever."""
    instance, person = _seed(admin_session, tenant_a.id, name="reclaim-lock-timeout")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    _claim_row(
        operation,
        claimed_by="host-a:111:oldboot",
        claimed_host="host-a",
        claimed_epoch="111:oldboot",
        policy=policy,
        db=admin_session,
    )
    admin_session.commit()

    factory = sessionmaker(bind=admin_engine, autoflush=False)
    holder = factory()
    # Take and deliberately hold a real conflicting row lock in a separate
    # session/connection — left open (no commit/rollback) across the
    # assertion below, simulating another transaction contending for the
    # exact row reclaim_previous_epoch's FOR UPDATE will try to lock.
    holder.execute(select(LabOperation).where(LabOperation.id == operation.id).with_for_update())
    try:
        start = time.monotonic()
        with pytest.raises(OperationalError):
            lab_operations.reclaim_previous_epoch(
                admin_session,
                host="host-a",
                epoch="222:newboot",
                policy=policy,
            )
        elapsed = time.monotonic() - start
        # Comfortably above the 5s lock_timeout, nowhere near "forever" —
        # proves the wait is bounded rather than merely "usually fast".
        assert elapsed < 10
    finally:
        admin_session.rollback()
        holder.rollback()
        holder.close()


# --- Conditional operations (migration 0060_lab_conditional_ops) -----------


def _claim(
    db,
    operation,
    *,
    claimed_by="worker",
    policy,
):
    """Claim ``operation`` by hand, mirroring ``claim_next``'s attempt charge
    (``op.attempts += 1``) so a HostLockUnavailable-triggered refund is
    actually observable (from a nonzero starting value), not trivially
    "0 minus 1, floored back to 0" either way."""
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = claimed_by
    operation.claimed_at = _db_now(db)
    operation.heartbeat_at = _db_now(db)
    operation.attempts += 1
    db.commit()


def test_conditional_deploy_crash_then_reclaim_then_retry_resyncs_consoles_and_status(
    admin_session,
    tenant_a,
    policy,
):
    """Reproduces the exact crash-then-retry gap: a conditional deploy's
    ``deploy_if_absent()`` genuinely succeeds, but the worker process crashes
    before ``provision_if_absent`` ever records ``consoles``/``status``.
    ``reclaim_previous_epoch`` requeues the still-``claimed`` row (projecting
    a defensive, stale ``status``/``runtime_presence`` it has no way to know
    is wrong). A second worker then claims the SAME operation; its
    preliminary check correctly observes the runtime IS present (it really
    was deployed). Without the resync fix, this would settle "succeeded"
    with empty ``consoles`` and a stale, non-"active" ``status`` — a live,
    running lab that looks like nothing happened. With the fix, the
    preliminary-present path detects the empty ``consoles`` and performs a
    real inspection to converge on the same end state a normal successful
    deploy would reach.
    """
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-crash-resync",
        status="queued",
        presence="absent",
    )
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    # First worker incarnation claims the row.
    _claim_row(
        operation,
        claimed_by="host-a:1:boot1",
        claimed_host="host-a",
        claimed_epoch="1:boot1",
        policy=policy,
        db=admin_session,
    )
    operation.attempts = 1
    admin_session.commit()

    # Simulate run_claimed's capacity-admission reservation commit — this is
    # the durable, pre-slow-work checkpoint that genuinely happens before
    # deploy_if_absent() is ever called, and it is the ONLY DB state a crash
    # immediately after a genuinely successful deploy_if_absent() would ever
    # leave behind (provision_if_absent's own success projection — building
    # consoles, setting status="active" — never got to run).
    instance.status = "provisioning"
    instance.runtime_presence = "unknown"
    admin_session.commit()

    # The worker process crashes here — never runs provision_if_absent's
    # console/status projection, despite the runtime genuinely now running.
    # A fresh worker incarnation's startup reclaim requeues this claim.
    reclaimed = lab_operations.reclaim_previous_epoch(
        admin_session,
        host="host-a",
        epoch="2:boot2",
        policy=policy,
    )
    admin_session.commit()
    assert reclaimed == 1
    admin_session.refresh(operation)
    admin_session.refresh(instance)
    assert operation.state == "queued"
    assert instance.status == "queued"  # provisioning -> queued, same as reconcile_stuck
    assert instance.consoles == {}  # never recorded — the crash gap

    # The new worker incarnation claims the same operation and retries.
    _claim_row(
        operation,
        claimed_by="host-a:2:boot2",
        claimed_host="host-a",
        claimed_epoch="2:boot2",
        policy=policy,
        db=admin_session,
    )
    operation.attempts += 1
    admin_session.commit()

    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    # Preliminary check correctly observes the runtime IS present — it
    # really was deployed by the crashed attempt.
    engine.status.return_value = "running"
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={"client": f"clab-{instance.instance_name}-client"},
        mgmt={"client": "172.20.20.9"},
        kinds={"client": "linux"},
    )

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="host-a:2:boot2",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )

    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert operation.state == "succeeded"
    # The gap this test guards against: without the resync fix, this would
    # be "queued" with empty consoles despite a genuinely running lab.
    assert instance.status == "active"
    assert instance.consoles != {}
    assert instance.consoles["client"]["mgmt"] == "172.20.20.9"
    assert instance.runtime_presence == "present"
    engine.inspect_running.assert_called_once()
    engine.deploy_if_absent.assert_not_called()  # preliminary short-circuit, never reached


def test_conditional_deploy_precondition_holds_deploys_and_succeeds(
    admin_session,
    tenant_a,
    policy,
):
    instance, _person = _seed(admin_session, tenant_a.id, name="cond-deploy-ok", status="active", presence="unknown")
    instance.error = "worker lease expired; operation requeued"
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"  # preliminary check: genuinely absent
    engine.deploy_if_absent.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={},
        mgmt={},
        kinds={},
    )

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.runtime_presence == "present"
    assert operation.state == "succeeded"
    engine.deploy_if_absent.assert_called_once()
    engine.deploy.assert_not_called()


def test_conditional_deploy_precondition_mismatch_at_preliminary_check_resyncs_stale_consoles(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The preliminary check alone (before any capacity admission) finds the
    runtime already present: settle as a successful no-op with respect to
    ``deploy_if_absent``/``engine.deploy`` (never called), but ALWAYS resync
    ``consoles``/``status`` from a fresh, authoritative inspection — never a
    bare trust-and-skip of whatever is currently recorded.

    ``instance.consoles`` starts NON-EMPTY here on purpose: this is the
    realistic ``missing_runtime``-repair starting condition (an instance
    whose runtime disappeared while it was genuinely active still carries
    its old, now-stale console data) — proving the fix for the case Codex's
    round-3 review found the empty/non-empty heuristic got wrong.
    """
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-prelim-stale-resync",
        status="active",
        presence="unknown",
    )
    instance.error = "containerlab runtime is absent"
    instance.consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.5", "port": 1111}}
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "running"  # preliminary check: genuinely present
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={"client": f"clab-{instance.instance_name}-client"},
        mgmt={"client": "172.20.20.11"},
        kinds={"client": "linux"},
    )

    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    # Resynced to the fresh, live console data — NOT the stale placeholder.
    assert instance.consoles["client"]["mgmt"] == "172.20.20.11"
    assert instance.runtime_presence == "present"
    assert stop_calls == [instance.id]  # stale consoles were actually torn down
    engine.inspect_running.assert_called_once()
    engine.deploy_if_absent.assert_not_called()
    engine.deploy.assert_not_called()


def test_conditional_deploy_precondition_mismatch_at_preliminary_check_resyncs_from_empty_consoles(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """Same preliminary-present no-op path, starting from EMPTY consoles —
    still a valid case now that the code no longer branches on emptiness;
    it should still resync correctly."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-prelim-empty-resync",
        status="active",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "running"  # preliminary check: genuinely present
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={"client": f"clab-{instance.instance_name}-client"},
        mgmt={"client": "172.20.20.12"},
        kinds={"client": "linux"},
    )

    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    assert instance.consoles["client"]["mgmt"] == "172.20.20.12"
    assert instance.runtime_presence == "present"
    assert stop_calls == [instance.id]  # unconditional — harmless no-op on already-empty consoles
    engine.inspect_running.assert_called_once()
    engine.deploy_if_absent.assert_not_called()
    engine.deploy.assert_not_called()


def test_conditional_deploy_authoritative_check_catches_a_race_the_preliminary_check_missed(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The preliminary check says absent, but a race makes the runtime
    genuinely present by the time deploy_if_absent's own lock-protected check
    runs — proves the SECOND (authoritative) check is the one that actually
    matters, not the first.

    ``consoles`` is pre-populated here with STALE data — a conditional
    deploy's own target instance is, by construction, one whose runtime was
    flagged missing while its lifecycle status still said active, so any
    pre-existing ``consoles`` on it are untrustworthy regardless of why the
    mismatch was caught (crash-then-retry or a genuine, narrower race) — see
    ``lab_lifecycle._rebuild_consoles_from_live_inspection``'s own docstring.
    The mismatch path always resyncs from a fresh inspection; there is no
    "preserve as-is" branch.
    """
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-race",
        status="active",
        presence="unknown",
    )
    instance.error = None
    instance.consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.5"}}
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"  # preliminary check misses the race
    engine.deploy_if_absent.return_value = None  # authoritative check: genuinely present
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={"client": f"clab-{instance.instance_name}-client"},
        mgmt={"client": "172.20.20.13"},
        kinds={"client": "linux"},
    )

    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    engine.deploy_if_absent.assert_called_once()  # the authoritative check DID run
    assert instance.status == "active"
    assert instance.error is None
    assert instance.runtime_presence == "present"
    # Resynced to fresh, live console data — NOT the stale placeholder.
    assert instance.consoles["client"]["mgmt"] == "172.20.20.13"
    assert stop_calls == [instance.id]  # stale consoles were actually torn down
    engine.deploy.assert_not_called()
    engine.inspect_running.assert_called_once()


def test_conditional_deploy_fully_ambiguous_outcome_escalates_a_transient_prior_status(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """End-to-end ``run_claimed``-level proof that Finding A's escalation
    composes correctly through the real commit/rebind sequence: the
    preliminary check says absent, capacity is admitted (committing the
    transient "provisioning"/"resetting" placeholder), ``deploy_if_absent``
    then returns ``None`` (a race — genuinely present after all), AND the
    follow-up ``inspect_running`` ALSO returns ``None`` (fully ambiguous —
    no positive proof either way).

    The instance's TRUE pre-admission status (``initial_instance_status``,
    captured at the very start of ``run_claimed`` before any branching) is
    itself "provisioning" here — a legitimate starting shape for a
    ``missing_runtime`` repair target, not only "active". Restoring it
    verbatim on this ambiguous outcome would return the row to exactly
    ``reconcile_stuck``'s own stuck-row sweep trigger shape once this
    operation closes with no open operation left to protect it. The fix
    escalates to ``status="error"`` instead."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-fully-ambiguous",
        status="provisioning",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"  # preliminary check misses the race
    engine.deploy_if_absent.return_value = None  # authoritative check: genuinely present
    engine.inspect_running.return_value = None  # follow-up inspection also can't confirm

    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    engine.deploy_if_absent.assert_called_once()
    assert operation.state == "succeeded"
    # Escalated, NOT restored verbatim to the transient "provisioning" value
    # that would otherwise match reconcile_stuck's stuck-row sweep shape.
    assert instance.status == "error"
    assert instance.error
    assert instance.runtime_presence == "unknown"
    assert stop_calls == []  # never confirmed present; nothing torn down
    engine.deploy.assert_not_called()


def test_single_conditional_deploy_failure_does_not_escalate(
    admin_session,
    tenant_a,
    policy,
):
    """A lone automatic repair-deploy failure (no prior history) must revert
    the instance to ``deploy_failure_status`` (here "active", since capacity
    admission commits "resetting" before the engine call fails) exactly as
    before this fix — proving Finding A's escalation only changes the
    CUMULATIVE case, not every failure."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-single-failure",
        status="active",
    )
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "worker"
    operation.claimed_at = _db_now(admin_session)
    operation.heartbeat_at = _db_now(admin_session)
    operation.attempts = 1  # matches what claim_next() would have set
    admin_session.commit()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.status.return_value = "absent"
    engine.deploy_if_absent.side_effect = RuntimeError("containerlab wedged")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=operation.id,
        claimed_by="worker",
        engine=engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert operation.state == "failed"
    assert instance.status == "active"  # reverted, not escalated
    assert instance.error == "containerlab wedged"


def test_repeated_automatic_repair_deploy_failures_escalate_to_manual_intervention(
    admin_session,
    tenant_a,
    policy,
):
    """Mirrors ``test_five_consecutive_automatic_destroy_failures_escalate_
    instance_to_error`` for the deploy side of the same problem: a
    persistently wedged instance's automatic (reconciler-triggered) repair
    deploys must stop retrying forever once cumulative attempts reach
    ``MAX_ATTEMPTS_BY_KIND["deploy"]`` — see this file's round-9 fix adding
    ``_automatic_repair_deploy_escalation_message``."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-escalate",
        status="active",
    )
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.status.return_value = "absent"
    engine.deploy_if_absent.side_effect = RuntimeError("containerlab wedged")

    _give_stale_consoles(admin_session, instance)
    for attempt in range(1, policy.lab_operation_max_attempts_by_kind["deploy"] + 1):
        operation = lab_operations.enqueue(
            admin_session,
            instance=instance,
            kind="deploy",
            requested_by=None,
            origin="runtime_repair",
            runtime_precondition="absent",
        )
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        operation.attempts = 1  # matches what claim_next() would have set
        admin_session.commit()

        outcome = lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        assert outcome == "failed"
        admin_session.refresh(instance)
        admin_session.refresh(operation)
        assert operation.state == "failed"
        if attempt < policy.lab_operation_max_attempts_by_kind["deploy"]:
            assert instance.status == "active"  # reverted, not escalated yet
        else:
            assert instance.status == "error"
            assert instance.consoles == {}
            assert instance.error == ("automatic repair deploy failed 3 times; manual intervention required")


def test_conditional_deploy_escalated_settlement_does_not_reset_destroy_failure_window(
    admin_session,
    tenant_a,
    policy,
):
    """A "succeeded" conditional-deploy settlement that only escalated a
    fully-ambiguous outcome to manual verification (round 6) must not
    silently reset the DESTROY-side automatic-escalation window (Finding B):
    the earlier destroy failures must still count toward the destroy
    ceiling afterward, unlike a GENUINELY successful redeploy (contrast with
    ``test_user_initiated_deploy_survives_escalation_and_resets_failure_
    window``, which proves a real successful redeploy DOES reset it)."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-no-reset",
        status="provisioning",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    destroy_engine = MagicMock(
        execution_policy=policy,
    )
    destroy_engine.destroy.side_effect = RuntimeError("destroy refused")
    prior_destroy_failures = policy.lab_operation_max_attempts_by_kind["destroy"] - 1
    for _ in range(prior_destroy_failures):
        operation = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
        operation.state = "claimed"
        operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
        operation.claimed_by = "worker"
        operation.claimed_at = _db_now(admin_session)
        operation.heartbeat_at = _db_now(admin_session)
        operation.attempts = 1  # matches what claim_next() would have set
        admin_session.commit()
        outcome = lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=destroy_engine,
            policy=policy,
        )
        assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "provisioning"  # under the destroy ceiling, not escalated

    # A conditional deploy on the SAME instance settles via the fully-
    # ambiguous escalation path: preliminary check misses a race, the
    # authoritative check also can't confirm, and the follow-up inspection
    # can't confirm either.
    deploy_op = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        deploy_op,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"
    engine.deploy_if_absent.return_value = None
    engine.inspect_running.return_value = None

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=deploy_op.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(deploy_op)
    assert instance.status == "error"  # escalated, per round-6 fix
    assert deploy_op.state == "succeeded"
    assert deploy_op.last_error  # marked so it can't be mistaken for a healthy redeploy

    # A subsequent destroy failure's cumulative count must still include the
    # earlier destroy failures: the window was NOT reset by the escalated
    # "succeeded" deploy settlement.
    next_destroy = lab_operations.enqueue(admin_session, instance=instance, kind="destroy", requested_by=None)
    next_destroy.state = "claimed"
    next_destroy.claim_lease_seconds = policy.lease_seconds(next_destroy.kind)
    next_destroy.claimed_by = "worker"
    next_destroy.claimed_at = _db_now(admin_session)
    next_destroy.heartbeat_at = _db_now(admin_session)
    next_destroy.attempts = 1
    admin_session.commit()
    final_destroy_engine = MagicMock(
        execution_policy=policy,
    )
    final_destroy_engine.destroy.side_effect = RuntimeError("destroy refused again")

    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=next_destroy.id,
        claimed_by="worker",
        engine=final_destroy_engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "error"
    assert instance.error == (
        f"automatic destroy failed {policy.lab_operation_max_attempts_by_kind['destroy']} "
        "times; manual intervention required"
    )


def test_conditional_deploy_ambiguous_non_escalated_outcome_still_marks_operation_last_error(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """Proves the specific gap this fix closes: the preliminary/authoritative
    check misses (genuinely present after all, per a race), the follow-up
    ``inspect_running`` is ALSO ambiguous (returns ``None``), but the
    instance's TRUE prior status is "active" — NOT one of the transient
    values ("queued"/"provisioning"/"resetting") that ``provision_if_absent``
    escalates to ``status="error"``. "active" is deliberately never
    escalated (it has its own safe self-healing path via
    ``reconcile_runtime``'s next pass), so ``instance.status`` stays
    "active" here — but the outcome is JUST AS UNCONFIRMED as the escalated
    case. Checking ``instance.status == "error"`` alone (the pre-fix trigger)
    missed this case entirely; the fix (``instance.runtime_presence !=
    "present"``) must still mark the OPERATION's own ``last_error`` so
    neither escalation function's window-reset query mistakes this for a
    genuinely healthy redeploy."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-ambiguous-active",
        status="active",
        presence="present",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"  # preliminary check misses the race
    engine.deploy_if_absent.return_value = None  # authoritative check: genuinely present
    engine.inspect_running.return_value = None  # follow-up inspection also can't confirm

    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    engine.deploy_if_absent.assert_called_once()
    assert operation.state == "succeeded"
    # NOT escalated: "active" is never escalated by design (its own
    # self-healing path via reconcile_runtime's next pass handles it) — this
    # is the case the pre-fix trigger missed entirely.
    assert instance.status == "active"
    assert instance.runtime_presence == "unknown"
    assert stop_calls == []  # never confirmed present; nothing torn down
    engine.deploy.assert_not_called()
    # The actual property this fix proves: last_error is set even though
    # status never became "error".
    assert operation.last_error


def test_conditional_deploy_ambiguous_non_escalated_settlement_does_not_reset_repair_deploy_failure_window(
    admin_session,
    tenant_a,
    policy,
):
    """End-to-end proof of the consequence: a genuine repair-deploy failure,
    then this exact ambiguous-but-NOT-escalated no-op (instance status stays
    "active" throughout, never touches "error"), then another genuine
    repair-deploy failure — the cumulative escalation count used by
    ``_automatic_repair_deploy_escalation_message`` must still include BOTH
    genuine failures; the ambiguous no-op in between must not reset the
    window. Mirrors ``test_conditional_deploy_escalated_settlement_does_not_
    reset_destroy_failure_window``'s structure, but for the repair-deploy
    escalation function and for this specific non-escalated-but-ambiguous
    case (rather than the escalated-to-error case that test already
    covers)."""
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-window-no-reset",
        status="active",
        presence="present",
    )
    instance.error = None
    admin_session.flush()

    # Genuine failure #1: burns 2 attempts on one row (mirrors a
    # lease-expiry/restart-reclaim burning multiple attempts on a single
    # operation — see _automatic_repair_deploy_escalation_message's own
    # docstring on why attempts, not row count, are summed).
    first_failure = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    first_failure.state = "claimed"
    first_failure.claim_lease_seconds = policy.lease_seconds(first_failure.kind)
    first_failure.claimed_by = "worker"
    first_failure.claimed_at = _db_now(admin_session)
    first_failure.heartbeat_at = _db_now(admin_session)
    first_failure.attempts = 2
    admin_session.commit()

    failing_engine = MagicMock(
        execution_policy=policy,
    )
    failing_engine.status.return_value = "absent"
    failing_engine.deploy_if_absent.side_effect = RuntimeError("containerlab wedged")
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=first_failure.id,
        claimed_by="worker",
        engine=failing_engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "active"  # under the deploy ceiling, not escalated

    # The ambiguous, non-escalated no-op: preliminary/authoritative checks
    # miss, follow-up inspection also can't confirm, TRUE prior status
    # ("active") is not one of the transient values, so it settles
    # "succeeded" WITHOUT escalating status to "error" — exactly the
    # previously-missed case.
    noop_op = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        noop_op,
        policy=policy,
    )
    noop_engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    noop_engine.status.return_value = "absent"
    noop_engine.deploy_if_absent.return_value = None
    noop_engine.inspect_running.return_value = None
    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=noop_op.id,
            claimed_by="worker",
            engine=noop_engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    admin_session.refresh(noop_op)
    assert instance.status == "active"  # NOT escalated — the case this fix covers
    assert noop_op.state == "succeeded"
    assert noop_op.last_error  # marked so it can't be mistaken for a healthy redeploy

    # Genuine failure #2: with the ceiling at 3 and failure #1 having burned
    # 2 attempts, this single-attempt failure's cumulative count (2 + 1 = 3)
    # reaches the ceiling and escalates — but ONLY if failure #1's attempts
    # still count, i.e. only if the no-op above did NOT reset the window.
    # Before this fix, the no-op would have settled with last_error=None,
    # which _automatic_repair_deploy_escalation_message's own
    # last_deploy_success_at query treats as proof of a genuinely healthy
    # redeploy, silently excluding failure #1 from the count and leaving
    # this second failure short of the ceiling (cumulative 1 < 3, not
    # escalated).
    second_failure = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    second_failure.state = "claimed"
    second_failure.claim_lease_seconds = policy.lease_seconds(second_failure.kind)
    second_failure.claimed_by = "worker"
    second_failure.claimed_at = _db_now(admin_session)
    second_failure.heartbeat_at = _db_now(admin_session)
    second_failure.attempts = 1
    admin_session.commit()

    final_engine = MagicMock(
        execution_policy=policy,
    )
    final_engine.status.return_value = "absent"
    final_engine.deploy_if_absent.side_effect = RuntimeError("containerlab wedged again")
    outcome = lab_operations.run_claimed(
        admin_session,
        operation_id=second_failure.id,
        claimed_by="worker",
        engine=final_engine,
        policy=policy,
    )
    assert outcome == "failed"
    admin_session.refresh(instance)
    assert instance.status == "error"
    assert instance.error == (
        f"automatic repair deploy failed {policy.lab_operation_max_attempts_by_kind['deploy']} "
        "times; manual intervention required"
    )


def test_conditional_destroy_precondition_holds_destroys_and_stops_consoles_after(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-destroy-ok",
        status="reaped",
        presence="present",
    )
    instance.consoles = {"client": {"kind": "linux"}}
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="destroy",
        requested_by=None,
        origin="runtime_cleanup",
        runtime_precondition="present",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    order = []
    engine.destroy_if_present.side_effect = lambda name: order.append("destroy_if_present") or True
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: order.append("stop_consoles"))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    assert instance.status == "reaped"
    assert instance.runtime_presence == "absent"
    assert order == ["destroy_if_present", "stop_consoles"]
    engine.destroy.assert_not_called()


def test_conditional_destroy_precondition_mismatch_settles_noop(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-destroy-noop",
        status="reaped",
        presence="unknown",
    )
    instance.error = "prior error text"
    instance.consoles = {"client": {"kind": "linux"}}
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="destroy",
        requested_by=None,
        origin="runtime_cleanup",
        runtime_precondition="present",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.destroy_if_present.return_value = False  # precondition mismatch: already absent
    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "succeeded"
    )
    admin_session.refresh(instance)
    assert instance.status == "reaped"
    assert instance.error == "prior error text"
    assert instance.consoles == {"client": {"kind": "linux"}}
    assert instance.runtime_presence == "absent"
    assert stop_calls == []
    engine.destroy.assert_not_called()


def test_conditional_deploy_host_lock_unavailable_restores_initial_state_without_charging_attempt(
    admin_session,
    tenant_a,
    policy,
):
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-lock",
        status="active",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    attempts_at_claim = operation.attempts
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.side_effect = host_lock.HostLockUnavailable("host lock held")

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    assert instance.runtime_presence == "unknown"
    assert operation.state == "queued"
    assert operation.claimed_by is None
    # Refunded, not charged an extra attempt for lock contention.
    assert operation.attempts == max(attempts_at_claim - 1, 0)


def test_conditional_deploy_host_lock_unavailable_during_present_resync_restores_initial_state_without_charging_attempt(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The preliminary check finds the runtime present (no lock error there),
    but the follow-up ``resync_present_preliminary`` ->
    ``_rebuild_consoles_from_live_inspection`` -> ``engine.inspect_running()``
    call raises ``HostLockUnavailable`` instead. This is the second of three
    ``HostLockUnavailable`` raise points in the conditional-deploy branch, and
    exercises the composed correctness claim in ``run_claimed``'s own
    ``HostLockUnavailable`` handler: because ``_rebuild_consoles_from_live_
    inspection`` now calls ``inspect_running()`` before ``stop_consoles()``,
    nothing has been mutated yet, so restoring the instance's exact
    pre-operation state here is safe. ``stop_consoles`` must never be called.
    """
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-lock-present",
        status="active",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    attempts_at_claim = operation.attempts
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "running"
    engine.inspect_running.side_effect = host_lock.HostLockUnavailable("host lock held")
    stop_calls = []
    monkeypatch.setattr(lab_lifecycle, "stop_consoles", lambda i: stop_calls.append(i.id))

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    assert instance.runtime_presence == "unknown"
    assert operation.state == "queued"
    assert operation.claimed_by is None
    # Refunded, not charged an extra attempt for lock contention.
    assert operation.attempts == max(attempts_at_claim - 1, 0)
    # Proves the ordering-fix composition claim: no console teardown occurred.
    assert stop_calls == []


def test_conditional_deploy_host_lock_unavailable_during_deploy_if_absent_restores_initial_state_without_charging_attempt(
    admin_session,
    tenant_a,
    policy,
):
    """The preliminary check finds the runtime absent, capacity is admitted,
    but ``provision_if_absent``'s own ``engine.deploy_if_absent()`` call
    raises ``HostLockUnavailable``. This is the third of three
    ``HostLockUnavailable`` raise points in the conditional-deploy branch:
    ``deploy_if_absent`` performs the precondition check and the mutation
    atomically inside one host-lock acquisition, so a raise from that single
    acquisition proves nothing was mutated, and restoring the instance's
    exact pre-operation state (including the capacity-admission placeholder
    status/presence) is safe.
    """
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-deploy-lock-absent",
        status="active",
        presence="unknown",
    )
    instance.error = None
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="deploy",
        requested_by=None,
        origin="runtime_repair",
        runtime_precondition="absent",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    attempts_at_claim = operation.attempts
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.status.return_value = "absent"
    engine.deploy_if_absent.side_effect = host_lock.HostLockUnavailable("host lock held")

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "active"
    assert instance.error is None
    assert instance.runtime_presence == "unknown"
    assert operation.state == "queued"
    assert operation.claimed_by is None
    # Refunded, not charged an extra attempt for lock contention.
    assert operation.attempts == max(attempts_at_claim - 1, 0)


def test_conditional_destroy_host_lock_unavailable_restores_initial_state_without_charging_attempt(
    admin_session,
    tenant_a,
    policy,
):
    instance, _person = _seed(
        admin_session,
        tenant_a.id,
        name="cond-destroy-lock",
        status="reaped",
        presence="present",
    )
    admin_session.flush()
    operation = lab_operations.enqueue(
        admin_session,
        instance=instance,
        kind="destroy",
        requested_by=None,
        origin="runtime_cleanup",
        runtime_precondition="present",
    )
    _claim(
        admin_session,
        operation,
        policy=policy,
    )
    attempts_at_claim = operation.attempts
    engine = _engine(
        instance.instance_name,
        policy=policy,
    )
    engine.destroy_if_present.side_effect = host_lock.HostLockUnavailable("host lock held")

    assert (
        lab_operations.run_claimed(
            admin_session,
            operation_id=operation.id,
            claimed_by="worker",
            engine=engine,
            policy=policy,
        )
        == "deferred"
    )
    admin_session.refresh(instance)
    admin_session.refresh(operation)
    assert instance.status == "reaped"
    assert instance.runtime_presence == "present"
    assert operation.state == "queued"
    assert operation.attempts == max(attempts_at_claim - 1, 0)


def _db_now(db):
    return db.scalar(select(func.clock_timestamp()))


@pytest.fixture
def policy():
    """Explicit validated env/default snapshot; never used as a production fallback."""
    return LabExecutionPolicy.from_settings(settings)


# Explicit whole-graph fixtures; a006 and actual retained worker privileges must
# qualify before these hosted canaries are run. No create_all or graph shortcut.

assert lab_policy_rows  # Register the genuine Settings cleanup fixture.


def _small_policy(policy):
    return replace(
        policy,
        lab_command_timeout_seconds_by_action={"inspect": 1, "deploy": 1, "destroy": 1, "check": 1},
        # Strictly above the policy relation bounds (app/config.py validate_lab_execution_relations):
        # deploy 4, destroy 2*inspect + destroy + 2*grace = 5, check 3. Destroy needs > 5.
        lab_operation_lease_seconds_by_kind={"deploy": 5, "destroy": 6, "check": 5},
        lab_operation_unknown_lease_seconds=5,
        lab_process_termination_grace_seconds=1,
    )


def _wait_for_actual_blocker(observer, follower_pid, blocker_pid):
    deadline = time.monotonic() + 10
    while blocker_pid not in observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": follower_pid}):
        assert time.monotonic() < deadline, "no genuine fenced row-lock wait"
        time.sleep(0.01)


def test_policy_shrink_does_not_expire_an_old_grant_and_next_claim_uses_new_policy(
    lab_policy_rows, tenant_a, admin_engine
):
    import json

    db = lab_policy_rows
    instance, person = _seed(db, tenant_a.id, name="policy-shrink")
    old = settings_store.execution_policy(db)
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    claimed = lab_operations.claim_next(db, claimed_by="old", policy=old)
    assert claimed.id == op.id
    assert claimed.claim_lease_seconds == 1200
    assert old.lab_command_timeout_seconds_by_action["deploy"] == 900
    operation_id = op.id
    db.commit()
    with sessionmaker(bind=admin_engine)() as writer:
        settings_store.set_many(
            writer,
            {
                "lab_execution_policy_overrides": json.dumps(
                    {
                        "lab_command_timeout_seconds_by_action": {
                            "inspect": 2,
                            "deploy": 30,
                            "destroy": 20,
                            "check": 20,
                        },
                        "lab_operation_lease_seconds_by_kind": {"deploy": 100, "destroy": 100, "check": 100},
                    }
                )
            },
        )
        writer.commit()
    new = settings_store.execution_policy(db)
    assert new.lease_seconds("deploy") == 100
    db.execute(
        update(LabOperation)
        .where(LabOperation.id == operation_id)
        .values(heartbeat_at=func.clock_timestamp() - text("interval '101 seconds'"))
    )
    db.commit()
    assert lab_operations.reconcile_stuck(db, policy=new) == 0
    db.commit()
    assert lab_operations.heartbeat(db, operation_id=operation_id, claimed_by="old") is True
    db.commit()
    db.expire_all()
    assert db.get(LabOperation, operation_id).claim_lease_seconds == 1200
    assert old.lease_seconds("deploy") == 1200
    assert old.lab_command_timeout_seconds_by_action["deploy"] == 900
    db.execute(
        update(LabOperation)
        .where(LabOperation.id == operation_id)
        .values(heartbeat_at=func.clock_timestamp() - text("interval '1201 seconds'"))
    )
    db.commit()
    assert lab_operations.reconcile_stuck(db, policy=new) == 1
    db.commit()
    assert db.get(LabOperation, operation_id).claim_lease_seconds is None
    next_claim = lab_operations.claim_next(db, claimed_by="new", policy=new)
    assert next_claim.id == operation_id
    assert next_claim.claim_lease_seconds == 100
    assert next_claim.attempts == 2
    db.rollback()


def test_policy_growth_cannot_extend_an_existing_short_grant(admin_session, tenant_a, policy):
    db = admin_session
    instance, person = _seed(db, tenant_a.id, name="policy-growth")
    short = _small_policy(policy)
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    lab_operations.claim_next(db, claimed_by="old-short", policy=short)
    db.commit()
    db.execute(
        update(LabOperation)
        .where(LabOperation.id == op.id)
        .values(heartbeat_at=func.clock_timestamp() - text("interval '6 seconds'"))
    )
    db.commit()
    assert lab_operations.reconcile_stuck(db, policy=policy) == 1
    db.commit()
    db.refresh(op)
    assert op.state == "queued"
    assert op.claim_lease_seconds is None
    assert op.attempts == 1


def test_heartbeat_samples_clock_after_actual_unchanged_row_lock_and_keeps_grant(
    admin_session, admin_engine, tenant_a, policy
):
    db = admin_session
    small = _small_policy(policy)
    instance, person = _seed(db, tenant_a.id, name="post-lock-clock")
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    lab_operations.claim_next(db, claimed_by="renew", policy=small)
    operation_id = op.id
    db.commit()
    factory = sessionmaker(bind=admin_engine, autoflush=False)
    started = Event()
    follower_pids = []
    with factory() as holder:
        row = holder.scalars(select(LabOperation).where(LabOperation.id == operation_id).with_for_update()).one()
        before = row.heartbeat_at
        holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))

        def refresh():
            with factory() as worker:
                follower_pids.append(worker.scalar(text("SELECT pg_backend_pid()")))
                started.set()
                assert lab_operations.heartbeat(worker, operation_id=operation_id, claimed_by="renew") is True
                worker.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(refresh)
            assert started.wait(10)
            try:
                _wait_for_actual_blocker(db, follower_pids[0], holder_pid)
                # Prove a live row lock is held unchanged across the actual
                # DB-clock lease expiry, not merely across a guessed host sleep.
                until = time.monotonic() + 10
                while _db_now(db) <= before + timedelta(seconds=5):
                    assert time.monotonic() < until
                    time.sleep(0.01)
                _wait_for_actual_blocker(db, follower_pids[0], holder_pid)
                released_after = _db_now(db)
            finally:
                holder.commit()  # No row mutation; waiting refresh still must sample later.
            future.result(timeout=15)
    db.expire_all()
    renewed = db.get(LabOperation, operation_id)
    assert renewed.heartbeat_at >= released_after
    assert renewed.claim_lease_seconds == 5
    assert lab_operations.reconcile_stuck(db, policy=policy) == 0
    db.commit()
    # The next protected command starts only after the real refresh succeeded.
    engine = _engine(instance.instance_name, policy=small)
    assert (
        lab_operations.run_claimed(db, operation_id=operation_id, claimed_by="renew", engine=engine, policy=small)
        == "succeeded"
    )
    db.refresh(renewed)
    assert renewed.claim_lease_seconds == 5  # Terminal audit retains the original grant.
    engine.deploy.assert_called_once()


@pytest.mark.parametrize("winner", ["renewal", "reclaim"])
def test_real_row_lock_orders_renewal_and_reclaim_and_preserves_stale_fences(
    admin_session, admin_engine, tenant_a, policy, winner
):
    db = admin_session
    small = _small_policy(policy)
    instance, person = _seed(db, tenant_a.id, name=f"lease-race-{winner}")
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    lab_operations.claim_next(db, claimed_by="old", policy=small)
    operation_id = op.id
    db.commit()
    db.execute(
        update(LabOperation)
        .where(LabOperation.id == operation_id)
        .values(heartbeat_at=func.clock_timestamp() - text("interval '6 seconds'"))
    )
    db.commit()
    factory = sessionmaker(bind=admin_engine, autoflush=False)
    if winner == "renewal":
        with factory() as leader, factory() as recovery:
            assert lab_operations.heartbeat(leader, operation_id=operation_id, claimed_by="old") is True
            assert lab_operations.reconcile_stuck(recovery, policy=policy) == 0  # SKIP LOCKED.
            recovery.commit()
            leader.commit()
            assert lab_operations.reconcile_stuck(recovery, policy=policy) == 0
            recovery.commit()
        db.expire_all()
        assert db.get(LabOperation, operation_id).claim_lease_seconds == 5
        assert db.get(LabOperation, operation_id).claimed_by == "old"
    else:
        started, pids = Event(), []
        with factory() as leader:
            assert lab_operations.reconcile_stuck(leader, policy=policy) == 1
            leader_pid = leader.scalar(text("SELECT pg_backend_pid()"))

            def stale_refresh():
                with factory() as follower:
                    pids.append(follower.scalar(text("SELECT pg_backend_pid()")))
                    started.set()
                    alive = lab_operations.heartbeat(follower, operation_id=operation_id, claimed_by="old")
                    follower.commit()
                    return alive

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(stale_refresh)
                assert started.wait(10)
                try:
                    _wait_for_actual_blocker(db, pids[0], leader_pid)
                finally:
                    leader.commit()
                assert future.result(timeout=15) is False
        db.expire_all()
        engine = _engine(instance.instance_name, policy=small)
        assert (
            lab_operations.run_claimed(db, operation_id=operation_id, claimed_by="old", engine=engine, policy=small)
            == "stale"
        )
        engine.destroy.assert_not_called()
        engine.deploy.assert_not_called()
        assert lab_operations._settle(db, operation_id=operation_id, claimed_by="old", state="succeeded") is False
        db.rollback()
        assert db.get(LabOperation, operation_id).claim_lease_seconds is None
        assert db.get(LabOperation, operation_id).attempts == 1


def test_policy_stamp_or_engine_identity_mismatch_refuses_before_external_work(admin_session, tenant_a, policy):
    db = admin_session
    instance, person = _seed(db, tenant_a.id, name="mismatched-policy")
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    lab_operations.claim_next(db, claimed_by="owner", policy=policy)
    operation_id = op.id
    db.commit()
    other = replace(policy, lab_worker_poll_interval_seconds=9)
    engine = _engine(instance.instance_name, policy=other)
    with pytest.raises(ValueError, match="granted policy"):
        lab_operations.run_claimed(db, operation_id=operation_id, claimed_by="owner", engine=engine, policy=policy)
    engine.destroy.assert_not_called()
    engine.deploy.assert_not_called()
    db.rollback()
    engine = _engine(instance.instance_name, policy=_small_policy(policy))
    with pytest.raises(ValueError, match="granted policy"):
        lab_operations.run_claimed(
            db, operation_id=operation_id, claimed_by="owner", engine=engine, policy=engine.execution_policy
        )
    engine.destroy.assert_not_called()
    db.rollback()


def test_unknown_kind_policy_fallback_and_granted_lease_stamp(policy):
    # ck_lab_operations_kind admits only deploy/destroy/check, so an unknown-kind
    # row cannot exist in the canonical database. The fallback is therefore
    # proved on the policy and on the in-memory stamp reader, with no DB row.
    # claim_next and reconcile_stuck behaviour is NOT exercised by this test.
    custom = replace(_small_policy(policy), lab_operation_unknown_lease_seconds=7, lab_operation_unknown_max_attempts=2)
    # Source reference only (not exercised here): lab_operations.py reads
    # policy.lease_seconds(op.kind) and policy.max_attempts(op.kind).
    assert custom.lease_seconds("legacy-unknown") == 7
    assert custom.max_attempts("legacy-unknown") == 2
    for kind in ("deploy", "destroy", "check"):
        assert custom.lease_seconds(kind) == custom.lab_operation_lease_seconds_by_kind[kind]
        assert custom.max_attempts(kind) == custom.lab_operation_max_attempts_by_kind[kind]
    # The granted lease is retained on the operation: a later policy change to
    # the fallback never rewrites a stamp already granted.
    op = LabOperation(kind="legacy-unknown", state="claimed", claim_lease_seconds=custom.lease_seconds("legacy-unknown"))
    changed = replace(custom, lab_operation_unknown_lease_seconds=11)
    assert changed.lease_seconds(op.kind) == 11
    assert lab_operations._claimed_duration(op) == 7


def test_invalid_claim_duration_refuses_without_guessing(policy):
    for duration in (None, True, 1.5, 0, -1, 2147483648):
        with pytest.raises(ValueError, match="granted lease"):
            lab_operations._claimed_duration(LabOperation(claim_lease_seconds=duration))


def test_nondefault_retry_capacity_and_attempt_policy_reaches_actual_worker_paths(
    admin_session, tenant_a, policy, monkeypatch
):
    db = admin_session
    custom = replace(
        _small_policy(policy),
        max_concurrent_labs=1,
        lab_operation_retry_delay_seconds=13,
        lab_operation_max_attempts_by_kind={"deploy": 1, "destroy": 2, "check": 1},
    )
    _seed(db, tenant_a.id, name="policy-capacity-busy", status="active", presence="present")
    instance, person = _seed(db, tenant_a.id, name="policy-capacity-pending", presence="absent")
    op = lab_operations.enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    lab_operations.claim_next(db, claimed_by="custom", policy=custom)
    operation_id = op.id
    db.commit()
    now = _db_now(db)
    monkeypatch.setattr(lab_operations, "_now", lambda: now)
    engine = _engine(instance.instance_name, policy=custom)
    assert (
        lab_operations.run_claimed(db, operation_id=operation_id, claimed_by="custom", engine=engine, policy=custom)
        == "deferred"
    )
    db.refresh(op)
    assert op.not_before == now + timedelta(seconds=13)
    assert op.attempts == 0
    assert op.claim_lease_seconds is None
    engine.destroy.assert_not_called()
    db.execute(update(LabOperation).where(LabOperation.id == operation_id).values(not_before=func.clock_timestamp()))
    db.commit()
    claimed = lab_operations.claim_next(db, claimed_by="crashed", policy=custom)
    assert claimed.id == operation_id and claimed.attempts == 1
    db.commit()
    db.execute(
        update(LabOperation)
        .where(LabOperation.id == operation_id)
        .values(heartbeat_at=func.clock_timestamp() - text("interval '6 seconds'"))
    )
    db.commit()
    assert lab_operations.reconcile_stuck(db, policy=custom) == 1
    db.refresh(op)
    assert op.state == "failed"
    assert op.attempts == 1
    assert op.claim_lease_seconds == 5
    db.rollback()


def test_restart_reclaim_lock_timeout_is_policy_bound_and_transaction_local(admin_session, policy):
    db = admin_session
    before = db.scalar(text("SELECT current_setting('lock_timeout')"))
    custom = replace(policy, lab_restart_reclaim_lock_timeout_seconds=1)
    assert lab_operations.reclaim_previous_epoch(db, host="no-such-policy-test-host", epoch="new", policy=custom) == 0
    assert db.scalar(text("SELECT current_setting('lock_timeout')")) == "1s"
    db.rollback()
    assert db.scalar(text("SELECT current_setting('lock_timeout')")) == before


def test_refresh_claim_sets_local_lock_timeout_before_heartbeat(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(lab_operations, "Session", lambda *_args: session)
    heartbeat_db = session.__enter__.return_value
    order = []
    heartbeat_db.execute.side_effect = lambda statement: order.append(str(statement))
    monkeypatch.setattr(lab_operations, "heartbeat", lambda *_a, **_k: order.append("heartbeat") or True)
    lab_operations._refresh_claim(MagicMock(), operation_id=uuid4(), claimed_by="worker")
    assert order == ["SET LOCAL lock_timeout = '5s'", "heartbeat"]
    heartbeat_db.commit.assert_called_once_with()


def test_grade_compares_integer_totals_at_float_persistence_boundary(monkeypatch):
    from app.services.lab_result_capability import LabGradeReceipt

    total = 2**53 + 1
    receipt = LabGradeReceipt(uuid4(), uuid4(), float(total), float(total), True)
    instance = MagicMock(status="active", error=None, seed={}, last_active_at=None)
    template = MagicMock(checks=[], limits={"pass_threshold": 1})
    monkeypatch.setattr(lab_lifecycle, "run_checks", lambda *_a, **_k: {
        "score": total, "max_score": total, "per_check": []})
    monkeypatch.setattr(lab_lifecycle, "record_claimed_lab_check", lambda *_a, **_k: receipt)
    assert lab_lifecycle.grade(MagicMock(), instance, MagicMock(), template, MagicMock(),
                               operation_id=uuid4(), claimed_by="worker") == receipt


@pytest.mark.parametrize("total", [float("nan"), float("inf"), float("-inf")])
def test_grade_matches_nonfinite_float_classes_exactly(monkeypatch, total):
    from app.services.lab_result_capability import LabGradeReceipt

    receipt = LabGradeReceipt(uuid4(), uuid4(), total, total, False)
    instance = MagicMock(status="active", error=None, seed={}, last_active_at=None)
    template = MagicMock(checks=[], limits={"pass_threshold": 0.5})
    monkeypatch.setattr(lab_lifecycle, "run_checks", lambda *_a, **_k: {
        "score": total, "max_score": total, "per_check": []})
    monkeypatch.setattr(lab_lifecycle, "record_claimed_lab_check", lambda *_a, **_k: receipt)
    assert lab_lifecycle.grade(MagicMock(), instance, MagicMock(), template, MagicMock(),
                               operation_id=uuid4(), claimed_by="worker") == receipt
