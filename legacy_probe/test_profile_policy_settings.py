"""Strict profile policy/default UI and avatar provider canaries; hosted only."""

import sys
from io import BytesIO
from pathlib import Path

import pytest

from app.config import PROFILE_POLICY_SHAPES, Settings, validate_profile_policy_value
from app.models.identity_extensions import AcademyAccountProfile
from app.services import account_profile as writer
from app.services import settings_store
from app.services.profile_policy import profile_policy
from legacy_probe.services.test_canonical_current_user import _request, current_account
from legacy_probe.web.test_canonical_self_profile import _PNG, profile_client

assert current_account and profile_client


def test_defaults_exist_only_in_product_settings_and_env_layer():
    configured = Settings(_env_file=None)
    assert configured.account_avatar_max_bytes == 1048576
    assert configured.account_email_results_default is True
    assert configured.account_email_digest_default is True


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.5, "", "false", "1.5"])
def test_invalid_upload_bound_refuses(value):
    with pytest.raises(ValueError):
        validate_profile_policy_value("account_avatar_max_bytes", value)


@pytest.mark.parametrize("key", ["account_email_results_default", "account_email_digest_default"])
@pytest.mark.parametrize("value", [None, 1, 0, [], {}, "maybe", ""])
def test_invalid_preference_default_refuses(key, value):
    with pytest.raises(ValueError):
        validate_profile_policy_value(key, value)


def test_db_over_env_and_delivery_policy_are_distinct(current_account, monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "account_avatar_max_bytes": "17",
            "account_email_results_default": "false",
            "account_email_digest_default": "true",
            "email_auto_on_pass": "true",
            "email_digest_enabled": "false",
        },
    )
    policy = profile_policy(current_account.db)
    assert policy.avatar_max_bytes == 17
    assert policy.email_results_default is False and policy.email_digest_default is True
    effective = settings_store.effective(current_account.db, require_store=True)
    assert effective.email_auto_on_pass is True and effective.email_digest_enabled is False


@pytest.mark.parametrize("key", list(PROFILE_POLICY_SHAPES))
def test_null_stored_policy_is_not_absence(current_account, monkeypatch, key):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: None})
    with pytest.raises(ValueError):
        profile_policy(current_account.db)
    with pytest.raises(ValueError):
        settings_store.set_many(current_account.db, {key: None})


def test_strict_store_failure_and_missing_session_have_no_fallback(current_account, monkeypatch):
    def fail(db):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(settings_store, "get_all", fail)
    with pytest.raises(RuntimeError):
        profile_policy(current_account.db)
    with pytest.raises(ValueError):
        profile_policy(None)


@pytest.mark.parametrize("results,digest", [(True, True), (False, True), (False, False)])
def test_ui_missing_keys_use_policy_stored_keys_win(profile_client, monkeypatch, results, digest):
    a, client, headers, _csrf = profile_client
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {"account_email_results_default": str(results), "account_email_digest_default": str(digest)},
    )
    text = client.get("/account/notifications", headers=headers).text
    results_tag = text.split('id="email_results"', 1)[1].split(">", 1)[0]
    digest_tag = text.split('id="email_digest"', 1)[1].split(">", 1)[0]
    assert ("checked" in results_tag) is results
    assert ("checked" in digest_tag) is digest
    from sqlalchemy import update

    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs={"email_results": not results, "email_digest": not digest})
    )
    a.admin.commit()
    text = client.get("/account/notifications", headers=headers).text
    assert ("checked" in text.split('id="email_results"', 1)[1].split(">", 1)[0]) is not results
    assert ("checked" in text.split('id="email_digest"', 1)[1].split(">", 1)[0]) is not digest


@pytest.mark.parametrize("data", [b"", b"12345"])
def test_empty_and_oversize_read_limit_plus_one_before_any_filesystem(current_account, monkeypatch, tmp_path, data):
    a = current_account
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"account_avatar_max_bytes": "4"})

    class Stream(BytesIO):
        def read(self, count):
            assert count == 5
            return super().read(count)

    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem touched for invalid upload")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    with pytest.raises(writer.ProfileInputError):
        writer.save_avatar(
            a.db,
            request=_request(a.tenant, a.token),
            stream=Stream(data),
            content_type="image/png",
            avatar_root=tmp_path,
        )
    assert a.db.get(AcademyAccountProfile, a.pid).avatar_path is None


