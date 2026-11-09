"""Hosted PostgreSQL qualification for lifecycle grading with a fake engine.

The full worker check path short of a real lab host: enqueue (web tier)
→ claim (worker) → `lab_lifecycle.grade` evaluates the template checks
against a fake `LabEngine` and delegates the result through the
capability client into the definer — the score must land in
`mod_learning` with the receipt totals matching the Python-side sums.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import LabExecutionPolicy, settings
from app.models.lab import LabInstance, LabTemplate
from app.services import lab_lifecycle, lab_operations
from app.services.labengine.interface import ExecResult, LabEngine, LabHandle
from tests.conftest import _scoped, _url_for, seed_lab_scenario


class FakeEngine(LabEngine):
    def inventory(self) -> dict[str, str]:
        return {}

    def deploy(self, topology_text: str, instance_name: str) -> LabHandle:
        raise NotImplementedError

    @property
    def execution_policy(self):
        from app.config import LabExecutionPolicy, settings

        return LabExecutionPolicy.from_settings(settings)

    def destroy(self, instance_name: str) -> None:
        return None

    def deploy_if_absent(self, topology_text: str, instance_name: str) -> LabHandle | None:
        raise NotImplementedError

    def destroy_if_present(self, instance_name: str) -> bool:
        return False

    def exec(self, handle: LabHandle, node: str, command: list) -> ExecResult:
        return ExecResult(stdout="ok\n", stderr="", exit_code=0)

    def ssh_exec(
        self,
        handle: LabHandle,
        node: str,
        command: str,
        user: str = "admin",
        password: str = "",
    ) -> ExecResult:
        return ExecResult(stdout="ok\n", stderr="", exit_code=0)

    def reset(self, topology_text: str, instance_name: str) -> LabHandle:
        raise NotImplementedError

    def status(self, instance_name: str) -> str:
        return "running"

    def inspect_running(self, instance_name: str) -> LabHandle | None:
        return None

    def console_target(self, handle: LabHandle, node: str) -> str:
        return "127.0.0.1:0"


def test_grade_through_fake_engine_lands_in_ledger(genesis_database):
    admin, superuser, name = genesis_database
    tenant, person, _course, _activity, template_id, instance_id = seed_lab_scenario(admin)
    policy = LabExecutionPolicy.from_settings(settings)

    app_engine = create_engine(_url_for(superuser, name, user="app_user"))
    worker_engine = create_engine(_url_for(superuser, name, user="academy_lab_worker"))
    try:
        with Session(app_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            op = lab_operations.enqueue(
                db,
                instance=db.get(LabInstance, instance_id),
                kind="check",
                requested_by=person,
            )
            operation_id = op.id

        with Session(worker_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            claimed = lab_operations.claim_next(db, claimed_by="worker-1", policy=policy)
            assert claimed is not None and claimed.id == operation_id
        with Session(worker_engine) as db, db.begin():
            _scoped(db.connection(), tenant)
            instance = db.get(LabInstance, instance_id)
            template = db.get(LabTemplate, template_id)
            handle = LabHandle(
                instance_name=instance.instance_name,
                nodes={"n1": "container-n1"},
                mgmt={},
                kinds={},
            )
            receipt = lab_lifecycle.grade(
                db,
                instance,
                FakeEngine(),
                template,
                handle,
                operation_id=operation_id,
                claimed_by="worker-1",
            )
            assert receipt.passed is True
            assert receipt.score == receipt.max_score == 1.0

        with admin.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM mod_learning.scores WHERE passed")) == 1
            assert conn.scalar(text("SELECT count(*) FROM public.lab_result_deliveries WHERE first_pass")) == 1
    finally:
        app_engine.dispose()
        worker_engine.dispose()
