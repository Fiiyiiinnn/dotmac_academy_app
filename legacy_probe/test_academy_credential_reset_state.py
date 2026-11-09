"""Hosted-only reset-state canaries; no live adoption or graph proof claimed."""

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dotmac_kernel.migrations.identity_history import IdentityHistoryImportRefused
from sqlalchemy.exc import DBAPIError

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.maintenance.identity_history import _POLICY, _projection_preflight, import_academy_identity_history
from app.migration_graph import ordinary_migration_config
from app.models.identity_extensions import AcademyCredentialPolicy
from legacy_probe.test_academy_identity_history import _prepared


def _projection():
    now = datetime.now(timezone.utc)
    return dict(
        credential_id=uuid4(),
        tenant_id=uuid4(),
        email="synthetic@example.invalid",
        failed_login_attempts=-3,
        lifecycle_version=0,
        reset_required=False,
        locked_until=None,
        created_at=now,
        updated_at=now,
    )


def test_model_requires_explicit_boolean_without_defaults():
    column = AcademyCredentialPolicy.__table__.c.reset_required
    assert isinstance(column.type, sa.Boolean)
    assert column.nullable is False
    assert column.default is None and column.server_default is None
    assert column.computed is None and column.identity is None


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", "unknown", object()])
def test_unknown_reset_projection_refuses(value):
    values = _projection()
    values["reset_required"] = value
    with pytest.raises(IdentityHistoryImportRefused):
        _projection_preflight(Mock(), _POLICY, values)


@pytest.mark.parametrize("value", [False, True])
def test_exact_boolean_reset_projection_admitted(value):
    values = _projection()
    values["reset_required"] = value
    _projection_preflight(Mock(), _POLICY, values)


def test_missing_reset_projection_refuses():
    values = _projection()
    del values["reset_required"]
    with pytest.raises(IdentityHistoryImportRefused):
        _projection_preflight(Mock(), _POLICY, values)


def _migration():
    return (
        ScriptDirectory.from_config(ordinary_migration_config())
        .get_revision("a002_credential_reset_state")
        .module
    )


