"""Hosted PostgreSQL qualification for the lab operations queue.

Web tier (app_user) enqueues a check through the column-scoped INSERT
grant; the worker (academy_lab_worker) claims it under the execution
policy, heartbeats, and grades it through the capability client — the
full claim fence (lease stamp, heartbeat, definer) on genesis storage.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import LabExecutionPolicy, settings
from app.models.lab import LabInstance, LabOperation
from app.services import lab_operations
from app.services.lab_result_capability import record_claimed_lab_check
from tests.conftest import _scoped, _url_for, seed_lab_scenario


def test_enqueue_claim_heartbeat_grade(genesis_database):
    admin, superuser, name = genesis_database
    tenant, person, _course, _activity, _template, instance = seed_lab_scenario(admin)
    policy = LabExecutionPolicy.from_settings(settings)

    app_engine = create_engine(_url_for(superuser, name, user="app_user"))
    worker_engine = create_engine(_url_for(superuser, name, user="academy_lab_worker"))
    try:
        with Session(app_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            lab_instance = db.get(LabInstance, instance)
            op = lab_operations.enqueue(db, instance=lab_instance, kind="check", requested_by=person)
            operation_id = op.id
            assert op.state == "queued"

        with Session(worker_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            claimed = lab_operations.claim_next(
                db,
                claimed_by="worker-1",
                claimed_host="labhost",
                claimed_epoch="epoch-1",
                policy=policy,
            )
            assert claimed is not None
            assert claimed.id == operation_id
            assert claimed.state == "claimed"
            assert claimed.claim_lease_seconds == policy.lease_seconds("check")
            assert claimed.attempts == 1
        with Session(worker_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            assert lab_operations.heartbeat(db, operation_id=operation_id, claimed_by="worker-1") is True
            receipt = record_claimed_lab_check(
                db,
                tenant_id=tenant,
                operation_id=operation_id,
                claimed_by="worker-1",
                per_item=[{"id": "c1", "label": "Check 1", "weight": 1, "pass": True}],
            )
            assert receipt.passed is True

        with Session(app_engine) as db:
            _scoped(db.connection(), tenant)
            stored = db.scalar(select(LabOperation).where(LabOperation.id == operation_id))
            assert stored.state == "claimed"
            # The web tier cannot forge worker-owned state: its INSERT
            # grant names only the five request columns.
            with pytest.raises(DBAPIError):
                with db.begin_nested():
                    db.execute(
                        text(
                            "INSERT INTO public.lab_operations (id, tenant_id, "
                            "instance_id, kind, state) VALUES (gen_random_uuid(), "
                            ":tenant, :instance, 'check', 'claimed')"
                        ),
                        {"tenant": tenant, "instance": instance},
                    )
        with admin.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM mod_learning.scores WHERE passed")) == 1
    finally:
        app_engine.dispose()
        worker_engine.dispose()
