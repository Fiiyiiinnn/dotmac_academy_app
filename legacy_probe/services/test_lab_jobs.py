"""TDD tests for the cross-tenant lab worker/reaper jobs (Task 7).

These jobs OWN their transaction (they ``commit``). The tests pass the conftest
``admin_session`` (a BYPASSRLS Session) directly into the functions rather than
opening a new admin connection, and clean up by letting ``tenant_a`` CASCADE.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning import ActivityWrite, CourseWrite, create_activity, create_course
from sqlalchemy import select

from app.config import LabExecutionPolicy, settings
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.services import lab_jobs, lab_operations
from app.services.labengine.interface import LabHandle


def _seed(db, tid):
    c = create_course(
        db,
        CourseWrite(
            id=uuid4(),
            tenant_id=tid,
            slug="foundation",
            title="F",
            discipline="networking",
            source_ref="x",
            version=1,
            status="published",
            listed=False,
        ),
    )
    act = create_activity(
        db,
        ActivityWrite(
            id=uuid4(),
            tenant_id=tid,
            course_id=c.id,
            chapter_number=14,
            type="lab",
            title="VLAN Lab",
            pass_threshold=0.6,
            assessment_mode="graded",
            grading="auto",
            weight=1.0,
        ),
    )
    lt = LabTemplate(
        tenant_id=tid,
        course_id=c.id,
        chapter_number=14,
        activity_id=act.id,
        slug="vlan",
        title="VLAN",
        topology="name: x {{o}}",
        instructions_html="<p>do</p>",
        checks=[{"id": "c1", "type": "probe"}],
        seed_spec={"o": {"type": "int", "min": 2, "max": 9}},
        limits={"time_minutes": 45},
    )
    db.add(lt)
    db.flush()
    p = Party(
        tenant_id=tid, party_type=PartyType.person, display_name="T U", email="trainee@example.com", is_active=True
    )
    db.add(p)
    db.flush()
    db.add(PartyPerson(party_id=p.id, first_name="T", last_name="U"))
    db.flush()
    return c, act, lt, p


def test_worker_session_refuses_a_non_worker_dsn(
    monkeypatch,
    policy,
):
    monkeypatch.setattr(
        settings,
        "lab_worker_database_url",
        "postgresql+psycopg://postgres@db/academy",
    )
    with pytest.raises(RuntimeError, match="must authenticate as academy_lab_worker"):
        with lab_jobs.lab_worker_session():
            pytest.fail("must refuse before opening a session")


def test_worker_session_refuses_worker_without_bypassrls(
    monkeypatch,
    policy,
):
    """A live worker role that has lost BYPASSRLS must fail loudly, not go quiet.

    Regression coverage for the guard added alongside the ``current_user``
    check: ``lab_operations`` has FORCE ROW LEVEL SECURITY, so a worker
    connection without BYPASSRLS would otherwise silently see an empty queue
    instead of raising. This mocks the DB round-trips so it never needs a live
    Postgres connection — only the URL-username prefilter is real.
    """
    monkeypatch.setattr(
        settings,
        "lab_worker_database_url",
        "postgresql+psycopg://academy_lab_worker@db/academy",
    )
    fake_session = MagicMock()
    fake_session.scalar.side_effect = ["academy_lab_worker", False]
    monkeypatch.setattr(lab_jobs, "create_engine", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        lab_jobs,
        "DatabaseRuntime",
        lambda *, engine: SimpleNamespace(session_factory=lambda: fake_session),
    )

    with pytest.raises(RuntimeError, match="does not have BYPASSRLS"):
        with lab_jobs.lab_worker_session():
            pytest.fail("must refuse before yielding a session")
    assert fake_session.scalar.call_count == 2
    fake_session.close.assert_called_once()


def test_worker_session_refuses_worker_that_owns_application_objects(
    monkeypatch,
    policy,
):
    monkeypatch.setattr(
        settings,
        "lab_worker_database_url",
        "postgresql+psycopg://academy_lab_worker@db/academy",
    )
    fake_session = MagicMock()
    fake_session.scalar.side_effect = ["academy_lab_worker", True, True, True]
    monkeypatch.setattr(lab_jobs, "create_engine", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        lab_jobs,
        "DatabaseRuntime",
        lambda *, engine: SimpleNamespace(session_factory=lambda: fake_session),
    )

    with pytest.raises(RuntimeError, match="must not own"):
        with lab_jobs.lab_worker_session():
            pytest.fail("must refuse an owning worker role")
    fake_session.close.assert_called_once()


def test_worker_session_refuses_unsafe_role_attributes(
    monkeypatch,
    policy,
):
    monkeypatch.setattr(
        settings,
        "lab_worker_database_url",
        "postgresql+psycopg://academy_lab_worker@db/academy",
    )
    fake_session = MagicMock()
    fake_session.scalar.side_effect = ["academy_lab_worker", True, False]
    monkeypatch.setattr(lab_jobs, "create_engine", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        lab_jobs,
        "DatabaseRuntime",
        lambda *, engine: SimpleNamespace(session_factory=lambda: fake_session),
    )

    with pytest.raises(RuntimeError, match="unsafe attributes or role memberships"):
        with lab_jobs.lab_worker_session():
            pytest.fail("must refuse unsafe role posture")
    fake_session.close.assert_called_once()


def test_drain_once_provisions_pending(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    _c, act, lt, p = _seed(admin_session, tenant_a.id)
    monkeypatch.setattr(settings, "max_concurrent_labs", 20)
    policy = LabExecutionPolicy.from_settings(settings)
    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-drain",
        seed={"o": 5},
        status="queued",
        consoles={},
    )
    admin_session.add(inst)
    admin_session.flush()
    lab_operations.enqueue(admin_session, instance=inst, kind="deploy", requested_by=p.id)
    admin_session.commit()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.deploy.return_value = LabHandle(
        instance_name="dal-drain",
        nodes={"client": "clab-x-client"},
        mgmt={"client": "172.20.20.3"},
        kinds={"client": "linux"},
    )

    n = lab_jobs.drain_once(
        admin_session,
        engine,
        policy=policy,
    )

    assert n == 1
    admin_session.refresh(inst)
    assert inst.status == "active"
    engine.deploy.assert_called_once()


def test_drain_once_default_identity_persists_structural_claim_ownership(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """When ``drain_once`` is left to compute its own identity (the CLI's
    production path, `claimed_by=` omitted), the settled operation's
    ``claimed_by`` matches ``worker_identity()`` exactly and
    ``claimed_host``/``claimed_epoch`` are populated from the same identity —
    retained as audit provenance by a terminal settlement, not cleared."""
    _c, act, lt, p = _seed(admin_session, tenant_a.id)
    monkeypatch.setattr(settings, "max_concurrent_labs", 20)
    policy = LabExecutionPolicy.from_settings(settings)
    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-drain-identity",
        seed={"o": 5},
        status="queued",
        consoles={},
    )
    admin_session.add(inst)
    admin_session.flush()
    operation = lab_operations.enqueue(admin_session, instance=inst, kind="deploy", requested_by=p.id)
    admin_session.commit()
    operation_id = operation.id

    identity = lab_operations.worker_identity_parts()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.deploy.return_value = LabHandle(
        instance_name="dal-drain-identity",
        nodes={"client": "clab-x-client"},
        mgmt={"client": "172.20.20.3"},
        kinds={"client": "linux"},
    )

    assert (
        lab_jobs.drain_once(
            admin_session,
            engine,
            policy=policy,
        )
        == 1
    )

    settled = admin_session.scalar(select(LabOperation).where(LabOperation.id == operation_id))
    assert settled.claimed_by == lab_operations.worker_identity()
    assert settled.claimed_host == identity.host
    assert settled.claimed_epoch == identity.epoch


def test_reap_idle_enqueues_destroy_without_touching_engine(
    admin_session,
    tenant_a,
    policy,
):
    _c, act, lt, p = _seed(admin_session, tenant_a.id)
    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-reap",
        seed={"o": 5},
        status="active",
        consoles={},
        last_active_at=datetime.now(UTC) - timedelta(minutes=999),
    )
    admin_session.add(inst)
    admin_session.commit()

    n = lab_jobs.request_idle_reaps(admin_session)

    assert n == 1
    admin_session.refresh(inst)
    assert inst.status == "active"
    operation = admin_session.query(LabOperation).filter_by(instance_id=inst.id).one()
    assert operation.kind == "destroy"
    assert operation.state == "queued"


def test_sweep_kills_consoles_whose_instance_is_not_live(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The backstop: a console outliving its instance gets reaped on the timer."""
    _c, act, lt, p = _seed(admin_session, tenant_a.id)
    live = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-live",
        seed={"o": 5},
        status="active",
        consoles={},
    )
    live.runtime_presence = "present"
    dead = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-dead",
        seed={"o": 5},
        status="reaped",
        consoles={},
    )
    dead.runtime_presence = "absent"
    admin_session.add_all([live, dead])
    admin_session.flush()

    monkeypatch.setattr(
        lab_jobs,
        "console_pids",
        lambda: {
            str(live.id): [111],
            str(dead.id): [222, 333],
            "11111111-2222-3333-4444-555555555555": [444],  # no row at all
        },
    )
    killed = []
    monkeypatch.setattr(lab_jobs, "kill_consoles", lambda pids: killed.extend(pids) or len(killed))

    assert lab_jobs.sweep_orphan_consoles(admin_session) == 3
    assert sorted(killed) == [222, 333, 444]  # the live instance's console survives
    admin_session.rollback()


