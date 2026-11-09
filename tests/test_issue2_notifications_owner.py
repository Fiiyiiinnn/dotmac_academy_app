# Maintained port of legacy_probe/services/test_notifications_owner.py (issue #2 wave 5).
"""Hosted Academy notification canaries, authored but not executed locally.

Requires the genuine installed assembly, current_account/app_user fixtures and
notification physical FK compatibility. Historical people FKs and missing a004
remain an admission hold; these tests do not manufacture migration evidence.
"""

import ast
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, update

from app.config import Settings
from app.models.identity_extensions import AcademyAccountProfile
from app.models.notification import Notification
from app.services import notifications, settings_store
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web import notifications as routes
from tests.test_issue2_ical_owner import (
    admin_session,
    app_client,
    app_user_session,
    grant_role,
    login_location,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert admin_session and app_client and app_user_session and sign_in_account and tenant_a and tenant_b
from tests.test_issue2_ical_owner import _request, current_account  # noqa: E402

assert current_account

KEY = "notifications_recent_limit"


@pytest.fixture(autouse=True)
def _restore_notification_policy(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    saved = settings_store.get_all(a.admin)
    try:
        yield
    finally:
        a.db.rollback()
        a.admin.rollback()
        if KEY in saved:
            settings_store.set_many(a.admin, {KEY: saved[KEY]})
        else:
            settings_store.delete(a.admin, KEY)
        a.admin.commit()


def _party(a, tenant_id):
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name="Recipient",
        email=f"notification-{uuid4().hex}@example.test",
        is_active=True,
    )
    a.admin.add(party)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=party.id, first_name="Notification", last_name="Recipient"))
    a.admin.flush()
    return party


def _notify(db, tid, pid, title):
    return notifications.notify(
        db, tenant_id=tid, person_id=pid, kind="test", title=title, body="Notification body", link="/account"
    )


def test_service_isolation_order_unread_and_idempotent_utc_mark_all(current_account, tenant_b):
    a = current_account
    other = _party(a, a.tid)
    foreign = _party(a, tenant_b.id)
    now = datetime.now(UTC)
    oldest = _notify(a.admin, a.tid, a.pid, "oldest")
    newest = _notify(a.admin, a.tid, a.pid, "newest")
    already = _notify(a.admin, a.tid, a.pid, "already read")
    other_note = _notify(a.admin, a.tid, other.id, "other account")
    foreign_note = _notify(a.admin, tenant_b.id, foreign.id, "other tenant")
    oldest.created_at, newest.created_at = now - timedelta(hours=2), now
    already.created_at, already.read_at = now - timedelta(hours=1), now - timedelta(minutes=30)
    original_read = already.read_at
    ids = oldest.id, newest.id, already.id, other_note.id, foreign_note.id
    a.admin.commit()
    root = a.db.get_transaction()
    assert notifications.unread_count(a.db, tenant_id=a.tid, person_id=a.pid) == 2
    assert [n.id for n in notifications.recent(a.db, tenant_id=a.tid, person_id=a.pid)] == [ids[1], ids[2], ids[0]]
    assert [n.id for n in notifications.recent(a.db, tenant_id=a.tid, person_id=a.pid, limit=1)] == [ids[1]]
    assert notifications.recent(a.db, tenant_id=tenant_b.id, person_id=a.pid) == []
    assert notifications.unread_count(a.db, tenant_id=a.tid, person_id=foreign.id) == 0
    before = datetime.now(UTC)
    notifications.mark_all_read(a.db, tenant_id=a.tid, person_id=a.pid)
    after = datetime.now(UTC)
    a.db.expire_all()
    assert notifications.unread_count(a.db, tenant_id=a.tid, person_id=a.pid) == 0
    stamps = {n.id: n.read_at for n in notifications.recent(a.db, tenant_id=a.tid, person_id=a.pid)}
    assert before <= stamps[ids[0]] <= after and stamps[ids[0]].utcoffset() == timedelta(0)
    assert stamps[ids[1]] == stamps[ids[0]] and stamps[ids[2]] == original_read
    notifications.mark_all_read(a.db, tenant_id=a.tid, person_id=a.pid)
    a.db.expire_all()
    assert {n.id: n.read_at for n in notifications.recent(a.db, tenant_id=a.tid, person_id=a.pid)} == stamps
    notifications.mark_all_read(a.db, tenant_id=tenant_b.id, person_id=foreign.id)
    assert a.db.get_transaction() is root
    a.db.commit()
    a.admin.expire_all()
    assert a.admin.get(Notification, ids[3]).read_at is None
    assert a.admin.get(Notification, ids[4]).read_at is None


