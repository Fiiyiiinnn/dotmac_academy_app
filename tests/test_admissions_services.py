"""Hosted qualification for the ported admissions services slice.

Onboarding seeds the settings-backed task catalog for an applicant;
invitation/entrance policies compose from legacy defaults and refuse
malformed stored values; settings expose attribute access.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.admissions import Applicant
from app.services import onboarding, settings_store
from app.services.entrance_invitation_policy import entrance_invitation_policy
from app.services.invitation_policy import invitation_enrollment_policy


def _super_engine(genesis_database):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    return create_engine(url)


def test_onboarding_seeds_catalog_and_policies_compose(genesis_database):
    engine = _super_engine(genesis_database)
    try:
        with Session(engine) as db, db.begin():
            tenant_id = db.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, 'T') RETURNING id"),
                {"s": f"t{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
            applicant = Applicant(
                tenant_id=tenant_id,
                email="ada@example.org",
                first_name="Ada",
                last_name="Lovelace",
            )
            db.add(applicant)
            db.flush()
            tasks = onboarding.seed_tasks(db, tenant_id=tenant_id, applicant_id=applicant.id)
            assert [t.key for t in tasks] == [
                "entrance_assessment",
                "confirm_details",
                "orientation",
            ]
            assert not onboarding.is_complete(db, tenant_id=tenant_id, applicant_id=applicant.id)
            ent = entrance_invitation_policy(db)
            assert ent.deadline_days == 7
            inv = invitation_enrollment_policy(db)
            assert inv.member_role("admin") == "instructor"
            assert inv.member_role("nobody") == "student"
    finally:
        engine.dispose()


def test_settings_attribute_access_and_override(genesis_database):
    admin, _superuser, _name = genesis_database
    with Session(admin) as db, db.begin():
        settings_store.set_many(db, {"entrance_invitation_deadline_days": 3})
    with Session(admin) as db:
        eff = settings_store.effective(db, require_store=True)
        assert eff.entrance_invitation_deadline_days == 3
        assert eff["invitation_token_hours"] == 168.0
        with pytest.raises(AttributeError):
            _ = eff.no_such_key