def test_sweep_is_a_no_op_when_no_consoles_are_running(
    admin_session,
    monkeypatch,
    policy,
):
    monkeypatch.setattr(lab_jobs, "console_pids", lambda: {})
    monkeypatch.setattr(lab_jobs, "kill_consoles", lambda pids: pytest.fail("must not kill anything"))
    assert lab_jobs.sweep_orphan_consoles(admin_session) == 0


def test_recover_missing_consoles_starts_only_missing_linux_nodes_and_is_idempotent(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name=f"dal-{uuid4()}",
        seed={},
        status="active",
        runtime_presence="present",
        consoles={
            "client": {"kind": "linux", "mgmt": "10.0.0.1", "port": 40000},
            "server": {"kind": "linux", "mgmt": "10.0.0.2", "port": 40001},
            "r1": {"kind": "vr-vmx", "mgmt": "10.0.0.3"},
        },
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={
            "client": "clab-x-client",
            "server": "clab-x-server",
            "r1": "clab-x-r1",
        },
        mgmt={"client": "172.20.0.1", "server": "172.20.0.2", "r1": "172.20.0.3"},
        kinds={"client": "linux", "server": "linux", "r1": "vr-vmx"},
    )
    processes = {
        str(instance.id): {"server": [(222, 41001)]},
    }
    monkeypatch.setattr(lab_jobs, "console_processes", lambda: processes)
    starts = []
    monkeypatch.setattr(
        lab_jobs,
        "start_console",
        lambda cname, path: starts.append((cname, path)) or 41000,
    )

    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 1
    )
    assert starts == [("clab-x-client", f"/labs/instances/{instance.id}/console/client")]
    assert instance.consoles == {
        "client": {"kind": "linux", "mgmt": "172.20.0.1", "port": 41000},
        "server": {"kind": "linux", "mgmt": "172.20.0.2", "port": 41001},
        "r1": {"kind": "vr-vmx", "mgmt": "172.20.0.3"},
    }

    processes[str(instance.id)]["client"] = [(111, 41000)]
    engine.reset_mock()
    starts.clear()
    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 0
    )
    engine.inspect_running.assert_not_called()
    assert starts == []


