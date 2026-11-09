"""Genuine profile UI read values with detached, read-only projection access."""

from copy import deepcopy

import pytest
from sqlalchemy import update

from app.models.identity_extensions import AcademyAccountProfile
from app.services.web_auth import _current_person
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


@pytest.mark.parametrize(
    "prefs",
    [
        None,
        {},
        {"email": {"enabled": True}, "items": [None, {"topic": "lab"}]},
        [],
        [1, {"enabled": False}, None],
        "text",
        True,
        17,
        0.25,
    ],
)
@pytest.mark.parametrize("avatar", [None, "/static/avatars/genuine-profile.png"])
def test_profile_read_projection_preserves_actual_json_shape_and_avatar(current_account, prefs, avatar):
    a = current_account
    profile = a.db.get(AcademyAccountProfile, a.pid)
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.tenant_id == a.tid, AcademyAccountProfile.party_id == a.pid)
        .values(prefs=prefs, avatar_path=avatar)
    )
    a.admin.commit()
    snapshot = _current_person(a.db, a.tid, a.token)
    assert snapshot.avatar_path == avatar
    assert snapshot.prefs == prefs and type(snapshot.prefs) is type(prefs)
    assert profile.avatar_path == avatar and profile.prefs == prefs
    # Existing UI reads keep their native JSON semantics. The guard itself
    # never applies the notifications route's existing `or {}` presentation.
    assert bool(snapshot.prefs) == bool(prefs)
    with pytest.raises((AttributeError, TypeError)):
        snapshot.avatar_path = "changed"
    with pytest.raises((AttributeError, TypeError)):
        snapshot.prefs = {}


def test_returned_nested_prefs_cannot_mutate_stored_snapshot_or_orm(current_account):
    a = current_account
    original = {"email": {"reminders": True}, "topics": [{"topic": "lab"}]}
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs=deepcopy(original), avatar_path="/static/avatars/current.png")
    )
    a.admin.commit()
    profile = a.db.get(AcademyAccountProfile, a.pid)
    snapshot = _current_person(a.db, a.tid, a.token)
    returned = snapshot.prefs
    assert isinstance(returned, dict)
    assert returned is not profile.prefs
    returned["email"]["reminders"] = False
    returned["topics"].append({"topic": "changed"})
    assert snapshot.prefs == original and profile.prefs == original
    assert snapshot.prefs is not snapshot.prefs
    a.db.flush()
    a.db.expire(profile)
    assert profile.prefs == original
    assert _current_person(a.db, a.tid, a.token).prefs == original


def test_next_request_refreshes_profile_without_mutating_previous_snapshot(current_account):
    a = current_account
    previous = _current_person(a.db, a.tid, a.token)
    old_prefs, old_avatar = previous.prefs, previous.avatar_path
    changed = {"email": {"digest": False}}
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs=changed, avatar_path="/static/avatars/new.png")
    )
    a.admin.commit()
    current = _current_person(a.db, a.tid, a.token)
    assert current.prefs == changed and current.avatar_path == "/static/avatars/new.png"
    assert previous.prefs == old_prefs and previous.avatar_path == old_avatar
