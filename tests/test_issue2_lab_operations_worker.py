# ruff: noqa: F811  # imported pytest fixtures are redefined as test parameters
"""Issue #2 port of legacy services/test_lab_operations_worker.py.

Disposition: test_lab_operations_claim_once_lease_and_console_fence
against app/services/lab_operations.py. Ports the legacy duplicate
collapse, competing-claim, destroy/check priority, expired-lease
requeue with stale-settle refusal, heartbeat fence, and the
destroy commits unknown-presence/cleared-consoles-before-engine-work
assertions. The remaining legacy capacity/reclaim/conditional-deploy
matrix stays in legacy_probe (see wave unresolved notes).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.lab import LabInstance, LabOperation
from app.services import lab_operations
from app.services.labengine.interface import LabHandle
from tests.issue2_wave1_fixtures import (  # noqa: F401 - pytest fixtures
    admin_engine,
    admin_session,
    app_client,
    app_user_session,
    canonical_account,
    current_account,
    invitation_account,
    lab_policy_rows,
    lab_worker_engine,
    lab_worker_session,
    no_real_console_spawn,
    policy,
    seed_lab,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert lab_policy_rows  # Settings cleanup parity with the legacy module.


@pytest.fixture(autouse=True)
def _no_console(monkeypatch):
    no_real_console_spawn(monkeypatch)


def _db_now(db):
    return db.scalar(select(func.clock_timestamp()))


def _engine(instance_name, policy):
    engine = MagicMock(execution_policy=policy)
    engine.deploy.return_value = LabHandle(instance_name=instance_name, nodes={}, mgmt={}, kinds={})
    return engine


def test_lab_operations_claim_once_lease_and_console_fence(admin_engine, admin_session, tenant_a, policy):
    # Duplicate requests collapse to one open operation.
    instance, person = seed_lab(admin_session, tenant_a.id)
    first = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    second = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    assert first.id == second.id
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 1
    operation_id = first.id
    admin_session.commit()

    # Competing claims claim an operation once (SKIP LOCKED).
    factory = sessionmaker(bind=admin_engine, autoflush=False)
    first_db, second_db = factory(), factory()
    try:
        claimed = lab_operations.claim_next(first_db, claimed_by="worker-one", policy=policy)
        assert claimed is not None and claimed.id == operation_id
        assert lab_operations.claim_next(second_db, claimed_by="worker-two", policy=policy) is None
        # Heartbeat fence: only the claiming worker may heartbeat.
        assert lab_operations.heartbeat(first_db, operation_id=operation_id, claimed_by="worker-one") is True
        assert lab_operations.heartbeat(first_db, operation_id=operation_id, claimed_by="worker-two") is False
        first_db.commit()
    finally:
        first_db.rollback()
        second_db.rollback()
        first_db.close()
        second_db.close()

    # Destroy and check both outrank an older deploy.
    deploy_instance, person = seed_lab(admin_session, tenant_a.id, name="deploy")
    destroy_instance, _ = seed_lab(admin_session, tenant_a.id, name="destroy", status="active")
    lab_operations.enqueue(admin_session, instance=deploy_instance, kind="deploy", requested_by=person.id)
    destroy = lab_operations.enqueue(admin_session, instance=destroy_instance, kind="destroy", requested_by=None)
    check_instance, _ = seed_lab(admin_session, tenant_a.id, name="check-priority", status="active")
    check = lab_operations.enqueue(admin_session, instance=check_instance, kind="check", requested_by=person.id)
    admin_session.commit()
    claimed = lab_operations.claim_next(admin_session, claimed_by="worker", policy=policy)
    assert claimed is not None and claimed.id == destroy.id
    admin_session.commit()
    claimed = lab_operations.claim_next(admin_session, claimed_by="worker", policy=policy)
    assert claimed is not None and claimed.id == check.id
    admin_session.commit()

    # Expired claim is requeued and the stale worker cannot settle.
    # (Settle the still-queued deploy above so claim ordering below is
    # the legacy test's single-operation queue.)
    admin_session.execute(
        LabOperation.__table__.update().where(LabOperation.state.in_(("queued", "claimed"))).values(state="succeeded")
    )
    # reconcile_stuck also repairs queued/provisioning instances that
    # have no open operation; park the earlier seeds out of that set.
    admin_session.execute(
        LabInstance.__table__.update()
        .where(LabInstance.status.in_(("queued", "provisioning", "resetting")))
        .values(status="error")
    )
    admin_session.commit()
    instance, person = seed_lab(admin_session, tenant_a.id, name="expiry", status="provisioning")
    operation = lab_operations.enqueue(admin_session, instance=instance, kind="deploy", requested_by=person.id)
    operation.state = "claimed"
    operation.claim_lease_seconds = policy.lease_seconds(operation.kind)
    operation.claimed_by = "stale-worker"
    operation.claimed_host = "stale-host"
    operation.claimed_epoch = "111:staleboot"
    expired_at = _db_now(admin_session) - timedelta(seconds=policy.lease_seconds(operation.kind) + 60)
    operation.claimed_at = expired_at
    operation.heartbeat_at = expired_at
    admin_session.commit()

    assert lab_operations.reconcile_stuck(admin_session, policy=policy) == 1
    admin_session.commit()
    admin_session.refresh(operation)
    assert operation.claimed_host is None
    assert operation.claimed_epoch is None
    replacement = lab_operations.claim_next(admin_session, claimed_by="new-worker", policy=policy)
    assert replacement is not None
    admin_session.commit()

    engine = _engine(instance.instance_name, policy)
    assert (
        lab_operations.run_claimed(
            admin_session, operation_id=operation.id, claimed_by="stale-worker", engine=engine, policy=policy
        )
        == "stale"
    )
    engine.destroy.assert_not_called()
    engine.deploy.assert_not_called()
    current = admin_session.scalar(select(LabOperation).where(LabOperation.id == operation.id))
    assert current is not None and current.claimed_by == "new-worker"


def test_destroy_commits_unknown_presence_and_cleared_consoles_before_engine_work(admin_session, tenant_a, policy):
    """The web tier must stop proxying as soon as destroy begins, not when it ends."""
    instance, _ = seed_lab(admin_session, tenant_a.id, name="destroy-visible", status="active", presence="present")
    instance.consoles = {"client": {"kind": "linux", "mgmt": "192.0.2.9", "port": 9001}}
    admin_session.commit()
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
        with Session(admin_session.get_bind()) as other:
            row = other.get(LabInstance, instance_id)
            seen["presence"], seen["consoles"] = row.runtime_presence, row.consoles
        raise RuntimeError("destroy refused")

    engine = MagicMock(execution_policy=policy)
    engine.destroy.side_effect = _destroy
    outcome = lab_operations.run_claimed(
        admin_session, operation_id=operation.id, claimed_by="worker", engine=engine, policy=policy
    )
    assert outcome == "failed"
    assert seen == {"presence": "unknown", "consoles": {}}
    admin_session.refresh(instance)
    assert instance.runtime_presence == "unknown"
    assert instance.consoles == {}