def test_recover_missing_consoles_excludes_non_live_and_open_instances(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)

    def _instance(*, status="active", presence="present"):
        row = LabInstance(
            tenant_id=tenant_a.id,
            activity_id=act.id,
            person_id=p.id,
            instance_name=f"dal-{uuid4()}",
            seed={},
            status=status,
            runtime_presence=presence,
            consoles={"client": {"kind": "linux", "port": 40000}},
        )
        admin_session.add(row)
        admin_session.flush()
        return row

    _instance(presence="absent")
    _instance(presence="unknown")
    _instance(status="error")
    open_instance = _instance()
    lab_operations.enqueue(admin_session, instance=open_instance, kind="check", requested_by=p.id)
    monkeypatch.setattr(lab_jobs, "console_processes", lambda: {})
    engine = MagicMock(
        execution_policy=policy,
    )

    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 0
    )
    engine.inspect_running.assert_not_called()


def test_recover_missing_consoles_scan_failure_never_spawns_duplicates(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.1", "port": 40000}}
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name=f"dal-{uuid4()}",
        seed={},
        status="active",
        runtime_presence="present",
        consoles=dict(consoles),
    )
    admin_session.add(instance)
    admin_session.flush()
    monkeypatch.setattr(lab_jobs, "console_processes", lambda: None)
    engine = MagicMock(
        execution_policy=policy,
    )
    starts = MagicMock()
    monkeypatch.setattr(lab_jobs, "start_console", starts)

    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 0
    )
    engine.inspect_running.assert_not_called()
    starts.assert_not_called()
    assert instance.consoles == consoles