def test_notify_many_and_producer_write_share_caller_rollback(current_account):
    a = current_account
    other = _party(a, a.tid)
    a.admin.commit()
    root = a.db.get_transaction()
    profile = a.db.get(AcademyAccountProfile, a.pid)
    original = profile.prefs
    title = f"rollback-{uuid4().hex}"
    profile.prefs = {"producer": title}
    notifications.notify_many(
        a.db,
        tenant_id=a.tid,
        person_ids=[a.pid, other.id],
        kind="test",
        title=title,
        body="rollback body",
        link="/account",
    )
    assert a.db.get_transaction() is root
    own = notifications.recent(a.db, tenant_id=a.tid, person_id=a.pid)
    recipient = notifications.recent(a.db, tenant_id=a.tid, person_id=other.id)
    assert [n.title for n in own] == [title] and [n.title for n in recipient] == [title]
    assert own[0].body == "rollback body" and own[0].link == "/account" and own[0].read_at is None
    assert a.admin.scalars(select(Notification).where(Notification.title == title)).all() == []
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    a.admin.expire_all()
    assert a.admin.scalars(select(Notification).where(Notification.title == title)).all() == []
    assert a.db.get(AcademyAccountProfile, a.pid).prefs == original


_HTTP_LEG_1 = """
import json, os
from fastapi.testclient import TestClient
from app.main import app

titles = json.loads(os.environ["NOTIFY_TITLES"])
with TestClient(app, raise_server_exceptions=False) as client:
    client.cookies.clear()
    client.cookies.set("session", os.environ["NOTIFY_TOKEN"])
    headers = {"Host": "alpha.localhost"}
    response = client.get("/notifications", headers=headers)
    assert response.status_code == 200, response.text[:300]
    assert titles[2] in response.text and titles[1] in response.text and titles[0] not in response.text
    csrf = client.cookies.get("csrf_token", "")
    print("CSRF:" + csrf)
    response = client.post("/notifications/read-all", headers={**headers, "x-csrf-token": csrf, "HX-Request": "true"})
    assert response.status_code == 200 and response.headers["HX-Redirect"] == "/notifications", response.text[:300]
    response = client.post("/notifications/read-all", headers={**headers, "x-csrf-token": csrf}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == "/notifications", response.text[:300]
print("LEG1-OK")
"""

_HTTP_LEG_2 = """
import os
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app, raise_server_exceptions=False) as client:
    client.cookies.clear()
    client.cookies.set("session", os.environ["NOTIFY_TOKEN"])
    client.cookies.set("csrf_token", os.environ["NOTIFY_CSRF"])
    headers = {"Host": "alpha.localhost"}
    csrf = os.environ["NOTIFY_CSRF"]
    for path in ("/notifications", "/notifications/read-all"):
        if path.endswith("read-all"):
            response = client.post(path, headers={**headers, "x-csrf-token": csrf}, follow_redirects=False)
        else:
            response = client.get(path, headers=headers, follow_redirects=False)
        expected = os.environ["NOTIFY_LOGIN_" + ("READALL" if path.endswith("read-all") else "LIST")]
        assert response.status_code == 302 and response.headers["Location"] == expected, (path, response.status_code, response.headers.get("Location"))
print("LEG2-OK")
"""


