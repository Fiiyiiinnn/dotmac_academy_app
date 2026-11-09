"""V6 importer product-table copy edge cases (issue #1 rehearsal bugs).

The live-snapshot rehearsal surfaced two failures in
``import_product_tables``: reserved-word column names (``current_role``)
breaking unquoted INSERT lists, and ``json``/``jsonb`` values arriving
as Python dicts that psycopg cannot adapt. This test builds the same
shape in two scratch databases and requires a clean verbatim copy.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_DDL = 'CREATE TABLE applicants (id uuid PRIMARY KEY, "current_role" text, assessment_profile jsonb, extra json, note text)'


def _databases(superuser: str):
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    names = [f"academy_v2_imp_{role}_{uuid.uuid4().hex[:10]}" for role in ("src", "dst")]
    with server.connect() as conn:
        for name in names:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    server.dispose()
    urls = [make_url(superuser).set(database=n).render_as_string(hide_password=False) for n in names]
    return names, urls


def _drop(superuser: str, names) -> None:
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        for name in names:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    server.dispose()


def test_product_copy_quotes_reserved_names_and_casts_json(genesis_database, monkeypatch):
    from scripts import v6_import

    _admin, superuser, _name = genesis_database
    names, (src_url, dst_url) = _databases(superuser)
    monkeypatch.setattr(v6_import, "PRODUCT_TABLES", ("applicants",))
    from scripts import product_transfer

    monkeypatch.setitem(
        product_transfer.PRODUCT_COLUMNS, "applicants", ("id", "current_role", "assessment_profile", "extra", "note")
    )
    monkeypatch.setitem(product_transfer.DESTINATION_NULL_FIELDS, "applicants", frozenset())
    row_id = uuid.uuid4()
    src = create_engine(src_url)
    dst = create_engine(dst_url)
    try:
        with src.begin() as conn:
            conn.execute(text(_DDL))
            conn.execute(
                text(
                    'INSERT INTO applicants (id, "current_role", assessment_profile, extra, note) '
                    "VALUES (:id, 'engineer', CAST(:p AS jsonb), CAST(:e AS json), NULL)"
                ),
                {"id": row_id, "p": '{"score": 7}', "e": '{"flag": true}'},
            )
        with dst.begin() as conn:
            conn.execute(text(_DDL))
        exceptions: list = []
        with src.connect() as sconn, dst.connect() as dconn, dconn.begin():
            totals = v6_import.import_product_tables(sconn, dconn, exceptions)
        assert exceptions == []
        assert totals == {"applicants": 1}
        with dst.connect() as conn:
            role, profile, extra, note = conn.execute(
                text(
                    'SELECT "current_role", assessment_profile::text, extra::text, note FROM applicants WHERE id = :id'
                ),
                {"id": row_id},
            ).one()
        assert role == "engineer"
        assert json.loads(profile) == {"score": 7}
        assert json.loads(extra) == {"flag": True}
        assert note is None
    finally:
        src.dispose()
        dst.dispose()
        _drop(superuser, names)