@pytest.mark.parametrize("inspection", [None, RuntimeError("inspect failed")])
def test_recover_missing_consoles_never_mutates_without_positive_inspection(
    admin_session,
    tenant_a,
    monkeypatch,
    inspection,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    consoles = {"client": {"kind": "linux", "mgmt": "10.0.0.1", "port": 40000}}
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name=f"dal-{uuid4()}",
        seed={},
        status="active",
        runtime_presence="present",
        consoles=dict(consoles),
    )
    admin_session.add(instance)
    admin_session.flush()
    monkeypatch.setattr(lab_jobs, "console_processes", lambda: {})
    starts = MagicMock()
    monkeypatch.setattr(lab_jobs, "start_console", starts)
    engine = MagicMock(
        execution_policy=policy,
    )
    if isinstance(inspection, Exception):
        engine.inspect_running.side_effect = inspection
    else:
        engine.inspect_running.return_value = inspection

    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 0
    )
    assert instance.consoles == consoles
    starts.assert_not_called()


def test_recover_missing_consoles_retries_a_failed_ttyd_launch(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name=f"dal-{uuid4()}",
        seed={},
        status="active",
        runtime_presence="present",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    monkeypatch.setattr(lab_jobs, "console_processes", lambda: {})
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inspect_running.return_value = LabHandle(
        instance_name=instance.instance_name,
        nodes={"client": "clab-x-client", "r1": "clab-x-r1"},
        mgmt={"client": "172.20.0.1", "r1": "172.20.0.2"},
        kinds={"client": "linux", "r1": "vr-vmx"},
    )
    ports = iter([None, 42000])
    monkeypatch.setattr(lab_jobs, "start_console", lambda *a: next(ports))

    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 0
    )
    assert instance.consoles["client"]["port"] is None
    assert "port" not in instance.consoles["r1"]
    assert (
        lab_jobs.recover_missing_consoles(
            admin_session,
            engine,
            policy=policy,
        )
        == 1
    )
    assert instance.consoles["client"]["port"] == 42000


def test_runtime_reconcile_queues_destroy_for_reaped_row_with_live_runtime(
    admin_session,
    tenant_a,
    policy,
):
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-runtime-leak",
        seed={},
        status="reaped",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {instance.instance_name: "/labs/leak.clab.yml"}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    # The reconciler no longer projects repaired lifecycle state itself —
    # only the worker's own locked, authoritative recheck
    # (lab_lifecycle.destroy_if_present) may settle status/runtime_presence.
    # status/error stay exactly as they were at snapshot time.
    assert instance.status == "reaped"
    assert instance.error is None
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "destroy"
    assert operation.origin == "runtime_cleanup"
    assert operation.runtime_precondition == "present"
    engine.destroy.assert_not_called()
    admin_session.rollback()


def test_runtime_reconcile_destroys_only_rowless_academy_labs(
    admin_session,
    tenant_a,
    policy,
):
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {
        "dal-rowless": "/labs/rowless.clab.yml",
        "shared-infrastructure": "/labs/shared.clab.yml",
    }

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (0, 1)
    engine.destroy.assert_called_once_with("dal-rowless")
    admin_session.rollback()


def test_runtime_reconcile_queues_replay_for_live_row_without_runtime(
    admin_session,
    tenant_a,
    policy,
):
    """The reconciler enqueues conditional repair INTENT only (origin=
    "runtime_repair", runtime_precondition="absent") — it no longer projects
    status/error directly onto the instance; only the worker's own locked,
    authoritative recheck may decide the outcome. runtime_presence IS
    updated: this pass's own fresh, locked inventory scan just confirmed
    absence, which is an observed fact, not a projected decision."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-runtime-missing",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "deploy"
    assert operation.origin == "runtime_repair"
    assert operation.runtime_precondition == "absent"
    assert instance.error is None
    assert instance.status == "active"
    assert instance.runtime_presence == "absent"
    admin_session.rollback()


def test_runtime_reconcile_releases_confirmed_absent_degraded_capacity(
    admin_session,
    tenant_a,
    policy,
):
    """A prior error no longer bypasses the worker's own conditional recheck
    by settling status="error" directly from this pass's snapshot — it must
    ALSO go through the same conditional enqueue() as any other
    missing-runtime instance. Only the worker's own locked, authoritative
    observation may decide status/error; the reconciler itself must not
    touch either. runtime_presence IS updated from this pass's own fresh,
    locked observation of absence."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-runtime-confirmed-absent",
        seed={},
        status="active",
        error="worker lease expired after 3 attempts",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    assert instance.status == "active"
    assert instance.error == "worker lease expired after 3 attempts"
    assert instance.runtime_presence == "absent"
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "deploy"
    assert operation.origin == "runtime_repair"
    assert operation.runtime_precondition == "absent"
    admin_session.rollback()


