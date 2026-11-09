"""Genuine canonical human HTTP surfaces; authored NOT RUN, fixture gates apply."""

import pytest
from dotmac_kernel.models import Party
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select

from app.models.account_token import AccountToken
from app.models.email_outbox import EmailOutbox
from app.web import accounts, instructor
from legacy_probe.conftest import csrf_for
from legacy_probe.services.test_canonical_invitation_issuance import _cohort, _set_actor_role, issuer

assert issuer


@pytest.fixture
def issuer_client(issuer, app_client):
    app_client.cookies.clear()
    app_client.cookies.set("session", issuer.token)
    host = {"Host": "alpha.localhost"}
    # A safe GET issues the signed token bound to this session cookie.
    token = csrf_for(app_client, host, "/login")
    headers = {**host, "x-csrf-token": token}
    return issuer, app_client, headers


def test_admin_invite_and_resend_keep_activation_url_in_durable_email_only(issuer_client):
    a, client, headers = issuer_client
    response = client.post(
        "/admin/users/invite",
        headers=headers,
        data={"first_name": "Human", "last_name": "Invite", "email": "human@a.edu", "role": "student"},
    )
    assert response.status_code == 200
    assert "token=" not in response.text and "Set up your account" not in response.text
    a.db.expire_all()
    target = a.db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.email == "human@a.edu")).one()
    tokens = a.db.scalars(
        select(AccountToken).where(AccountToken.person_id == target.id).order_by(AccountToken.created_at)
    ).all()
    emails = a.db.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid, EmailOutbox.recipient == target.email)
    ).all()
    assert len(tokens) == len(emails) == 1
    assert emails[0].idempotency_key == f"account-invite:{target.id}:{tokens[0].token_hash}"
    assert "token=" in emails[0].html_body and "token=" in emails[0].text_body
    response = client.post(f"/admin/users/{target.id}/invite-link", headers=headers)
    assert response.status_code == 200 and "token=" not in response.text
    a.db.expire_all()
    tokens = a.db.scalars(
        select(AccountToken).where(AccountToken.person_id == target.id).order_by(AccountToken.created_at)
    ).all()
    assert len(tokens) == 2 and tokens[0].used_at is not None and tokens[1].used_at is None


@pytest.mark.parametrize("surface", ["admin", "instructor"])
def test_enqueue_false_is_ordinary_response_not_domain_rollback(issuer_client, monkeypatch, surface):
    a, client, headers = issuer_client
    if surface == "admin":
        monkeypatch.setattr(accounts, "enqueue_email", lambda *args, **kwargs: False)
        path, data = (
            "/admin/users/invite",
            {"first_name": "Not", "last_name": "Queued", "email": "false@a.edu", "role": "student"},
        )
    else:
        _set_actor_role(a, "instructor")
        cohort = _cohort(a.db, a.tid)
        a.db.commit()
        resolve_database_runtime().set_tenant(a.db, a.tid)
        monkeypatch.setattr(instructor, "enqueue_email", lambda *args, **kwargs: False)
        path, data = (
            f"/instructor/cohorts/{cohort.id}/invite",
            {"first_name": "Not", "last_name": "Queued", "email": "false@a.edu"},
        )
    response = client.post(path, headers=headers, data=data)
    assert response.status_code == 200 and "could not be queued" in response.text
    assert "token=" not in response.text
    a.db.expire_all()
    target = a.db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.email == "false@a.edu")).one()
    assert a.db.scalars(
        select(AccountToken).where(AccountToken.person_id == target.id, AccountToken.used_at.is_(None))
    ).one()
