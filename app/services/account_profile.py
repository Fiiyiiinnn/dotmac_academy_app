"""Prepared canonical self-profile writer; source only, no runtime activation.

Every action proves the actual cookie on the caller's Session/root under the
Party → subtype → profile → exact-session lock order. A request projection is
never authority. Participating status/session writers must use Party first.
Avatar replacement intentionally retains the actual non-transactional filesystem
ordering: a database failure can leave the replacement file on disk.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Callable

from dotmac_kernel.identity import person_display_name
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.models.identity_extensions import AcademyAccountProfile
from app.services.identity import AcademyCurrentAccount, current_account
from app.services.profile_policy import profile_policy
from app.services.security import hash_token
from app.services.web_auth import COOKIE

# Existing accepted content-type/extension protocol, not business policy.
AVATAR_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


class ProfileWriteError(Exception):
    """Unclassified store/provider/policy failure; caller root remains required."""


class ProfileInputError(ValueError):
    """Expected user-input refusal with no product database mutation."""


class _Unauthenticated(Exception):
    pass


def _require_recovered_root(db, root):
    if (
        root is None
        or not db.is_active
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=root)
    ):
        raise ProfileWriteError("profile update requires caller transaction rollback") from None


def _lock_self(db: Session, *, tenant_id, digest):
    # This initial read identifies lock keys only. It grants no authority.
    candidate = db.scalars(
        select(AuthSession)
        .where(AuthSession.tenant_id == tenant_id, AuthSession.token_hash == digest)
        .execution_options(populate_existing=True)
    ).first()
    if candidate is None:
        raise _Unauthenticated
    session_id, party_id = candidate.id, candidate.party_id
    party = db.scalars(
        select(Party)
        .where(Party.tenant_id == tenant_id, Party.id == party_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if party is None:
        raise _Unauthenticated
    person = db.scalars(
        select(PartyPerson)
        .where(PartyPerson.party_id == party_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    profile = db.scalars(
        select(AcademyAccountProfile)
        .where(AcademyAccountProfile.tenant_id == tenant_id, AcademyAccountProfile.party_id == party_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    session = db.scalars(
        select(AuthSession)
        .where(
            AuthSession.tenant_id == tenant_id,
            AuthSession.id == session_id,
            AuthSession.party_id == party_id,
            AuthSession.token_hash == digest,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    now = datetime.now(UTC)  # AFTER every blocking authority lock.
    if (
        party.tenant_id != tenant_id
        or party.id != party_id
        or party.party_type != PartyType.person
        or party.is_active is not True
        or type(party.email) is not str
        or not party.email.strip()
        or person is None
        or person.party_id != party_id
        or type(person.first_name) is not str
        or type(person.last_name) is not str
        or profile is None
        or profile.tenant_id != tenant_id
        or profile.party_id != party_id
        or profile.status != "active"
        or session is None
        or session.tenant_id != tenant_id
        or session.party_id != party_id
        or session.id != session_id
        or session.revoked_at is not None
        or session.expires_at <= now
        or not hmac.compare_digest(session.token_hash, digest)
    ):
        raise _Unauthenticated
    return party, person, profile, now


def _write(db: Session, *, request: Request, prepare: Callable) -> AcademyCurrentAccount:
    tenant = require_tenant(request)
    root = db.get_transaction()
    try:
        # No owned additions/mutations before entry; begin_nested may preflush
        # pending caller work. Kernel owns only this local conflict boundary.
        with conflict_savepoint(db):
            root = db.get_transaction()
            raw = request.cookies.get(COOKIE)
            if not raw:
                raise _Unauthenticated
            digest = hash_token(raw)
            del raw
            apply = prepare()
            party, person, profile, now = _lock_self(db, tenant_id=tenant.id, digest=digest)
            del digest
            _require_recovered_root(db, root)
            # Private closure receives genuine locked rows only for this action;
            # no proved context, mutable ORM account or caller UUID escapes.
            if apply(party, person, profile):
                profile.updated_at = now
            db.flush()
            view = current_account(db, tenant_id=tenant.id, party_id=party.id)
            if view is None:
                raise ProfileWriteError("profile outcome is unavailable")
            _require_recovered_root(db, root)
        return view
    except Exception as exc:
        _require_recovered_root(db, root)
        if isinstance(exc, _Unauthenticated):
            raise HTTPException(status_code=303, headers={"Location": "/login"}) from None
        if isinstance(exc, ProfileInputError):
            raise ProfileInputError(str(exc)) from None
        raise ProfileWriteError("profile update failed; no profile database change was saved") from None


def save_names(db: Session, *, request: Request, first_name: str, last_name: str) -> AcademyCurrentAccount:
    def prepare():
        # Technical canonical column limits only; preserve bytes/casing/spacing.
        display_name = person_display_name(first_name, last_name)
        if len(first_name) > 100 or len(last_name) > 100 or len(display_name) > 200:
            raise ProfileInputError("Name exceeds the account storage limit.")

        def apply(party, person, profile):
            changed = person.first_name != first_name or person.last_name != last_name
            person.first_name, person.last_name = first_name, last_name
            party.display_name = display_name
            # Kernel model timestamp behavior remains owner-managed.
            return changed

        return apply

    return _write(db, request=request, prepare=prepare)


def save_preferences(
    db: Session, *, request: Request, email_results: bool, email_digest: bool
) -> AcademyCurrentAccount:
    def prepare():
        def apply(party, person, profile):
            # Preserve actual legacy falsey coercion; truthy non-objects refuse.
            previous = profile.prefs or {}
            if not isinstance(previous, dict):
                raise ProfileInputError("Stored notification preferences must be an object.")
            prefs = {**previous, "email_results": email_results, "email_digest": email_digest}
            changed = profile.prefs != prefs
            profile.prefs = prefs
            return changed

        return apply

    return _write(db, request=request, prepare=prepare)


def save_avatar(
    db: Session, *, request: Request, stream: BinaryIO, content_type: str | None, avatar_root: Path
) -> AcademyCurrentAccount:
    def prepare():
        ext = AVATAR_EXTENSIONS.get((content_type or "").lower())
        if ext is None:
            raise ProfileInputError("Please upload a PNG, JPEG, GIF or WebP image.")
        policy = profile_policy(db)
        data = stream.read(policy.avatar_max_bytes + 1)
        if not data:
            raise ProfileInputError("The uploaded file was empty.")
        if len(data) > policy.avatar_max_bytes:
            raise ProfileInputError(f"Image too large — please keep it under {policy.avatar_limit_label}.")

        def apply(party, person, profile):
            # Only reached after proving and locking the actual self principal.
            dest_dir = avatar_root / str(party.tenant_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            for old in dest_dir.glob(f"{party.id}.*"):
                try:
                    old.unlink()
                except OSError:
                    pass
            (dest_dir / f"{party.id}.{ext}").write_bytes(data)
            path = f"/static/avatars/{party.tenant_id}/{party.id}.{ext}"
            changed = profile.avatar_path != path
            profile.avatar_path = path
            return changed

        return apply

    return _write(db, request=request, prepare=prepare)