def test_runtime_reconcile_does_not_act_on_a_snapshot_made_stale_by_a_concurrent_reset(
    admin_session,
    tenant_a,
    policy,
):
    """Phase 4 must re-validate, not trust, Phase 2's snapshot.

    ``missing_runtime`` is computed once, in Phase 2, before Phase 3's
    (potentially multi-minute, per orphan) destroy loop runs. If a real,
    concurrent, user-initiated reset completes for one of those instances
    while Phase 3 is still running, Phase 4 must not enqueue a destroy
    against it using the now-stale Phase 2 snapshot — that would tear down a
    lab that just came back up.

    The concurrent mutation is injected via ``engine.destroy``'s side effect
    for the one rowless orphan this test also seeds — ``engine.destroy`` is
    the last thing Phase 3 does before Phase 4 runs, so performing the
    "concurrent" enqueue from inside it deterministically reproduces "some
    real time and a real database write happened between Phase 2 and Phase
    4" without needing an actual second thread or process.
    """
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-race-missing-runtime",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()

    def _concurrent_reset_during_phase_3(name: str) -> None:
        assert name == "dal-rowless-race-trigger"
        # Simulate a real worker completing an unrelated, concurrent
        # user-initiated redeploy for `instance` while this destroy is
        # "in flight" — it now has a genuinely open operation.
        lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=p.id)
        admin_session.flush()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {
        "dal-rowless-race-trigger": "/labs/rowless-race-trigger.clab.yml",
    }
    engine.destroy.side_effect = _concurrent_reset_during_phase_3

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 1
    engine.destroy.assert_called_once_with("dal-rowless-race-trigger")
    # The concurrent deploy is the only operation queued for `instance` — no
    # destroy/deploy was ALSO enqueued against the stale Phase 2 snapshot.
    ops = admin_session.query(LabOperation).filter_by(instance_id=instance.id).all()
    assert [op.kind for op in ops] == ["deploy"]
    assert queued == 0
    # Untouched by Phase 4 — it was skipped, not mutated.
    assert instance.status == "active"
    assert instance.error is None
    admin_session.rollback()