def test_populated_forward_migration_preserves_other_policy_fields(legacy_engine, tmp_path, monkeypatch):
    with _prepared(legacy_engine, tmp_path, monkeypatch) as (connection, expected, operation):
        import_academy_identity_history(connection, expected, operation)
        connection.execute(sa.text("ALTER TABLE public.academy_credential_policies DROP COLUMN reset_required"))
        connection.execute(sa.text("CREATE TEMP TABLE reset_policy_before AS TABLE public.academy_credential_policies"))
        root = connection.get_transaction()

        def refuse_population(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE public.academy_credential_policies SET reset_required"):
                raise RuntimeError("synthetic population fault")

        sa.event.listen(connection, "before_cursor_execute", refuse_population)
        try:
            with pytest.raises(RuntimeError, match="synthetic population fault"):
                with connection.begin_nested():
                    with Operations.context(MigrationContext.configure(connection)):
                        _migration().upgrade()
        finally:
            sa.event.remove(connection, "before_cursor_execute", refuse_population)
        assert "reset_required" not in {
            c["name"] for c in sa.inspect(connection).get_columns("academy_credential_policies", schema="public")
        }
        with Operations.context(MigrationContext.configure(connection)):
            _migration().upgrade()
        assert connection.get_transaction() is root and root.is_active
        assert (
            connection.scalar(
                sa.text("SELECT bool_and(reset_required IS FALSE) FROM public.academy_credential_policies")
            )
            is True
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT NOT EXISTS ((SELECT "
                    "credential_id,tenant_id,email,failed_login_attempts,lifecycle_version,locked_until,created_at,updated_at"
                    " FROM public.academy_credential_policies EXCEPT TABLE reset_policy_before) UNION ALL "
                    "(TABLE reset_policy_before EXCEPT SELECT "
                    "credential_id,tenant_id,email,failed_login_attempts,lifecycle_version,locked_until,created_at,updated_at"
                    " FROM public.academy_credential_policies))"
                )
            )
            is True
        )
        column = next(
            c
            for c in sa.inspect(connection).get_columns("academy_credential_policies", schema="public")
            if c["name"] == "reset_required"
        )
        assert isinstance(column["type"], sa.Boolean)
        assert column["nullable"] is False and column["default"] is None
        for statement in (
            "UPDATE public.academy_credential_policies SET reset_required=NULL",
            "INSERT INTO public.academy_credential_policies "
            "(credential_id,tenant_id,email,failed_login_attempts,lifecycle_version,locked_until,created_at,updated_at)"
            " SELECT "
            "credential_id,tenant_id,email,failed_login_attempts,lifecycle_version,locked_until,created_at,updated_at"
            " FROM public.academy_credential_policies LIMIT 1",
        ):
            with pytest.raises(DBAPIError) as failure:
                with connection.begin_nested():
                    connection.execute(sa.text(statement))
            assert failure.value.orig.sqlstate == "23502"


def test_later_true_survives_unrelated_update_and_downgrade_refuses(legacy_engine, tmp_path, monkeypatch):
    with _prepared(legacy_engine, tmp_path, monkeypatch) as (connection, expected, operation):
        import_academy_identity_history(connection, expected, operation)
        connection.execute(sa.update(_POLICY).values(reset_required=True))
        connection.execute(sa.update(_POLICY).values(failed_login_attempts=4, lifecycle_version=1))
        assert connection.scalar(sa.select(sa.func.bool_and(_POLICY.c.reset_required))) is True
        # A repeated upgrade must not clear accepted True values.
        with pytest.raises(DBAPIError) as failure:
            with connection.begin_nested():
                with Operations.context(MigrationContext.configure(connection)):
                    _migration().upgrade()
        assert failure.value.orig.sqlstate == "42701"
        assert connection.scalar(sa.select(sa.func.bool_and(_POLICY.c.reset_required))) is True
        with pytest.raises(IdentityHistoryImportRefused):
            import_academy_identity_history(connection, expected, operation)
        assert connection.scalar(sa.select(sa.func.bool_and(_POLICY.c.reset_required))) is True
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="new-facts disposition"):
                _migration().downgrade()
        assert connection.scalar(sa.select(sa.func.bool_and(_POLICY.c.reset_required))) is True


@pytest.mark.parametrize("drift", ["missing", "nullable", "defaulted", "generated", "wrong_type"])
def test_reset_catalog_drift_refuses_before_canonical_import(legacy_engine, tmp_path, monkeypatch, drift):
    with _prepared(legacy_engine, tmp_path, monkeypatch) as (connection, expected, operation):
        prefix = "ALTER TABLE public.academy_credential_policies "
        if drift in {"missing", "generated", "wrong_type"}:
            connection.execute(sa.text(prefix + "DROP COLUMN reset_required"))
        changes = {
            "nullable": "ALTER COLUMN reset_required DROP NOT NULL",
            "defaulted": "ALTER COLUMN reset_required SET DEFAULT false",
            "generated": "ADD COLUMN reset_required BOOLEAN GENERATED ALWAYS AS (false) STORED NOT NULL",
            "wrong_type": "ADD COLUMN reset_required INTEGER NOT NULL",
        }
        if drift in changes:
            connection.execute(sa.text(prefix + changes[drift]))
        import app.maintenance.identity_history as history

        canonical = Mock(side_effect=AssertionError("canonical importer reached"))
        monkeypatch.setattr(history, "import_identity_history", canonical)
        with pytest.raises(IdentityHistoryImportRefused):
            import_academy_identity_history(connection, expected, operation)
        canonical.assert_not_called()
        assert connection.scalar(sa.text("SELECT count(*) FROM public.user_credentials")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM public.academy_credential_policies")) == 0
