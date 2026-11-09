"""Hosted canonical self-profile canaries; authored only in this source capsule.

The four route scenarios port actual-a5 test_account assertions with genuine
Party/subtype/profile/session setup. Password/reminder legacy tests stay gated.
"""

from io import BytesIO

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException, UploadFile
from sqlalchemy import text, update

from app.models.identity_extensions import AcademyAccountProfile
from app.services import account_profile as writer
from app.services.web_auth import _current_person
from app.web import account
from legacy_probe.conftest import grant_role
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def profile_client(current_account, app_client, monkeypatch, tmp_path):
    a = current_account
    # The web facet admits only the default roles; the signed-in account is a student.
    grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
    a.admin.commit()
    monkeypatch.setattr(account, "AVATAR_ROOT", tmp_path)
    app_client.cookies.clear()
    app_client.cookies.set("session", a.token)
    h = {"Host": "alpha.localhost"}
    app_client.get("/account", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    return a, app_client, h, csrf


def test_profile_update_changes_name(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account", headers={**h, "x-csrf-token": csrf}, data={"first_name": "Newfirst", "last_name": "Newlast"}
    )
    assert r.status_code == 200
    r = app_client.get("/account", headers=h)
    assert r.status_code == 200
    assert "Newfirst" in r.text
    assert "Newlast" in r.text
    assert a.email in r.text  # Same email-read-only assertion on canonical fixture identity.
    a.db.expire_all()
    assert a.db.get(Party, a.pid).display_name == "Newfirst Newlast"
    assert a.db.get(Party, a.pid).email == a.email
    assert a.db.get(AcademyAccountProfile, a.pid).status == "active"


def test_avatar_upload_sets_path(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account/avatar", headers={**h, "x-csrf-token": csrf}, files={"file": ("me.png", _PNG, "image/png")}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"
    a.db.expire_all()
    profile = a.db.get(AcademyAccountProfile, a.pid)
    assert profile.avatar_path == f"/static/avatars/{a.tid}/{a.pid}.png"
    r = app_client.get("/account", headers=h)
    assert "/static/avatars/" in r.text


def test_avatar_upload_rejects_non_image(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account/avatar",
        headers={**h, "x-csrf-token": csrf},
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") is None
    assert "image" in r.text.lower() or "PNG" in r.text
    a.db.expire_all()
    assert a.db.get(AcademyAccountProfile, a.pid).avatar_path is None


def test_notifications_toggle_persists(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post("/account/notifications", headers={**h, "x-csrf-token": csrf}, data={"email_digest": "1"})
    assert r.status_code == 200
    a.db.expire_all()
    prefs = a.db.get(AcademyAccountProfile, a.pid).prefs
    assert prefs.get("email_results") is False
    assert prefs.get("email_digest") is True
    r = app_client.get("/account/notifications", headers=h)
    assert r.status_code == 200
    assert "Result emails" in r.text


def test_names_preserve_legacy_max_bytes_and_atomic_derived_display(current_account):
    a = current_account
    root = a.db.get_transaction()
    before = a.db.get(AcademyAccountProfile, a.pid).updated_at
    first, last = " " + "É" * 79, "Q" * 79 + " "
    result = writer.save_names(a.db, request=_request(a.tenant, a.token), first_name=first, last_name=last)
    assert result.first_name == first and result.last_name == last
    assert a.db.get(PartyPerson, a.pid).first_name == first
    assert a.db.get(PartyPerson, a.pid).last_name == last
    assert a.db.get(Party, a.pid).display_name == f"{first} {last}"
    assert a.db.get(Party, a.pid).email == a.email
    assert a.db.get(AcademyAccountProfile, a.pid).status == "active"
    assert a.db.get(AcademyAccountProfile, a.pid).updated_at >= before
    assert a.db.get_transaction() is root
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
    with pytest.raises((AttributeError, TypeError)):
        result.first_name = "forged"


@pytest.mark.parametrize("state", ["revoked", "inactive", "suspended", "expired"])
@pytest.mark.parametrize("route", ["names", "avatar", "preferences"])
def test_stale_projection_does_not_authorize_any_writer(current_account, state, route, tmp_path):
    from datetime import UTC, datetime, timedelta

    a = current_account
    stale = _current_person(a.db, a.tid, a.token)
    original = (stale.first_name, stale.last_name, stale.avatar_path, stale.prefs)
    if state == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif state == "expired":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == a.session_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    elif state == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    a.admin.commit()
    req = _request(a.tenant, a.token)
    with pytest.raises(HTTPException) as caught:
        if route == "names":
            account.profile_save(req, "Forged", "Name", stale, a.db)
        elif route == "avatar":
            account.avatar_upload(req, UploadFile(BytesIO(_PNG), headers={"content-type": "image/png"}), stale, a.db)
        else:
            account.notifications_save(req, "1", "1", stale, a.db)
    assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
    profile = a.db.get(AcademyAccountProfile, a.pid)
    subtype = a.db.get(PartyPerson, a.pid)
    assert (subtype.first_name, subtype.last_name, profile.avatar_path, profile.prefs) == original
    assert list(tmp_path.iterdir()) == []
    a.db.commit()  # Caught refusal must leave a committable caller root.


@pytest.mark.parametrize("cookie", [None, "unpersisted-cookie", "tampered"])
def test_cookie_and_tenant_are_actual_write_authority(current_account, tenant_b, cookie):
    a = current_account
    raw = a.token + "x" if cookie == "tampered" else cookie
    with pytest.raises(HTTPException) as caught:
        writer.save_names(a.db, request=_request(a.tenant, raw), first_name="X", last_name="Y")
    assert caught.value.status_code == 303
    with pytest.raises(HTTPException) as caught:
        writer.save_names(a.db, request=_request(tenant_b, a.token), first_name="X", last_name="Y")
    assert caught.value.status_code == 303
    assert a.db.get(PartyPerson, a.pid).first_name == "Sign"


@pytest.mark.parametrize("prefs", [None, {}, [], "", 0, False])
def test_preferences_preserve_actual_falsey_merge_semantics(current_account, prefs):
    a = current_account
    profile = a.db.get(AcademyAccountProfile, a.pid)
    profile.prefs = prefs
    a.db.flush()
    result = writer.save_preferences(a.db, request=_request(a.tenant, a.token), email_results=False, email_digest=True)
    assert result.prefs == {"email_results": False, "email_digest": True}


@pytest.mark.parametrize("prefs", [[1], "wrong-shape", 1, True])
def test_truthy_nonobject_preferences_refuse_without_normalizing_native_json(current_account, prefs):
    a = current_account
    profile = a.db.get(AcademyAccountProfile, a.pid)
    profile.prefs = prefs
    a.db.flush()
    root = a.db.get_transaction()
    with pytest.raises(writer.ProfileInputError):
        writer.save_preferences(a.db, request=_request(a.tenant, a.token), email_results=False, email_digest=False)
    assert a.db.get_transaction() is root
    assert _current_person(a.db, a.tid, a.token).prefs == prefs
    a.db.commit()


def test_preferences_merge_unrelated_keys_and_absent_checkboxes(current_account):
    a = current_account
    a.db.get(AcademyAccountProfile, a.pid).prefs = {"other": {"nested": [1, None]}, "email_results": True}
    a.db.flush()
    stale = _current_person(a.db, a.tid, a.token)
    request = _request(a.tenant, a.token)
    # The rendered page reads request.url, which needs a path in the scope.
    request.scope.update({"path": "/account/notifications", "query_string": b""})
    r = account.notifications_save(request, None, None, stale, a.db)
    assert r.status_code == 200
    assert a.db.get(AcademyAccountProfile, a.pid).prefs == {
        "other": {"nested": [1, None]},
        "email_results": False,
        "email_digest": False,
    }


@pytest.mark.parametrize("kind", ["names", "preferences", "avatar"])
def test_caught_failure_then_outer_commit_has_zero_partial_database_change(
    current_account, monkeypatch, tmp_path, kind
):
    a = current_account
    before = _current_person(a.db, a.tid, a.token)
    original_display = a.db.get(Party, a.pid).display_name
    stamp = a.db.get(AcademyAccountProfile, a.pid).updated_at
    root = a.db.get_transaction()

    def fail(*args, **kwargs):
        raise RuntimeError("injected postflush outcome failure")

    monkeypatch.setattr(writer, "current_account", fail)
    with pytest.raises(writer.ProfileWriteError):
        if kind == "names":
            writer.save_names(a.db, request=_request(a.tenant, a.token), first_name="Changed", last_name="Name")
        elif kind == "preferences":
            writer.save_preferences(a.db, request=_request(a.tenant, a.token), email_results=False, email_digest=True)
        else:
            writer.save_avatar(
                a.db,
                request=_request(a.tenant, a.token),
                stream=BytesIO(_PNG),
                content_type="image/png",
                avatar_root=tmp_path,
            )
    assert a.db.get_transaction() is root and a.db.is_active
    a.db.commit()

    resolve_database_runtime().set_tenant(a.db, a.tid)
    after = _current_person(a.db, a.tid, a.token)
    assert (after.first_name, after.last_name, after.avatar_path, after.prefs) == (
        before.first_name,
        before.last_name,
        before.avatar_path,
        before.prefs,
    )
    assert a.db.get(Party, a.pid).display_name == original_display
    assert a.db.get(AcademyAccountProfile, a.pid).updated_at == stamp
    if kind == "avatar":
        # Honest retained limitation: filesystem replacement is not rolled back.
        assert (tmp_path / str(a.tid) / f"{a.pid}.png").read_bytes() == _PNG


@pytest.mark.parametrize("kind", ["names", "preferences", "avatar"])
def test_profile_timestamp_tracks_persisted_change_and_preserves_noop(current_account, tmp_path, kind):
    a = current_account
    req = _request(a.tenant, a.token)

    def save(changed=False):
        if kind == "names":
            return writer.save_names(a.db, request=req, first_name="Second" if changed else "First", last_name="Name")
        if kind == "preferences":
            return writer.save_preferences(a.db, request=req, email_results=changed, email_digest=True)
        return writer.save_avatar(
            a.db,
            request=req,
            stream=BytesIO(_PNG),
            content_type="image/jpeg" if changed else "image/png",
            avatar_root=tmp_path,
        )

    profile = a.db.get(AcademyAccountProfile, a.pid)
    created_stamp = profile.updated_at
    first = save()
    changed_stamp = profile.updated_at
    assert changed_stamp > created_stamp
    again = save()
    assert profile.updated_at == changed_stamp
    assert (again.first_name, again.last_name, again.avatar_path, again.prefs) == (
        first.first_name,
        first.last_name,
        first.avatar_path,
        first.prefs,
    )
    save(changed=True)
    assert profile.updated_at > changed_stamp