def test_runtime_reconcile_does_not_claim_a_destroy_that_lost_the_enqueue_race(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The status/open-op pre-check narrows the race, it does not close it.

    A concurrent enqueue can still land in the gap between
    ``fresh_open_instance_ids``'s query and this exact instance's ``enqueue()``
    call further down the same loop iteration — the pre-check has already
    passed by then. The only correctness guarantee is ``enqueue()``'s own
    atomic return value (backed by the database's partial unique index):
    if it hands back an operation of a different kind than requested, a
    genuinely concurrent operation won, and this pass must not claim its own
    destroy was enqueued.

    The race is reproduced faithfully — through the real ``on_conflict_do_
    nothing`` + re-select path in ``lab_operations.enqueue``, not a mock —
    by wrapping ``lab_operations.enqueue`` so that its first invocation
    inserts a genuinely competing ``"deploy"`` operation for this instance
    immediately before letting the real, intended ``"destroy"`` enqueue
    call proceed and lose that race.
    """
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-race-destroy-enqueue",
        seed={},
        status="reaped",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {instance.instance_name: "/labs/race.clab.yml"}

    real_enqueue = lab_operations.enqueue
    calls = {"n": 0}

    def _enqueue_with_late_concurrent_winner(
        db, *, instance, kind, requested_by, origin=None, runtime_precondition=None
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # A real, concurrent user-initiated reset's enqueue landing
            # after reconcile_runtime's fresh_open_instance_ids query
            # already ran (and found nothing) for this instance, but
            # before reconcile_runtime's own enqueue() call for it below.
            real_enqueue(db, instance=instance, kind="deploy", requested_by=p.id)
        # Forward origin/runtime_precondition unchanged: this pass's own
        # intended destroy enqueue call now passes them (item 4's
        # conditional-operations design), and the whole point of this test
        # is to reproduce the race through the REAL enqueue()/on_conflict
        # machinery, not a mock — silently dropping them here would test a
        # call shape reconcile_runtime no longer makes.
        return real_enqueue(
            db,
            instance=instance,
            kind=kind,
            requested_by=requested_by,
            origin=origin,
            runtime_precondition=runtime_precondition,
        )

    monkeypatch.setattr(lab_operations, "enqueue", _enqueue_with_late_concurrent_winner)

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 0
    assert queued == 0
    # The concurrent deploy is the only operation that actually exists — no
    # destroy was ALSO created, and none of this pass's destroy-enqueued
    # fields were written despite losing the race.
    ops = admin_session.query(LabOperation).filter_by(instance_id=instance.id).all()
    assert [op.kind for op in ops] == ["deploy"]
    assert instance.status == "reaped"
    assert instance.error is None
    admin_session.rollback()


def test_runtime_reconcile_does_not_redeploy_an_instance_whose_runtime_reappeared_during_phase_3(
    admin_session,
    tenant_a,
    policy,
):
    """Phase 1's runtime snapshot can go stale exactly like Phase 2's database
    snapshot did in earlier rounds — a concurrent deploy elsewhere can SETTLE
    (not merely get enqueued; that race is covered by the previous test)
    while Phase 3's destroy loop is still running, making an instance no
    longer "missing" by the time Phase 4 needs to decide. Phase 4 must
    re-verify against a fresh, second ``engine.inventory()`` call, not
    Phase 1's now-stale one.
    """
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-race-fresh-inventory",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    # A concurrent worker's deploy for this instance has already SETTLED by
    # the time Phase 4 runs (a terminal state, not an open one) — the
    # instance is genuinely running again, not merely claimed.
    settled = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="deploy",
        state="succeeded",
        requested_by=p.id,
    )
    admin_session.add(settled)
    admin_session.flush()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.side_effect = [
        {},  # Phase 1: genuinely missing at snapshot time
        {instance.instance_name: "/labs/settled.clab.yml"},  # Phase 4's fresh re-check
    ]

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 0
    assert queued == 0
    assert engine.inventory.call_count == 2
    ops = admin_session.query(LabOperation).filter_by(instance_id=instance.id).all()
    assert [op.kind for op in ops] == ["deploy"]  # only the pre-existing settled one
    assert instance.status == "active"
    assert instance.error is None
    admin_session.rollback()


def test_runtime_reconcile_leaves_an_escalated_instance_alone_even_with_runtime_present(
    admin_session,
    tenant_a,
    policy,
):
    """An instance already escalated to status="error" (Stream A's destroy-
    escalation feature, which sets this specifically to stop automatic
    destroy retries once cumulative failures hit its threshold) must not get
    ANOTHER automatic destroy enqueued by reconcile_runtime just because its
    status is not one of the "live" ones. The idle reaper already respects
    this (it only selects "active" instances) — reconcile_runtime's
    db_only_repairs path is a different automatic path to the same
    containerlab destroy, and must respect the same escalation.
    """
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-escalated-runtime-present",
        seed={},
        status="error",
        error="automatic destroy failed 5 time(s); escalated for manual review",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {
        instance.instance_name: "/labs/escalated-runtime-present.clab.yml",
    }

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 0
    assert queued == 0
    engine.destroy.assert_not_called()
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 0
    assert instance.status == "error"
    assert instance.error == "automatic destroy failed 5 time(s); escalated for manual review"
    admin_session.rollback()


def test_sweep_protects_present_and_unknown_including_escalated_error(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The protected set is now presence-based: an escalated instance
    (status="error") whose runtime may still be present/unknown must be
    protected exactly like a live one — killing its console would be wrong."""
    _c, act, lt, p = _seed(admin_session, tenant_a.id)
    present = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-present",
        seed={"o": 5},
        status="active",
        consoles={},
    )
    present.runtime_presence = "present"
    unknown_errored = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-unknown-error",
        seed={"o": 5},
        status="error",
        consoles={},
    )
    unknown_errored.runtime_presence = "unknown"
    absent = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-absent",
        seed={"o": 5},
        status="reaped",
        consoles={},
    )
    absent.runtime_presence = "absent"
    admin_session.add_all([present, unknown_errored, absent])
    admin_session.flush()

    monkeypatch.setattr(
        lab_jobs,
        "console_pids",
        lambda: {
            str(present.id): [111],
            str(unknown_errored.id): [222],
            str(absent.id): [333],
        },
    )
    killed = []
    monkeypatch.setattr(lab_jobs, "kill_consoles", lambda pids: killed.extend(pids) or len(killed))

    assert lab_jobs.sweep_orphan_consoles(admin_session) == 1
    assert killed == [333]  # only the confirmed-absent instance's console is killed
    admin_session.rollback()


