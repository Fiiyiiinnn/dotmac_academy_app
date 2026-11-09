"""Hosted source-to-target conservation and refusal proofs for product copying."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import product_transfer
from scripts.product_transfer import ProductTransferRefused, copy_product_tables
from tests.conftest import _superuser_url


@pytest.fixture
def product_databases():
    url = _superuser_url()
    names = [f"academy_copy_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    server = create_engine(url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        for name in names:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    engines = [create_engine(make_url(url).set(database=name)) for name in names]
    try:
        yield engines
    finally:
        for engine in engines:
            engine.dispose()
        with server.connect() as conn:
            for name in names:
                conn.execute(text(f'DROP DATABASE "{name}"'))
        server.dispose()


def test_console_history_preserved_and_stamp_trigger_restored(product_databases):
    source, dest = product_databases
    stamp = datetime(2020, 2, 3, 4, 5, 6, 123456, tzinfo=UTC)
    instance, tenant = uuid.uuid4(), uuid.uuid4()
    ddl = "CREATE TABLE lab_console_activity (instance_id uuid PRIMARY KEY, tenant_id uuid, last_seen_at timestamptz)"
    for engine in (source, dest):
        with engine.begin() as conn:
            conn.execute(text(ddl))
    with source.begin() as conn:
        conn.execute(
            text("INSERT INTO lab_console_activity VALUES (:i, :t, :stamp)"),
            {"i": instance, "t": tenant, "stamp": stamp},
        )
    with dest.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION lab_console_activity_stamp() RETURNS trigger LANGUAGE plpgsql AS "
                "$$BEGIN NEW.last_seen_at := now(); RETURN NEW; END$$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER lab_console_activity_stamp BEFORE INSERT OR UPDATE "
                "ON lab_console_activity FOR EACH ROW EXECUTE FUNCTION lab_console_activity_stamp()"
            )
        )
    with source.connect() as sconn, dest.begin() as dconn:
        assert copy_product_tables(sconn, dconn, tables=("lab_console_activity",)) == {"lab_console_activity": 1}
    with dest.begin() as conn:
        assert conn.scalar(text("SELECT last_seen_at FROM lab_console_activity")) == stamp
        assert conn.scalar(text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'lab_console_activity_stamp'")) == "O"
        conn.execute(
            text("INSERT INTO lab_console_activity VALUES (:i, :t, :stamp)"),
            {"i": uuid.uuid4(), "t": tenant, "stamp": stamp},
        )
        assert conn.scalar(text("SELECT max(last_seen_at) FROM lab_console_activity")) > stamp


@pytest.mark.parametrize("side", ["source", "destination"])
def test_unmapped_columns_refuse_instead_of_intersection_copy(product_databases, monkeypatch, side):
    source, dest = product_databases
    monkeypatch.setitem(product_transfer.PRODUCT_COLUMNS, "notifications", ("id", "message"))
    for engine in (source, dest):
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE notifications (id uuid PRIMARY KEY, message text)"))
    with (source if side == "source" else dest).begin() as conn:
        conn.execute(text("ALTER TABLE notifications ADD COLUMN unreviewed text"))
    with source.connect() as sconn:
        with pytest.raises(ProductTransferRefused, match="schema_drift"), dest.begin() as dconn:
            copy_product_tables(sconn, dconn, tables=("notifications",))
    with dest.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM notifications")) == 0


def test_value_verification_detects_trigger_corruption_and_rolls_back(product_databases, monkeypatch):
    source, dest = product_databases
    monkeypatch.setitem(product_transfer.PRODUCT_COLUMNS, "notifications", ("id", "score"))
    for engine in (source, dest):
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE notifications (id uuid PRIMARY KEY, score double precision)"))
    with source.begin() as conn:
        conn.execute(text("INSERT INTO notifications VALUES (:id, '-0'::double precision)"), {"id": uuid.uuid4()})
    with dest.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION alter_score() RETURNS trigger LANGUAGE plpgsql AS "
                "$$BEGIN NEW.score := 0.0; RETURN NEW; END$$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER alter_score BEFORE INSERT ON notifications FOR EACH ROW EXECUTE FUNCTION alter_score()"
            )
        )
    with source.connect() as sconn:
        with pytest.raises(ProductTransferRefused, match="value_verification"), dest.begin() as dconn:
            copy_product_tables(sconn, dconn, tables=("notifications",))
    with dest.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM notifications")) == 0


@pytest.mark.parametrize("state", [None, "D", "R", "A"])
def test_console_copy_refuses_missing_or_unexpected_trigger_protection(product_databases, state):
    source, dest = product_databases
    ddl = "CREATE TABLE lab_console_activity (instance_id uuid PRIMARY KEY, tenant_id uuid, last_seen_at timestamptz)"
    for engine in (source, dest):
        with engine.begin() as conn:
            conn.execute(text(ddl))
    if state is not None:
        with dest.begin() as conn:
            conn.execute(
                text(
                    "CREATE FUNCTION lab_console_activity_stamp() RETURNS trigger LANGUAGE plpgsql AS "
                    "$$BEGIN NEW.last_seen_at := now(); RETURN NEW; END$$"
                )
            )
            conn.execute(
                text(
                    "CREATE TRIGGER lab_console_activity_stamp BEFORE INSERT OR UPDATE "
                    "ON lab_console_activity FOR EACH ROW EXECUTE FUNCTION lab_console_activity_stamp()"
                )
            )
            command = {"D": "DISABLE", "R": "ENABLE REPLICA", "A": "ENABLE ALWAYS"}[state]
            conn.execute(text(f"ALTER TABLE lab_console_activity {command} TRIGGER lab_console_activity_stamp"))
    with source.connect() as sconn:
        with pytest.raises(ProductTransferRefused, match="not_protected"), dest.begin() as dconn:
            copy_product_tables(sconn, dconn, tables=("lab_console_activity",))
    with dest.connect() as conn:
        assert (
            conn.scalar(text("SELECT tgenabled FROM pg_trigger WHERE tgname = 'lab_console_activity_stamp'")) == state
        )
