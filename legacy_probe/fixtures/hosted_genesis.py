"""Concrete hosted disposable Genesis fixture; all effects HOSTED ONLY, NOT RUN.

Imported explicitly by the hosted canary module. This never registers a normal
application fixture, starts a worker, opens admission, or replaces wholeassembly.
"""

import json
import os
from contextlib import contextmanager

import pytest

from alembic import command
from alembic.script import ScriptDirectory
from app.maintenance import retained_environment as environment
from app.maintenance import retained_hosted_genesis as hosted
from app.maintenance import retained_schema as retained
from tests.fixtures.kernel_identity_entry.rehearsal import FIXTURE, LEGACY_HEAD, config_for, verify_legacy_source
from tests.test_kernel_identity_entry import _seed_populated_legacy


def declared_settings():
    assert os.environ.get("CI") == "true", "hosted resource fixture is never local"
    raw = os.environ.get("ACADEMY_HOSTED_GENESIS_DECLARATION")
    assert raw, "Root must predeclare every purpose before resource creation"
    values = json.loads(raw)
    assert type(values) is dict
    for purpose in ("environment", "history", "assembly"):
        key = "academy_retained_environment_extension_versions"
        values[purpose][key] = tuple(tuple(pair) for pair in values[purpose][key])
        # Cross-check independent purpose output; no inferred target/retarget.
        projected = json.loads(os.environ["ACADEMY_RETAINED_" + purpose.upper() + "_BINDINGS"])
        projected[key] = tuple(tuple(pair) for pair in projected[key])
        assert projected == values[purpose]
    return values


@pytest.fixture
def hosted_declaration():
    return hosted.declaration_from_settings(declared_settings())


@contextmanager
def replayed_owner(declaration, purpose, tmp_path):
    """Actual immutable frozen62 replay finishes before sole coordinator root."""
    verify_legacy_source()
    retained._verify_frozen_source(FIXTURE / "alembic/versions")
    with hosted.disposable_genesis(declaration, purpose=purpose) as owner:
        with owner.replay_connection() as connection:
            applied = []
            cfg = config_for(connection, FIXTURE / "alembic/versions", tmp_path, applied)
            scripts = ScriptDirectory.from_config(cfg)
            assert scripts.get_heads() == [LEGACY_HEAD]
            command.upgrade(cfg, "heads")
            assert len(applied) == 62
            assert {r.revision for r in applied} == {r.revision for r in scripts.walk_revisions()}
            assert not connection.in_transaction(), "frozen62 autocommit must finish before adoption root"
            with connection.begin():
                _seed_populated_legacy(connection)
            assert not connection.in_transaction()
        yield owner


@pytest.fixture
def hosted_owner(hosted_declaration, tmp_path):
    with replayed_owner(hosted_declaration, "environment", tmp_path) as owner:
        yield owner


@contextmanager
def qualified_root(owner, *, _after_rollback=None):
    """The fixture coordinator owns rollback; provider never touches this root."""
    bindings = environment.HostedEnvironmentBindings(**dict(owner.expected))
    with owner.coordinator_connection() as connection:
        root = connection.begin()
        try:
            context = owner.bind_root(connection)
            yield connection, root, context, bindings
        finally:
            if root.is_active:
                root.rollback()
            # A canary may have replaced/physically restarted the root. It is
            # still this fixture coordinator's responsibility to roll it back.
            replacement = connection.get_transaction()
            if replacement is not None and replacement.is_active:
                replacement.rollback()
            if _after_rollback is not None:
                # Observation only: the hosted owner remains terminally poisoned.
                # No owner liveness, ENV, product capture or rebinding is called.
                with connection.begin() as observation:
                    try:
                        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                        assert connection.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
                        _after_rollback(connection)
                    finally:
                        if observation.is_active:
                            observation.rollback()
            assert not connection.in_transaction()