def test_runtime_reconcile_db_only_repair_enqueues_conditional_destroy_without_projecting_state(
    admin_session,
    tenant_a,
    policy,
):
    """A reaped row whose runtime is confirmed live by fresh inventory gets a
    conditional destroy enqueued (origin="runtime_cleanup",
    runtime_precondition="present") — the reconciler itself still never
    projects status/error (only the worker's own locked, authoritative
    recheck, lab_lifecycle.destroy_if_present, may settle that outcome), but
    it DOES now record runtime_presence="present": Phase 4's own fresh_runtime
    membership check, immediately before this enqueue, is itself a
    lock-verified OBSERVATION (not a projected decision) that the runtime is
    genuinely live right now."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-positive-observation",
        seed={},
        status="reaped",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {instance.instance_name: "/labs/leak.clab.yml"}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    admin_session.refresh(instance)
    assert instance.status == "reaped"
    assert instance.runtime_presence == "present"  # lock-verified observed fact
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "destroy"
    assert operation.origin == "runtime_cleanup"
    assert operation.runtime_precondition == "present"
    admin_session.rollback()


def test_runtime_reconcile_missing_runtime_with_prior_error_still_enqueues_conditionally(
    admin_session,
    tenant_a,
    policy,
):
    """A prior error no longer bypasses the worker's own conditional recheck
    by settling status="error" directly from this pass's snapshot — it must
    ALSO go through the same conditional enqueue() as any other
    missing-runtime instance, exactly like the fresh-redeploy case below.
    Only the worker's own locked, authoritative observation may decide
    status/error; the reconciler itself must not touch either. It DOES
    record runtime_presence="absent" — fresh_runtime's own membership check
    just confirmed absence under lock, so that is a lock-verified observed
    fact, not a projected decision."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-confirmed-absent",
        seed={},
        status="active",
        error="worker lease expired after 3 attempts",
        consoles={},
    )
    instance.runtime_presence = "unknown"
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    admin_session.refresh(instance)
    # status/error untouched by the reconciler itself — the prior
    # error/status survive exactly as they were until the worker's own
    # recheck settles them. runtime_presence IS updated: fresh_runtime's own
    # membership check just confirmed absence under lock.
    assert instance.status == "active"
    assert instance.error == "worker lease expired after 3 attempts"
    assert instance.runtime_presence == "absent"
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "deploy"
    assert operation.origin == "runtime_repair"
    assert operation.runtime_precondition == "absent"
    admin_session.rollback()


def test_runtime_reconcile_missing_runtime_fresh_redeploy_enqueues_conditionally(
    admin_session,
    tenant_a,
    policy,
):
    """No prior error: a conditional deploy (origin="runtime_repair",
    runtime_precondition="absent") is enqueued. The row's runtime_presence
    was left at a stale, contradictory "present" from before the runtime
    actually disappeared; fresh_runtime's own membership check, immediately
    before this enqueue, just re-confirmed absence under lock — that fresh
    OBSERVATION now corrects the stale prior value (still leaving status/
    error strictly to the worker's own conditional recheck)."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-fresh-redeploy",
        seed={},
        status="active",
        consoles={},
    )
    instance.runtime_presence = "present"
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {}

    assert lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    ) == (1, 0)
    admin_session.refresh(instance)
    assert instance.runtime_presence == "absent"  # corrected from stale "present"
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "deploy"
    assert operation.origin == "runtime_repair"
    assert operation.runtime_precondition == "absent"
    admin_session.rollback()


def test_runtime_reconcile_escalated_instance_keeps_its_presence_value(
    admin_session,
    tenant_a,
    policy,
):
    """An escalated instance (status="error") excluded from automatic destroy
    must keep whatever presence value it already carries — reconcile_runtime
    must not touch it at all."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-presence-escalated",
        seed={},
        status="error",
        error="automatic destroy failed 5 time(s); escalated for manual review",
        consoles={},
    )
    instance.runtime_presence = "unknown"
    admin_session.add(instance)
    admin_session.flush()
    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {
        instance.instance_name: "/labs/escalated-presence.clab.yml",
    }

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 0
    assert queued == 0
    engine.destroy.assert_not_called()
    admin_session.refresh(instance)
    assert instance.status == "error"
    assert instance.runtime_presence == "unknown"
    admin_session.rollback()


