# ruff: noqa: F811  # imported pytest fixtures are redefined as test parameters
"""Issue #2 port of legacy web/test_canonical_public_reset.py.

Disposition: test_public_reset_anti_enumeration_and_token_spend
against app/services/canonical_lifecycle.py via app/web/lifecycle
routes. All legacy tests are ported; the PlatformSetting prior-value
read becomes settings_store.get_all over kernel domain_settings.
"""

from __future__ import annotations

from uuid import uuid4

from dotmac_kernel.models import Tenant, UserCredential
from dotmac_kernel.security import verify_password
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.models.account_token import AccountToken
from app.models.email_outbox import EmailOutbox
from app.services import canonical_lifecycle
from app.services.security import hash_token
from app.services.settings_store import delete, get_all, set_many
from app.web import lifecycle as routes
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

assert canonical_account


def _request(db, tid, *, htmx=False):
    headers = [(b"host", b"alpha.localhost")]
    if htmx:
        headers.append((b"hx-request", b"true"))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/forgot",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("alpha.localhost", 80),
            "headers": headers,
            "router": routes.router,
        }
    )
    tenant = db.get(Tenant, tid)
    assert tenant is not None
    request.state.tenant = tenant
    return request


def _tokens(db, tid, pid):
    return db.scalars(
        select(AccountToken).where(
            AccountToken.tenant_id == tid, AccountToken.person_id == pid, AccountToken.kind == "password_reset"
        )
    ).all()


def test_public_reset_anti_enumeration_and_token_spend(canonical_account):
    db, tid, pid, email, cid = canonical_account
    root = db.get_transaction()

    # Forgot: real proof + durable outbox for the known account, and a
    # byte-identical response for an unknown address.
    known = routes.forgot_submit(_request(db, tid), email=f" {email.upper()} ", db=db)
    unknown = routes.forgot_submit(_request(db, tid), email=f"missing-{uuid4().hex}@a.edu", db=db)
    assert known.status_code == unknown.status_code == 200
    assert known.body == unknown.body
    assert "on its way" in known.body.decode()
    assert "Returning to login" in known.body.decode()
    body = known.body.decode()
    assert "data-redirect-after='3000'" in body
    assert "data-redirect-href='/login'" in body
    assert "window.location" not in body
    tokens = _tokens(db, tid, pid)
    assert len(tokens) == 1
    queued = db.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == tid, EmailOutbox.kind == "password_reset")
    ).one()
    assert queued.status == "pending" and queued.recipient == email.strip().lower()
    assert "http://alpha.localhost/reset?token=" in queued.html_body
    assert "http://alpha.localhost/reset?token=" in queued.text_body
    raw = queued.text_body.partition("?token=")[2].strip()
    assert hash_token(raw) == tokens[0].token_hash
    assert raw not in known.body.decode()
    assert db.get_transaction() is root and db.is_active
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)

    # Invalid bearer: response shape kept, actual proof and credential intact.
    before = db.get(UserCredential, cid).password_hash
    for htmx, expected_status in ((False, 200), (True, 400)):
        response = routes.reset_submit(
            _request(db, tid, htmx=htmx), token="invalid-<bearer>", password="brandnew9", db=db
        )
        assert response.status_code == expected_status
        assert "Password was not changed" in response.body.decode()
        assert "invalid-<bearer>" not in response.body.decode()
        assert db.get(UserCredential, cid).password_hash == before
        assert _tokens(db, tid, pid)[0].used_at is None
        assert db.get_transaction() is root and db.is_active

    # Genuine reset: credential changes, token spent.
    response = routes.reset_submit(_request(db, tid), token=raw, password="brandnew9", db=db)
    assert response.status_code == 200
    assert "Password updated" in response.body.decode()
    assert "You can now sign in with your new password." in response.body.decode()
    credential = db.get(UserCredential, cid)
    assert verify_password("brandnew9", credential.password_hash)
    assert not verify_password("origpass1", credential.password_hash)
    assert _tokens(db, tid, pid)[0].used_at is not None
    assert raw not in response.body.decode()
    assert db.get_transaction() is root and db.is_active


