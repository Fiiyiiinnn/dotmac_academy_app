"""Authored hosted public reset delegation canaries with genuine canonical state.

These call the route functions with a real app_user session; they do not qualify
the still-required full TestClient/CSRF or original lifecycle fixture conversion.
"""

from uuid import uuid4

import pytest
from dotmac_kernel.models import Tenant, UserCredential
from dotmac_kernel.security import verify_password
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.models.account_token import AccountToken
from app.models.email_outbox import EmailOutbox
from app.models.platform_settings import PlatformSetting
from app.services import canonical_lifecycle
from app.services.security import hash_token
from app.services.settings_store import delete, set_many
from app.web import lifecycle as routes
from legacy_probe.services.test_canonical_lifecycle import canonical_account

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


def test_forgot_creates_real_proof_and_durable_outbox_with_identical_unknown_response(canonical_account):
    db, tid, pid, email, _ = canonical_account
    root = db.get_transaction()
    known = routes.forgot_submit(_request(db, tid), email=f" {email.upper()} ", db=db)
    unknown = routes.forgot_submit(_request(db, tid), email=f"missing-{uuid4().hex}@a.edu", db=db)
    assert known.status_code == unknown.status_code == 200
    assert known.body == unknown.body
    assert "on its way" in known.body.decode()
    assert "Returning to login" in known.body.decode()
    # The inline redirect script became data attributes (CSP forbids inline script).
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


@pytest.mark.parametrize("htmx", [False, True])
def test_reset_route_changes_real_credential_and_preserves_response(canonical_account, htmx):
    db, tid, pid, email, cid = canonical_account
    raw = canonical_lifecycle.request_password_reset(db, tenant_id=tid, email=email)
    assert raw is not None
    root = db.get_transaction()
    response = routes.reset_submit(_request(db, tid, htmx=htmx), token=raw, password="brandnew9", db=db)
    assert response.status_code == 200
    assert "Password updated" in response.body.decode()
    assert "You can now sign in with your new password." in response.body.decode()
    credential = db.get(UserCredential, cid)
    assert verify_password("brandnew9", credential.password_hash)
    assert not verify_password("origpass1", credential.password_hash)
    assert _tokens(db, tid, pid)[0].used_at is not None
    assert raw not in response.body.decode()
    assert db.get_transaction() is root and db.is_active


@pytest.mark.parametrize("htmx,expected_status", [(False, 200), (True, 400)])
def test_invalid_reset_response_does_not_spend_actual_proof(canonical_account, htmx, expected_status):
    db, tid, pid, email, cid = canonical_account
    raw = canonical_lifecycle.request_password_reset(db, tenant_id=tid, email=email)
    assert raw is not None
    before = db.get(UserCredential, cid).password_hash
    root = db.get_transaction()
    response = routes.reset_submit(_request(db, tid, htmx=htmx), token="invalid-<bearer>", password="brandnew9", db=db)
    assert response.status_code == expected_status
    assert "Password was not changed" in response.body.decode()
    assert "invalid-<bearer>" not in response.body.decode()
    assert db.get(UserCredential, cid).password_hash == before
    assert _tokens(db, tid, pid)[0].used_at is None
    assert db.get_transaction() is root and db.is_active


def test_reset_form_and_real_owner_share_configured_minimum(canonical_account, admin_session):
    db, tid, pid, email, _ = canonical_account
    key = "password_reset_min_length"
    assert admin_session.scalar(text("SELECT current_user")) in {"app_admin", "platform_api"}
    prior = admin_session.get(PlatformSetting, key)
    existed, prior_value = prior is not None, prior.value if prior is not None else None
    # Validate the exact prior state before committed setup; malformed prior
    # configuration refuses before writes rather than breaking restoration.
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
    assert failures == ["23502"]  # Reached the actual required recipient constraint.
    assert response.status_code == 200 and "on its way" in response.body.decode()
    assert db.get_transaction() is root and db.is_active
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    assert len(_tokens(db, tid, pid)) == 1  # Preserve issued proof / best-effort delivery semantics.
    assert db.scalar(select(EmailOutbox.id).where(EmailOutbox.idempotency_key == failed_key)) is None
    db.commit()  # Caller deliberately catches the enqueue failure and commits.

    resolve_database_runtime().set_tenant(db, tid)
    assert len(_tokens(db, tid, pid)) == 1
    assert db.scalar(select(EmailOutbox.id).where(EmailOutbox.idempotency_key == failed_key)) is None
