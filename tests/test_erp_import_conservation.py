"""Hosted PostgreSQL proof through the actual legacy ERP history importer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from scripts.erp_transfer import ERPHistoryImportError, import_erp_history

ACK = datetime(2026, 3, 4, 5, 6, 7, 123456, tzinfo=UTC)


@pytest.fixture
def erp_import_databases(genesis_database):
    _admin, superuser, destination_name = genesis_database
    source_name = f"academy_erp_source_{uuid.uuid4().hex[:12]}"
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    source = destination = None
    try:
        with server.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{source_name}"'))
        source = create_engine(make_url(superuser).set(database=source_name))
        destination = create_engine(make_url(superuser).set(database=destination_name))
        with source.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE public.course_completions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL, "
                    "erp_synced_at timestamptz, erp_synced_pct double precision)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE public.academy_erp_completion_checkpoints "
                    "(tenant_id uuid NOT NULL, completion_id uuid NOT NULL, erp_synced_at timestamptz, "
                    "PRIMARY KEY (tenant_id, completion_id))"
                )
            )
        yield source, destination
    finally:
        if source is not None:
            source.dispose()
        if destination is not None:
            destination.dispose()
        with server.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{source_name}"'))
        server.dispose()


def _seed_source(source, facts):
    tenant = uuid.uuid4()
    rows = [
        {"tenant_id": tenant, "completion_id": uuid.uuid4(), "erp_synced_at": at, "erp_synced_pct": pct}
        for at, pct in facts
    ]
    with source.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.course_completions (tenant_id, id, erp_synced_at, erp_synced_pct) "
                "VALUES (:tenant_id, :completion_id, :erp_synced_at, :erp_synced_pct)"
            ),
            rows,
        )
    return rows


def _seed_learning(conn, rows):
    tenant, course = rows[0]["tenant_id"], uuid.uuid4()
    conn.execute(
        text("INSERT INTO public.tenants (id, slug, name) VALUES (:id, :slug, 'ERP import')"),
        {"id": tenant, "slug": tenant.hex},
    )
    conn.execute(
        text(
            "INSERT INTO mod_learning.courses (id, tenant_id, slug, title, discipline, source_ref, status, listed) "
            "VALUES (:id, :t, :slug, 'Import', 'networking', 'seed:import', 'published', false)"
        ),
        {"id": course, "t": tenant, "slug": course.hex},
    )
    for row in rows:
        person = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO public.parties (id, tenant_id, party_type, display_name) "
                "VALUES (:id, :t, 'person', 'Imported learner')"
            ),
            {"id": person, "t": tenant},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.course_completions (id, tenant_id, person_id, course_id, status, pct) "
                "VALUES (:id, :t, :person, :course, 'completed', 1.0)"
            ),
            {"id": row["completion_id"], "t": tenant, "person": person, "course": course},
        )


def _marker(source, row, at):
    with source.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.academy_erp_completion_checkpoints (tenant_id, completion_id, erp_synced_at) "
                "VALUES (:tenant_id, :completion_id, :at)"
            ),
            {**row, "at": at},
        )


def test_actual_import_conserves_all_keys_raw_nulls_timestamps_float_bits(erp_import_databases):
    source, destination = erp_import_databases
    rows = _seed_source(
        source,
        [(None, None), (ACK, None), (None, 87.5), (ACK, -0.0), (ACK, 0.1), (None, float("inf")), (ACK, float("nan"))],
    )
    _marker(source, rows[0], ACK)  # Independent ACK fills timestamp only; pct stays NULL.
    _marker(source, rows[1], None)  # NULL marker cannot erase completion ACK.
    _marker(source, rows[3], ACK)  # Agreeing ACK facts are compatible.
    with source.connect() as src, src.begin(), destination.connect() as dst, dst.begin():
        _seed_learning(dst, rows)
        counts = import_erp_history(src, dst)
        assert counts == {
            "academy_erp_completion_checkpoints": len(rows),
            "academy_erp_imported_completion_keys": len(rows),
        }
        expected = {
            (r[0], r[1]): (r[2], r[3])
            for r in src.execute(
                text(
                    "SELECT c.tenant_id, c.id, COALESCE(c.erp_synced_at, m.erp_synced_at), "
                    "pg_catalog.float8send(c.erp_synced_pct) FROM public.course_completions c "
                    "LEFT JOIN public.academy_erp_completion_checkpoints m "
                    "ON (m.tenant_id, m.completion_id) = (c.tenant_id, c.id)"
                )
            )
        }
        actual = {
            (r[0], r[1]): (r[2], r[3])
            for r in dst.execute(
                text(
                    "SELECT tenant_id, completion_id, erp_synced_at, pg_catalog.float8send(erp_synced_pct) "
                    "FROM public.academy_erp_completion_checkpoints"
                )
            )
        }
        assert actual == expected
        assert set(
            dst.execute(text("SELECT tenant_id, completion_id FROM public.academy_erp_imported_completion_keys"))
        ) == set(expected)


@pytest.mark.parametrize("invalid", ["orphan", "conflict", "unknown_column", "coverage", "checkpoint", "membership"])
def test_actual_import_refuses_ambiguous_or_incomplete_history_and_rolls_back(erp_import_databases, invalid):
    source, destination = erp_import_databases
    rows = _seed_source(source, [(ACK, 0.125)])
    reason = {
        "orphan": "erp_history_orphan_marker",
        "conflict": "erp_history_conflicting_ack",
        "unknown_column": "erp_history_source_schema",
        "coverage": "erp_history_completion_coverage",
        "checkpoint": "erp_history_destination_not_empty",
        "membership": "erp_history_destination_not_empty",
    }[invalid]
    if invalid == "orphan":
        _marker(source, {**rows[0], "completion_id": uuid.uuid4()}, ACK)
    elif invalid == "conflict":
        _marker(source, rows[0], datetime(2026, 3, 5, tzinfo=UTC))
    elif invalid == "unknown_column":
        with source.begin() as src:
            src.execute(text("ALTER TABLE public.academy_erp_completion_checkpoints ADD COLUMN future_ack text"))
    with (
        pytest.raises(ERPHistoryImportError, match=f"^{reason}$"),
        source.connect() as src,
        src.begin(),
        destination.connect() as dst,
        dst.begin(),
    ):
        if invalid != "coverage":
            _seed_learning(dst, rows)
        if invalid == "checkpoint":
            dst.execute(
                text(
                    "INSERT INTO public.academy_erp_completion_checkpoints (tenant_id, completion_id) VALUES (:tenant_id, :completion_id)"
                ),
                rows[0],
            )
        elif invalid == "membership":
            dst.execute(
                text(
                    "INSERT INTO public.academy_erp_imported_completion_keys (tenant_id, completion_id) VALUES (:tenant_id, :completion_id)"
                ),
                rows[0],
            )
        import_erp_history(src, dst)
    with destination.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mod_learning.course_completions")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM public.academy_erp_completion_checkpoints")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM public.academy_erp_imported_completion_keys")).scalar_one() == 0


def test_successful_import_is_rolled_back_with_caller_and_missing_checkpoint_prevents_sender(
    erp_import_databases, monkeypatch
):
    from dotmac_learning.models import CourseCompletion

    from app.config import settings
    from app.services.erp_sync import ERPCheckpointInvariantError, push_completion

    source, destination = erp_import_databases
    rows = _seed_source(source, [(None, None)])
    monkeypatch.setattr(settings, "erp_webhook_url", "https://erp.invalid/hook")

    def no_send(*args, **kwargs):
        raise AssertionError("missing imported checkpoint must refuse before HTTP")

    monkeypatch.setattr("app.services.erp_sync.httpx.post", no_send)
    with source.connect() as src, src.begin(), destination.connect() as dst:
        transaction = dst.begin()
        try:
            _seed_learning(dst, rows)
            import_erp_history(src, dst)
            dst.execute(text("DELETE FROM public.academy_erp_completion_checkpoints"))
            with Session(bind=dst) as db:
                completion = db.get(CourseCompletion, rows[0]["completion_id"])
                with pytest.raises(ERPCheckpointInvariantError, match="missing checkpoint for imported"):
                    push_completion(db, tenant_id=rows[0]["tenant_id"], completion=completion)
        finally:
            transaction.rollback()
    with destination.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mod_learning.course_completions")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM public.academy_erp_imported_completion_keys")).scalar_one() == 0


def test_independent_persisted_ack_verification_refuses_trigger_mutation(erp_import_databases):
    source, destination = erp_import_databases
    rows = _seed_source(source, [(ACK, -0.0)])
    with (
        pytest.raises(ERPHistoryImportError, match=r"^erp_history_ack_verification$"),
        source.connect() as src,
        src.begin(),
        destination.connect() as dst,
        dst.begin(),
    ):
        _seed_learning(dst, rows)
        dst.execute(
            text(
                "CREATE FUNCTION public.corrupt_imported_ack() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN NEW.erp_synced_pct := 42.0; RETURN NEW; END $$"
            )
        )
        dst.execute(
            text(
                "CREATE TRIGGER corrupt_imported_ack BEFORE INSERT ON public.academy_erp_completion_checkpoints "
                "FOR EACH ROW EXECUTE FUNCTION public.corrupt_imported_ack()"
            )
        )
        import_erp_history(src, dst)
    with destination.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mod_learning.course_completions")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM public.academy_erp_completion_checkpoints")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM public.academy_erp_imported_completion_keys")).scalar_one() == 0