def test_reset_route_htmx_success(canonical_account):
    db, tid, pid, email, cid = canonical_account
    raw = canonical_lifecycle.request_password_reset(db, tenant_id=tid, email=email)
    assert raw is not None
    response = routes.reset_submit(_request(db, tid, htmx=True), token=raw, password="brandnew9", db=db)
    assert response.status_code == 200
    assert "Password updated" in response.body.decode()
    assert verify_password("brandnew9", db.get(UserCredential, cid).password_hash)
    assert _tokens(db, tid, pid)[0].used_at is not None


def test_reset_form_and_real_owner_share_configured_minimum(canonical_account, admin_session):
    db, tid, pid, email, _ = canonical_account
    key = "password_reset_min_length"
    assert admin_session.scalar(text("SELECT current_user")) in {"app_admin", "platform_api"}
    prior_value = get_all(admin_session).get(key)
    existed = prior_value is not None
    routes.password_reset_policy(admin_session)
    set_many(admin_session, {key: "11"})
    admin_session.commit()
    try:
        form = routes.reset_form(_request(db, tid), token='<unsafe>&"', db=db)
        assert "minlength='11'" in form.body.decode()
        assert "New password (min 11 chars)" in form.body.decode()
        assert "&lt;unsafe&gt;&amp;&quot;" in form.body.decode()
        raw = canonical_lifecycle.request_password_reset(db, tenant_id=tid, email=email)
        assert raw is not None
        refused = routes.reset_submit(_request(db, tid, htmx=True), token=raw, password="brandnew9", db=db)
        assert refused.status_code == 400
        assert _tokens(db, tid, pid)[0].used_at is None
        accepted = routes.reset_submit(_request(db, tid, htmx=True), token=raw, password="brandnew12345", db=db)
        assert accepted.status_code == 200
        assert _tokens(db, tid, pid)[0].used_at is not None
    finally:
        admin_session.rollback()
        if existed:
            set_many(admin_session, {key: prior_value})
        else:
            delete(admin_session, key)
        admin_session.commit()


def test_forgot_keeps_best_effort_enqueue_failure_neutral_and_material_safe(canonical_account, monkeypatch, caplog):
    db, tid, pid, email, _ = canonical_account

    def unavailable(*args, **kwargs):
        raise RuntimeError("provider-private-material")

    monkeypatch.setattr(routes, "enqueue_email", unavailable)
    # Alembic's logging fileConfig (genesis fixture) disables existing
    # loggers in-process; re-enable the route logger for capture.
    import logging

    monkeypatch.setattr(logging.getLogger("app.web.lifecycle"), "disabled", False)
    with caplog.at_level(logging.WARNING, logger="app.web.lifecycle"):
        response = routes.forgot_submit(_request(db, tid), email=email, db=db)
    assert response.status_code == 200 and "on its way" in response.body.decode()
    assert len(_tokens(db, tid, pid)) == 1
    assert "RuntimeError" in caplog.text
    assert "provider-private-material" not in caplog.text
    assert "provider-private-material" not in response.body.decode()
    assert db.is_active


def test_actual_outbox_flush_failure_recovers_same_root_and_caught_outer_commit(canonical_account, monkeypatch):
    db, tid, pid, email, _ = canonical_account
    failed_key = f"failed-reset-{uuid4().hex}"
    failures = []

    def invalid_enqueue(session, **kwargs):
        assert session is db
        session.add(
            EmailOutbox(
                tenant_id=tid,
                idempotency_key=failed_key,
                kind="password_reset",
                recipient=None,
                subject="Failure canary",
                html_body="Failure canary",
            )
        )
        try:
            session.flush()
        except IntegrityError as exc:
            failures.append(getattr(exc.orig, "sqlstate", None))
            raise

    monkeypatch.setattr(routes, "enqueue_email", invalid_enqueue)
    root = db.get_transaction()
    response = routes.forgot_submit(_request(db, tid), email=email, db=db)
    assert failures == ["23502"]
    assert response.status_code == 200 and "on its way" in response.body.decode()
    assert db.get_transaction() is root and db.is_active
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    assert len(_tokens(db, tid, pid)) == 1
    assert db.scalar(select(EmailOutbox.id).where(EmailOutbox.idempotency_key == failed_key)) is None
    db.commit()

    resolve_database_runtime().set_tenant(db, tid)
    assert len(_tokens(db, tid, pid)) == 1
    assert db.scalar(select(EmailOutbox.id).where(EmailOutbox.idempotency_key == failed_key)) is None
