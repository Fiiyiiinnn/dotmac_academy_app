"""Hosted PostgreSQL qualification for the a0002 admissions lineage.

Genesis creates the five product tables with forced tenant RLS and the
tenant-isolation policies; the ported ORM models match the frozen DDL
(a superuser insert round-trips an applicant row).
"""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.admissions import Applicant

TABLES = (
    "applicants",
    "onboarding_tasks",
    "facilitator_nominations",
    "facilitator_requirements",
    "tenant_entrance_defaults",
)


def test_admissions_tables_have_forced_tenant_rls(genesis_database):
    admin, _superuser, _name = genesis_database
    with admin.connect() as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                {"t": list(TABLES)},
            )
        }
        assert set(rows) == set(TABLES)
        assert all(v == (True, True) for v in rows.values())
        policies = {r[0] for r in conn.execute(text("SELECT policyname FROM pg_policies WHERE schemaname='public'"))}
        assert {f"{t}_tenant_isolation" for t in TABLES} <= policies
        # Head tracking lives in the newest lineage test; a0002 tables
        # existing proves this lineage ran.


def test_applicant_model_matches_genesis_ddl(genesis_database):
    admin, superuser, name = genesis_database
    del admin
    url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        with Session(engine) as db:
            tenant_id = db.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, 'T') RETURNING id"),
                {"s": f"t{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
            applicant = Applicant(tenant_id=tenant_id, email="ada@example.org", first_name="Ada", last_name="Lovelace")
            db.add(applicant)
            db.commit()
            assert db.get(Applicant, applicant.id).email == "ada@example.org"
    finally:
        engine.dispose()