def test_runtime_reconcile_still_raises_on_duplicate_instance_names(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """The in-application duplicate guard (a second line of defense alongside
    the new DB-level ``uq_lab_instances_instance_name`` unique index — see
    ``alembic/versions/0057_lab_instance_name_unique.py``) must still fire.

    Two persisted rows can no longer actually share an ``instance_name`` now
    that the DB constraint exists, so this feeds ``reconcile_runtime`` an
    in-memory duplicate pair directly (never added to the session) rather
    than trying to persist a conflict the database would now reject.
    """
    fake_rows = [
        LabInstance(
            tenant_id=tenant_a.id,
            activity_id=uuid4(),
            person_id=uuid4(),
            instance_name="dal-duplicate-in-memory",
            seed={},
        ),
        LabInstance(
            tenant_id=uuid4(),
            activity_id=uuid4(),
            person_id=uuid4(),
            instance_name="dal-duplicate-in-memory",
            seed={},
        ),
    ]
    fake_scalars_result = MagicMock()
    fake_scalars_result.all.return_value = fake_rows
    monkeypatch.setattr(admin_session, "scalars", lambda *a, **k: fake_scalars_result)

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {}

    with pytest.raises(RuntimeError, match="duplicate lab instance names"):
        lab_jobs.reconcile_runtime(
            admin_session,
            engine,
            policy=policy,
        )

    engine.destroy.assert_not_called()
    admin_session.rollback()


def test_runtime_reconcile_enqueue_race_is_detected_by_the_full_tuple_not_kind_alone(
    admin_session,
    tenant_a,
    monkeypatch,
    policy,
):
    """A genuinely concurrent operation of the SAME ``kind`` ("destroy") but a
    DIFFERENT ``(origin, runtime_precondition)`` — an ordinary, unconditional,
    user-initiated destroy, not this pass's own conditional intent — must
    still be detected as a race loss. Comparing ``op.kind`` alone would wrongly
    treat this as "this pass's own operation won", attributing (and
    potentially misinterpreting) someone else's unconditional destroy as this
    pass's conditional one.
    """
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-race-full-tuple",
        seed={},
        status="reaped",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()

    engine = MagicMock(
        execution_policy=policy,
    )
    engine.inventory.return_value = {instance.instance_name: "/labs/race-full-tuple.clab.yml"}

    real_enqueue = lab_operations.enqueue
    calls = {"n": 0}

    def _enqueue_with_unconditional_concurrent_winner(
        db, *, instance, kind, requested_by, origin=None, runtime_precondition=None
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # A real, concurrent, user-initiated destroy (ordinary —
            # origin/runtime_precondition both None) landing just before this
            # pass's own conditional destroy enqueue call below.
            real_enqueue(db, instance=instance, kind="destroy", requested_by=p.id)
        return real_enqueue(
            db,
            instance=instance,
            kind=kind,
            requested_by=requested_by,
            origin=origin,
            runtime_precondition=runtime_precondition,
        )

    monkeypatch.setattr(lab_operations, "enqueue", _enqueue_with_unconditional_concurrent_winner)

    queued, destroyed = lab_jobs.reconcile_runtime(
        admin_session,
        engine,
        policy=policy,
    )

    assert destroyed == 0
    assert queued == 0  # this pass's own tuple did not come back — not claimed
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "destroy"
    assert operation.origin is None
    assert operation.runtime_precondition is None
    admin_session.rollback()


def test_reap_idle_never_sets_origin_or_runtime_precondition(
    admin_session,
    tenant_a,
    policy,
):
    """`request_idle_reaps` is explicitly out of scope for conditional
    operations — it must remain an unconditional, originating enqueue with
    both new fields left NULL."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-idle-regression",
        seed={},
        status="active",
        last_active_at=datetime.now(UTC) - timedelta(days=1),
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()

    assert lab_jobs.request_idle_reaps(admin_session) == 1
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "destroy"
    assert operation.origin is None
    assert operation.runtime_precondition is None
    admin_session.rollback()


def test_reconcile_stuck_requeue_never_sets_origin_or_runtime_precondition(
    admin_session,
    tenant_a,
    policy,
):
    """`reconcile_stuck` is explicitly out of scope — its phase-1-to-worker
    cutover repair enqueue must remain unconditional, both new fields NULL."""
    _c, act, _lt, p = _seed(admin_session, tenant_a.id)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=p.id,
        instance_name="dal-stuck-regression",
        seed={},
        status="queued",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()

    assert (
        lab_operations.reconcile_stuck(
            admin_session,
            policy=policy,
        )
        == 1
    )
    operation = admin_session.query(LabOperation).filter_by(instance_id=instance.id).one()
    assert operation.kind == "deploy"
    assert operation.origin is None
    assert operation.runtime_precondition is None
    admin_session.rollback()


@pytest.fixture
def policy():
    """Explicit validated env/default snapshot; never used as a production fallback."""
    return LabExecutionPolicy.from_settings(settings)
