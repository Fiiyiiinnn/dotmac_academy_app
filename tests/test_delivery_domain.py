"""Hosted qualification for a0005 delivery core.

Outbox enqueue is idempotent per (tenant, key); SMTP stays inert when
unconfigured; the SMTP password can never become a stored setting.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.email import send_email_detailed
from app.services.email_outbox import enqueue_email


def test_outbox_enqueue_idempotent_and_smtp_inert(genesis_database):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        with Session(engine) as db, db.begin():
            tenant_id = db.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, 'T') RETURNING id"),
                {"s": f"t{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
            kw = dict(
                tenant_id=tenant_id,
                idempotency_key="welcome:1",
                kind="welcome",
                recipient="ada@example.org",
                subject="Welcome",
                html_body="<p>Hi</p>",
            )
            assert enqueue_email(db, **kw) is True
            assert enqueue_email(db, **kw) is False
            count = db.execute(text("SELECT count(*) FROM email_outbox")).scalar_one()
            assert count == 1
            result = send_email_detailed("ada@example.org", "Hi", "<p>Hi</p>", db=db)
            assert result.sent is False
            assert "SMTP host is empty" in result.error
            with pytest.raises(ValueError, match="unknown academy setting keys"):
                settings_store.set_many(db, {"smtp_password": "must-not-store"})
    finally:
        engine.dispose()