def _run_http_leg(genesis_database, script, extra_env):
    import subprocess
    import sys

    from sqlalchemy.engine import make_url

    _admin, superuser, name = genesis_database
    user_url = (
        make_url(superuser).set(database=name, username="app_user", password=None).render_as_string(hide_password=False)
    )
    platform_url = (
        make_url(superuser)
        .set(database=name, username="platform_api", password=None)
        .render_as_string(hide_password=False)
    )
    env = {
        **os.environ,
        "DATABASE_URL": user_url,
        "PLATFORM_DATABASE_URL": platform_url,
        "ENVIRONMENT": "dev",
        "CSRF_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "true",
        **extra_env,
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter + constant scripts
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    return result.stdout


def test_actual_authenticated_routes_use_nondefault_limit_and_hx_redirect(current_account, genesis_database):
    # The full assembled app cannot bind to the per-test genesis DB in this
    # process (one sealed runtime per process), so the HTTP legs run in a
    # subprocess against the same genesis database (boot-smoke pattern).
    import json as _json

    a = current_account
    settings_store.set_many(a.admin, {KEY: "2"})
    now = datetime.now(UTC)
    titles = [f"notification-{uuid4().hex}-{i}" for i in range(3)]
    for i, title in enumerate(titles):
        note = _notify(a.admin, a.tid, a.pid, title)
        note.created_at = now + timedelta(seconds=i)
    # The web facet admits only the default roles; the signed-in account is a student.
    grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    assert isinstance(account, AcademyCurrentAccount) and account.id == a.pid
    out = _run_http_leg(
        genesis_database,
        _HTTP_LEG_1,
        {"NOTIFY_TOKEN": a.token, "NOTIFY_TITLES": _json.dumps(titles)},
    )
    assert "LEG1-OK" in out
    csrf = next(line[5:] for line in out.splitlines() if line.startswith("CSRF:"))
    a.db.expire_all()
    assert notifications.unread_count(a.db, tenant_id=a.tid, person_id=a.pid) == 0
    unread = _notify(a.admin, a.tid, a.pid, "revoked session must not mark this")
    unread_id = unread.id
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    a.admin.commit()
    out = _run_http_leg(
        genesis_database,
        _HTTP_LEG_2,
        {
            "NOTIFY_TOKEN": a.token,
            "NOTIFY_CSRF": csrf,
            "NOTIFY_LOGIN_LIST": login_location("/notifications"),
            "NOTIFY_LOGIN_READALL": login_location("/notifications/read-all"),
        },
    )
    assert "LEG2-OK" in out
    a.admin.expire_all()
    assert a.admin.get(Notification, unread_id).read_at is None


@pytest.mark.parametrize("raw", [None, "", "0", "-1", "true", "1.5", "null", "{}"])
def test_malformed_store_refuses_read_and_write(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    settings_store.delete(a.admin, KEY)
    from dotmac_kernel.settings_models import DomainSetting as _DS
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    a.admin.add(_DS(tenant_id=None, scope_kind="platform", domain="academy", key=KEY, value_text=raw, value_json=None))
    if raw is None:
        # Kernel domain_settings forbids stored NULL (ck_domain_settings_value_alignment);
        # the refusal now happens at storage instead of at read.
        with pytest.raises(_IntegrityError):
            a.admin.flush()
        a.admin.rollback()
        return
    a.admin.flush()
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    with pytest.raises(ValueError, match=KEY):
        routes.notifications_list(request, person=account, db=a.db)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "1.5", "true", "", [], {}])
def test_settings_refuses_nonpositive_or_coerced_notification_limit(value):
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: value})


def test_settings_env_and_store_override_then_delete(current_account, monkeypatch):
    a = current_account
    monkeypatch.setenv("NOTIFICATIONS_RECENT_LIMIT", "7")
    assert Settings().notifications_recent_limit == 7
    assert Settings(**{KEY: " 9 "}).notifications_recent_limit == 9
    monkeypatch.setattr(settings_store.settings, KEY, 7)
    settings_store.set_many(a.admin, {KEY: "2"})
    a.admin.commit()
    assert settings_store.effective(a.db, require_store=True).notifications_recent_limit == 2
    settings_store.delete(a.admin, KEY)
    a.admin.commit()
    assert settings_store.effective(a.db, require_store=True).notifications_recent_limit == 7


def test_route_source_uses_current_account_without_legacy_person():
    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(node.module == "app.models.person" for node in imports)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"notifications_list", "read_all"}:
            person = next(arg for arg in node.args.args if arg.arg == "person")
            assert ast.unparse(person.annotation) == "AcademyCurrentAccount"


def test_required_settings_store_never_substitutes_defaults():
    with pytest.raises(ValueError, match="settings database session is required"):
        settings_store.effective(None, require_store=True)
