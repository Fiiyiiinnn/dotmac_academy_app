# ruff: noqa: F811  # imported pytest fixtures are redefined as test parameters
"""Issue #2 port of legacy services/test_lab_worker_lifecycle.py.

Disposition: test_lab_worker_singleton_single_settings_snapshot
against app/cli.py _lab_worker/_lab_reconcile, host_lock, lab_jobs.
All three legacy tests are ported; the retired PlatformSetting row
becomes a kernel DomainSetting row (value_text="null").
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from dotmac_kernel.settings_models import DomainSetting
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import cli
from app.config import settings
from app.services import host_lock, lab_jobs, lab_operations, settings_store
from app.services.labengine import containerlab
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

assert lab_policy_rows


def _cli_seams(monkeypatch, worker, seen):
    @contextmanager
    def singleton():
        seen.append("singleton-enter")
        try:
            yield
        finally:
            seen.append("singleton-exit")

    @contextmanager
    def session():
        assert seen[0] == "singleton-enter"
        assert worker.scalar(text("SELECT current_user")) == "academy_lab_worker"
        seen.append("session")
        yield worker

    monkeypatch.setattr(host_lock, "worker_singleton_lock", singleton)
    monkeypatch.setattr(lab_jobs, "lab_worker_session", session)
    monkeypatch.setattr(settings, "lab_host_role", "lab")
    monkeypatch.setattr("shutil.which", lambda command: "/test/containerlab")
    monkeypatch.setattr("app.config.validate_settings", lambda value: [])


def test_lab_worker_singleton_single_settings_snapshot(lab_policy_rows, lab_worker_session, admin_engine, monkeypatch):
    lab_policy_rows.commit()
    seen, snapshots, engines, sleeps = [], [], [], []
    worker = lab_worker_session
    _cli_seams(monkeypatch, worker, seen)
    real_loader = settings_store.execution_policy

    def load(db):
        assert db is worker
        policy = real_loader(db)
        snapshots.append(policy)
        return policy

    monkeypatch.setattr(settings_store, "execution_policy", load)

    def engine(workdir, *, execution_policy, lock_label):
        assert execution_policy is snapshots[-1]
        result = SimpleNamespace(execution_policy=execution_policy)
        engines.append(result)
        return result

    monkeypatch.setattr(containerlab, "ContainerlabEngine", engine)
    identity = lab_operations.WorkerIdentity(host="test-host", epoch="test-epoch")
    monkeypatch.setattr(lab_operations, "worker_identity_parts", lambda: identity)

    def startup(db, *, host, epoch, policy):
        assert db is worker and policy is snapshots[0]
        assert (host, epoch) == (identity.host, identity.epoch)
        assert not engines
        return 0

    monkeypatch.setattr(lab_operations, "reclaim_previous_epoch", startup)

    def stuck(db, *, policy):
        assert db is worker and policy is snapshots[-1]
        assert engines[-1].execution_policy is policy
        return 0

    monkeypatch.setattr(lab_operations, "reconcile_stuck", stuck)

    def recover(db, engine, *, policy):
        assert db is worker and engine is engines[-1]
        assert engine.execution_policy is policy is snapshots[-1]
        return 0

    monkeypatch.setattr(lab_jobs, "recover_missing_consoles", recover)

    def drain(db, engine, *, policy, claimed_by, claimed_host, claimed_epoch):
        assert db is worker and engine is engines[-1]
        assert engine.execution_policy is policy is snapshots[-1]
        assert (claimed_by, claimed_host, claimed_epoch) == (identity.claimed_by, identity.host, identity.epoch)
        if len(engines) == 1:
            with sessionmaker(bind=admin_engine)() as writer:
                settings_store.set_many(
                    writer, {"lab_execution_policy_overrides": '{"lab_worker_poll_interval_seconds":9}'}
                )
                writer.commit()
            assert policy.lab_worker_poll_interval_seconds == 5
        return 0

    monkeypatch.setattr(lab_jobs, "drain_once", drain)

    def sleep(interval):
        sleeps.append(interval)
        if len(sleeps) == 2:
            raise cli._WorkerShutdownRequested()

    monkeypatch.setattr(cli.time, "sleep", sleep)
    cli._lab_worker(argparse.Namespace())
    assert len(snapshots) == 3  # Separate startup plus two passes.
    assert snapshots[0] is not snapshots[1] and snapshots[1] is not snapshots[2]
    assert len(engines) == 2 and engines[0] is not engines[1]
    assert sleeps == [5, 9]
    assert seen[-1] == "singleton-exit"


def test_cli_strict_store_failure_prevents_engine_and_external_work(lab_policy_rows, lab_worker_session, monkeypatch):
    lab_policy_rows.add(
        DomainSetting(
            tenant_id=None,
            scope_kind="platform",
            scope_id=None,
            domain="academy",
            key="lab_execution_policy_overrides",
            value_text="null",
            value_json=None,
        )
    )
    lab_policy_rows.commit()
    seen = []
    _cli_seams(monkeypatch, lab_worker_session, seen)

    def forbidden(*args, **kwargs):
        pytest.fail("engine or external work started after invalid strict snapshot")

    monkeypatch.setattr(containerlab, "ContainerlabEngine", forbidden)
    monkeypatch.setattr(lab_operations, "reclaim_previous_epoch", forbidden)
    monkeypatch.setattr(lab_jobs, "drain_once", forbidden)
    with pytest.raises(ValueError):
        cli._lab_worker(argparse.Namespace())


def test_cli_one_shot_reconcile_shares_snapshot_inside_actual_worker_session(
    lab_policy_rows, lab_worker_session, monkeypatch
):
    import shutil

    monkeypatch.setattr(settings, "lab_host_role", "lab")
    monkeypatch.setattr(shutil, "which", lambda command: "/test/containerlab")
    monkeypatch.setattr("app.config.validate_settings", lambda value: [])
    worker = lab_worker_session

    @contextmanager
    def session():
        assert worker.scalar(text("SELECT current_user")) == "academy_lab_worker"
        yield worker

    monkeypatch.setattr(lab_jobs, "lab_worker_session", session)
    policies, engines = [], []
    real_loader = settings_store.execution_policy

    def load(db):
        assert db is worker
        policies.append(real_loader(db))
        return policies[-1]

    monkeypatch.setattr(settings_store, "execution_policy", load)

    def engine(workdir, *, execution_policy, lock_label):
        assert len(policies) == 1 and execution_policy is policies[0]
        engines.append(SimpleNamespace(execution_policy=execution_policy))
        return engines[-1]

    monkeypatch.setattr(containerlab, "ContainerlabEngine", engine)

    def stuck(db, *, policy):
        assert db is worker and policy is policies[0]
        return 0

    def runtime(db, engine, *, policy):
        assert db is worker and engine is engines[0]
        assert policy is engine.execution_policy is policies[0]
        return 0, 0

    monkeypatch.setattr(lab_operations, "reconcile_stuck", stuck)
    monkeypatch.setattr(lab_jobs, "reconcile_runtime", runtime)
    monkeypatch.setattr(lab_jobs, "sweep_orphan_consoles", lambda db: 0)
    cli._lab_reconcile(argparse.Namespace())
    assert len(policies) == len(engines) == 1