@pytest.mark.parametrize(
    "mime,extension", [("image/png", "png"), ("image/jpeg", "jpg"), ("image/gif", "gif"), ("image/webp", "webp")]
)
def test_avatar_provider_order_replacement_and_proof_before_filesystem(
    current_account, monkeypatch, tmp_path, mime, extension
):
    a = current_account
    directory = tmp_path / str(a.tid)
    directory.mkdir()
    old = directory / f"{a.pid}.old"
    old.write_bytes(b"prior")
    events = []
    proved = False
    original_lock = writer._lock_self
    original_mkdir, original_unlink, original_write = Path.mkdir, Path.unlink, Path.write_bytes

    def lock(*args, **kwargs):
        nonlocal proved
        rows = original_lock(*args, **kwargs)
        proved = True
        return rows

    def mkdir(path, *args, **kwargs):
        assert proved
        events.append("mkdir")
        return original_mkdir(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        assert proved
        events.append("delete")
        return original_unlink(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        assert proved
        events.append("write")
        assert a.db.get(AcademyAccountProfile, a.pid).avatar_path is None
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(writer, "_lock_self", lock)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "write_bytes", write)
    result = writer.save_avatar(
        a.db, request=_request(a.tenant, a.token), stream=BytesIO(_PNG), content_type=mime, avatar_root=tmp_path
    )
    assert events == ["mkdir", "delete", "write"]
    assert result.avatar_path == f"/static/avatars/{a.tid}/{a.pid}.{extension}"
    assert not old.exists()
    assert (directory / f"{a.pid}.{extension}").read_bytes() == _PNG


def test_prior_avatar_delete_oserror_is_swallowed_as_actual_protocol(current_account, monkeypatch, tmp_path):
    a = current_account
    directory = tmp_path / str(a.tid)
    directory.mkdir()
    old = directory / f"{a.pid}.jpg"
    old.write_bytes(b"old")
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path == old:
            raise OSError("injected prior-file deletion failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    result = writer.save_avatar(
        a.db, request=_request(a.tenant, a.token), stream=BytesIO(_PNG), content_type="image/png", avatar_root=tmp_path
    )
    assert result.avatar_path == f"/static/avatars/{a.tid}/{a.pid}.png"
    assert old.read_bytes() == b"old"
    assert (directory / f"{a.pid}.png").read_bytes() == _PNG


@pytest.mark.parametrize("value", [sys.maxsize, sys.maxsize + 1, 10**400, str(sys.maxsize), str(10**400)])
def test_avatar_policy_refuses_unrepresentable_read_count_in_settings_and_store(current_account, monkeypatch, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, account_avatar_max_bytes=value)
    with pytest.raises(ValueError):
        validate_profile_policy_value("account_avatar_max_bytes", value)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"account_avatar_max_bytes": str(value)})
    with pytest.raises(ValueError):
        settings_store.effective(current_account.db, require_store=True)
    with pytest.raises(ValueError):
        profile_policy(current_account.db)
    # Refusal occurs before the app_user session could attempt settings writes.
    with pytest.raises(ValueError):
        settings_store.set_many(current_account.db, {"account_avatar_max_bytes": str(value)})


@pytest.mark.parametrize("value", [sys.maxsize - 1, str(sys.maxsize - 1)])
def test_largest_representable_avatar_bound_has_safe_exact_read_count_and_label(
    current_account, monkeypatch, tmp_path, value
):
    configured = Settings(_env_file=None, account_avatar_max_bytes=value)
    assert configured.account_avatar_max_bytes == sys.maxsize - 1
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"account_avatar_max_bytes": str(value)})
    policy = profile_policy(current_account.db)
    assert policy.avatar_max_bytes == sys.maxsize - 1
    assert policy.avatar_limit_label == f"{sys.maxsize - 1} bytes"

    class EmptyProbe:
        def read(self, count):
            # Observe the actual adapter read count without allocating a huge
            # payload: the maximum allowed policy leaves exactly one byte.
            assert count == sys.maxsize
            return b""

    with pytest.raises(writer.ProfileInputError, match="empty"):
        writer.save_avatar(
            current_account.db,
            request=_request(current_account.tenant, current_account.token),
            stream=EmptyProbe(),
            content_type="image/png",
            avatar_root=tmp_path,
        )
    assert list(tmp_path.iterdir()) == []


def test_avatar_label_uses_exact_integer_units_at_default_and_nonmultiple(current_account, monkeypatch):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"account_avatar_max_bytes": "1048576"})
    assert profile_policy(current_account.db).avatar_limit_label == "1 MB"
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"account_avatar_max_bytes": "1048577"})
    assert profile_policy(current_account.db).avatar_limit_label == "1048577 bytes"
