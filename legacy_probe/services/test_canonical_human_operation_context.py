"""AUTHORED HOSTED ONLY: genuine PostgreSQL requests/roots, never local execution.

Requires composed canonical identity fixtures and installed role.grant action.
No fake context, unbound method invocation or request DTO supplies authority.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, PartyType, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.models.identity_extensions import AcademyAccountProfile
from app.services import audit_context as boundary
from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.security import decode_access_token, hash_token
from legacy_probe.services.test_canonical_current_user import _request, current_account
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

assert current_account


@pytest.fixture
def human(current_account):
    a = current_account
    a.db.rollback()
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "instructor")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="instructor", name="Instructor")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    a.role_id = role.id
    return a


def _roster(a, request=None):
    return boundary.bulk_enroll_web_roster(
        a.db, request=request or _request(a.tenant, a.token), cohort_id=uuid4(), emails=[a.email]
    )


def _api_request(a, token=None):
    return Request(
        {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token or a.token}".encode())],
            "state": {"tenant": a.tenant},
        }
    )


def test_genuine_instance_root_scope_and_roster_nonaudit(human):
    a = human
    root = a.db.get_transaction()
    before = a.db.scalar(select(text("count(*)")).select_from(AuditEvent))
    with _roster(a) as context:
        assert context.actor_id == a.pid and context.actor_is_admin is False
        assert context.resources.emails == (a.email,)
        assert context.db is a.db and context.root is root
        captured = context
    assert captured.__dict__ == {}
    assert a.db.get_transaction() is root
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
    assert a.db.scalar(select(text("count(*)")).select_from(AuditEvent)) == before


@pytest.fixture
def admin_human(human):
    a = human
    a.db.rollback()
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="admin", name="Admin")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    grant_roles = [Role(tenant_id=a.tid, slug="v2-hosted-" + uuid4().hex, name="Hosted grant") for _ in range(2)]
    a.admin.add_all(grant_roles)
    a.admin.flush()
    a.grant_role_ids = tuple(row.id for row in grant_roles)
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    return a


@pytest.mark.parametrize("field", ["entity_id", "party_id", "role_id", "cohort_id", "track_id", "emails"])
def test_independent_scalar_resource_snapshot_refuses_frozen_dto_tamper_before_dml(admin_human, field):
    a = admin_human
    first_role, second_role = a.grant_role_ids
    if field in {"entity_id", "party_id", "role_id"}:
        operation = boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=first_role)
    else:
        operation = boundary.bulk_enroll_web_roster(
            a.db, request=_request(a.tenant, a.token), cohort_id=uuid4(), track_id=uuid4(), emails=[a.email]
        )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement.lstrip().split()[0].upper())

    with pytest.raises(boundary.HumanOperationError):
        with operation as context:
            # Genuine B grant reproduces the original A→B substitution concern.
            a.db.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=second_role))
            a.db.flush()
            a.db.get(Party, a.pid).display_name = "MUST ROLLBACK"
            replacement = ("different@a.edu",) if field == "emails" else second_role
            object.__setattr__(context.resources, field, replacement)
            event.listen(a.db.get_bind(), "before_cursor_execute", capture)
            try:
                with pytest.raises(boundary.HumanOperationError):
                    context.require()
                assert statements == []
                # Restoring the DTO cannot clear the failure latch.
                with pytest.raises(boundary.HumanOperationError):
                    context.revalidate()
                assert statements == []
            finally:
                event.remove(a.db.get_bind(), "before_cursor_execute", capture)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).display_name == "Sign In"
    assert (
        a.db.scalar(
            select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == second_role)
        )
        is None
    )
    assert context.__dict__ == {}


@pytest.mark.parametrize("transport", ["api", "web", "web_header"])
def test_same_request_credential_mutation_latches_even_when_restored_and_caught(admin_human, transport):
    a = admin_human
    request = _api_request(a) if transport == "api" else _request(a.tenant, a.token)
    if transport == "api":
        operation = boundary.grant_api_role(a.db, request=request, person_id=a.pid, role_id=a.grant_role_ids[0])
    else:
        operation = _roster(a, request)
    with pytest.raises(boundary.HumanOperationError):
        with operation as context:
            a.db.get(Party, a.pid).display_name = "MUST ROLLBACK"
            if transport in {"api", "web_header"}:
                original = list(request.scope["headers"])
                request.scope["headers"][:] = (
                    [(b"authorization", b"Bearer different-session-token")]
                    if transport == "api"
                    else [(b"cookie", b"session=different-session-token")]
                )
            else:
                original = request.cookies["session"]
                request.cookies["session"] = "different-session-token"
            with pytest.raises(boundary.HumanOperationError):
                context.require()
            if transport in {"api", "web_header"}:
                request.scope["headers"][:] = original
            else:
                request.cookies["session"] = original
            with pytest.raises(boundary.HumanOperationError):
                context.require()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).display_name == "Sign In"
    assert context.__dict__ == {}


def test_preexisting_grant_cannot_manufacture_new_audit_event(admin_human):
    a = admin_human
    before = a.db.scalar(select(text("count(*)")).select_from(AuditEvent))
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=a.role_id) as context:
            with pytest.raises(boundary.HumanOperationError):
                context.audit()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(select(text("count(*)")).select_from(AuditEvent)) == before


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_audited_new_grant_must_survive_with_exact_row_identity(admin_human, change):
    a = admin_human
    role_id = a.grant_role_ids[0]
    before = a.db.scalar(select(text("count(*)")).select_from(AuditEvent))
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=role_id) as context:
            context.grant_role()
            grant = a.db.get(PartyRoleGrant, context.expected_grant_id)
            a.db.delete(grant)
            a.db.flush()
            if change == "replace":
                a.db.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role_id))
                a.db.flush()
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert (
        a.db.scalar(
            select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == role_id)
        )
        is None
    )
    assert a.db.scalar(select(text("count(*)")).select_from(AuditEvent)) == before


def test_api_jwt_expiry_after_real_party_wait_is_independent_of_session_expiry(admin_human, monkeypatch):
    a = admin_human
    claims = decode_access_token(a.token)
    assert claims is not None
    expiry = datetime.fromtimestamp(claims["exp"], UTC)
    a.db.rollback()
    a.admin.execute(
        update(AuthSession).where(AuthSession.id == a.session_id).values(expires_at=expiry + timedelta(days=1))
    )
    a.admin.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry + timedelta(seconds=1) if release.is_set() else datetime.now(UTC)

    monkeypatch.setattr(boundary, "datetime", Clock)

    def leader():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                with boundary.grant_api_role(db, request=_api_request(a), person_id=a.pid, role_id=a.grant_role_ids[0]):
                    pytest.fail("expired JWT survived real lock wait with live session")
            assert caught.value.status_code == 401
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)


def test_actual_multi_target_party_locks_precede_authority_in_sorted_order(human):
    a = human
    a.db.rollback()
    targets = [
        Party(
            tenant_id=a.tid,
            party_type=PartyType.person,
            display_name="Hosted target",
            email="target-" + uuid4().hex + "@a.edu",
            is_active=True,
        )
        for _ in range(2)
    ]
    a.admin.add_all(targets)
    a.admin.flush()
    expected = sorted([a.pid, *(row.id for row in targets)])
    emails = [row.email for row in reversed(targets)] + [a.email]
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    observed = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and "WHERE parties.tenant_id" in statement:
            observed.extend(value for value in parameters.values() if value in expected)

    event.listen(a.db.get_bind(), "before_cursor_execute", capture)
    try:
        with boundary.bulk_enroll_web_roster(
            a.db, request=_request(a.tenant, a.token), cohort_id=uuid4(), emails=emails
        ) as context:
            assert observed == expected
            assert set(context.parties) == set(expected)
    finally:
        event.remove(a.db.get_bind(), "before_cursor_execute", capture)


def test_mutating_module_descriptor_cannot_change_fixed_action(admin_human):
    a = admin_human
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(
            a.db, request=_api_request(a), person_id=a.pid, role_id=a.grant_role_ids[0]
        ) as context:
            original = context.operation.action
            try:
                object.__setattr__(context.operation, "action", "role.create")
                with pytest.raises(boundary.HumanOperationError):
                    context.require()
            finally:
                object.__setattr__(context.operation, "action", original)
    a.db.commit()


def _owned_traceback_locals(error):
    frames = []
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename == boundary.__file__:
                frames.append((frame.f_code.co_name, dict(frame.f_locals)))
            traceback = traceback.tb_next
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
    return frames


def _assert_owned_locals_cleared(error, expected):
    frames = _owned_traceback_locals(error)
    for name, aliases in expected.items():
        matching = [values for function, values in frames if function == name]
        assert matching, "expected own helper failure frame is absent"
        for values in matching:
            assert all(values.get(alias) is None for alias in aliases), "own helper retained a cleared alias"


@pytest.mark.parametrize("transport", ["api", "web"])
def test_credential_refusal_retained_own_helper_frames_clear_request_and_raw_aliases(admin_human, transport):
    a = admin_human
    request = _api_request(a) if transport == "api" else _request(a.tenant, a.token)
    operation = (
        boundary.grant_api_role(a.db, request=request, person_id=a.pid, role_id=a.grant_role_ids[0])
        if transport == "api"
        else _roster(a, request)
    )
    with pytest.raises(boundary.HumanOperationError) as outer:
        with operation as context:
            a.db.get(Party, a.pid).display_name = "MUST ROLLBACK"
            if transport == "api":
                request.scope["headers"][:] = [(b"authorization", b"credential-without-bearer-separator")]
            else:
                request.cookies["session"] = "changed-cookie"
            with pytest.raises(boundary.HumanOperationError) as caught:
                context.require()
            _assert_owned_locals_cleared(
                caught.value, {"_request_digest": ["request", "operation", "raw", "headers", "scheme", "_"]}
            )
    _assert_owned_locals_cleared(
        outer.value,
        {
            "_human_operation": [
                "request",
                "operation",
                "resources",
                "raw",
                "digest",
                "claims",
                "candidate",
                "context",
                "payload",
                "scheme",
                "_",
            ]
        },
    )
    assert context.__dict__ == {}
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).display_name == "Sign In"


def test_invalid_resource_constructor_clears_partial_context_and_retained_own_frames(admin_human):
    a = admin_human
    request = _api_request(a)
    with pytest.raises(boundary.HumanOperationError) as caught:
        with boundary.grant_api_role(a.db, request=request, person_id=a.pid, role_id="invalid-uuid-command"):
            pytest.fail("invalid resource reached genuine owner handoff")
    _assert_owned_locals_cleared(
        caught.value,
        {
            "_resource_values": ["resources", "identifiers", "values", "value", "email"],
            "__init__": [
                "self",
                "db",
                "request",
                "operation",
                "resources",
                "tenant_id",
                "digest",
                "candidate",
                "claims",
            ],
            "_human_operation": [
                "request",
                "operation",
                "resources",
                "context",
                "digest",
                "claims",
                "candidate",
                "raw",
            ],
        },
    )
    a.db.commit()


def test_malformed_normalization_clears_only_own_factory_and_helper_frames(human):
    a = human
    request = _request(a.tenant, a.token)
    with pytest.raises(AttributeError) as caught:
        boundary.bulk_enroll_web_roster(a.db, request=request, cohort_id=uuid4(), emails=[object()])
    _assert_owned_locals_cleared(
        caught.value,
        {
            "bulk_enroll_web_roster": ["db", "request", "cohort_id", "emails", "track_id", "normalized"],
            "_normalized_emails": ["emails", "normalized", "raw", "value"],
        },
    )
    # Returned contextmanager bindings still function after factory locals clear.
    with _roster(a, request) as context:
        assert context.actor_id == a.pid


def test_operation_snapshot_failure_clears_own_command_parameter(admin_human):
    a = admin_human
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(
            a.db, request=_api_request(a), person_id=a.pid, role_id=a.grant_role_ids[0]
        ) as context:
            original = context.operation.roles
            try:
                object.__setattr__(context.operation, "roles", [None, "admin"])
                with pytest.raises(TypeError) as caught:
                    context.require()
                _assert_owned_locals_cleared(caught.value, {"_operation_values": ["operation"]})
            finally:
                object.__setattr__(context.operation, "roles", original)
    a.db.commit()


def test_audit_failure_clears_own_command_aliases_without_mutating_shared_expected_values(admin_human, monkeypatch):
    a = admin_human
    role_id = a.grant_role_ids[0]

    def refusal(*args, **kwargs):
        raise RuntimeError("hosted audit append refusal")

    monkeypatch.setattr(boundary, "write_audit_event", refusal)
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=role_id) as context:
            with pytest.raises(RuntimeError) as caught:
                context.grant_role()
            _assert_owned_locals_cleared(
                caught.value, {"audit": ["details", "op", "resource", "expected", "row", "email"]}
            )
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert (
        a.db.scalar(
            select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == role_id)
        )
        is None
    )


def test_wrong_tenant_session_scope_and_real_request_refuse_before_operation(human, tenant_b):
    a = human
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, tenant_b.id)
    with pytest.raises(boundary.HumanOperationError), _roster(a):
        pytest.fail("cross-tenant caller Session authorized operation")
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, tenant_b.id)
    wrong_request = _request(tenant_b, a.token)
    with pytest.raises(HTTPException) as caught, _roster(a, wrong_request):
        pytest.fail("cross-tenant exact token authorized operation")
    assert caught.value.status_code == 303
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tenant_b.id)
    a.db.commit()


@pytest.mark.parametrize("restriction", ["revoked", "expired", "suspended", "inactive", "role_loss"])
def test_current_database_authority_refuses_stale_identity_map(human, restriction):
    a = human
    a.db.get(AuthSession, a.session_id)
    a.db.get(Party, a.pid)
    a.db.get(AcademyAccountProfile, a.pid)
    a.db.rollback()
    if restriction == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif restriction == "expired":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == a.session_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    elif restriction == "suspended":
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    elif restriction == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == a.pid))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    with pytest.raises(HTTPException) as caught, _roster(a):
        pytest.fail("stale account became authority")
    assert caught.value.status_code == (403 if restriction == "role_loss" else 303)
    a.db.commit()  # Caught refusal must leave caller root usable.


def test_web_hmac_session_does_not_decode_jwt_but_api_does(human):
    a = human
    opaque = "hosted-opaque-session-" + uuid4().hex
    a.db.rollback()
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(token_hash=hash_token(opaque)))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    with _roster(a, _request(a.tenant, opaque)) as context:
        assert context.actor_id == a.pid
    with pytest.raises(HTTPException) as caught:
        with boundary.grant_api_role(a.db, request=_api_request(a, opaque), person_id=a.pid, role_id=a.role_id):
            pytest.fail("opaque cookie session authorized API JWT operation")
    assert caught.value.status_code == 401


@pytest.mark.parametrize("restriction", ["role_loss", "suspended", "expiry"])
def test_actual_party_lock_wait_rechecks_authority_and_samples_clock_after_wait(human, restriction, monkeypatch):
    a = human
    a.db.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    now = datetime.now(UTC)
    if restriction == "expiry":
        a.admin.execute(
            update(AuthSession).where(AuthSession.id == a.session_id).values(expires_at=now + timedelta(minutes=1))
        )
        a.admin.commit()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now + timedelta(minutes=2) if release.is_set() else now

        monkeypatch.setattr(boundary, "datetime", Clock)

    def leader():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            if restriction == "role_loss":
                db.execute(
                    delete(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == a.pid)
                )
            elif restriction == "suspended":
                db.execute(
                    update(AcademyAccountProfile)
                    .where(AcademyAccountProfile.tenant_id == a.tid, AcademyAccountProfile.party_id == a.pid)
                    .values(status="suspended")
                )
            db.commit()

    def follower():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                with boundary.bulk_enroll_web_roster(
                    db, request=_request(a.tenant, a.token), cohort_id=uuid4(), emails=[a.email]
                ):
                    pytest.fail("prewait authority survived conflicting writer")
            assert caught.value.status_code == (403 if restriction == "role_loss" else 303)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)


@pytest.mark.parametrize("tamper", ["copy", "request", "resource", "root", "tenant", "session"])
def test_private_binding_refusal_is_fatal_even_when_caught_inside_boundary(human, tamper):
    a = human
    original_email = a.email
    with pytest.raises(boundary.HumanOperationError):
        with _roster(a) as context:
            a.db.get(Party, a.pid).email = "unsaved-" + original_email
            if tamper == "copy":
                forged = copy(context)
                with pytest.raises(boundary.HumanOperationError):
                    forged.require()
                # A copied instance cannot poison the original; fail the real
                # operation separately to assert its savepoint rollback.
                raise boundary.HumanOperationError("copied instance refused")
            if tamper == "request":
                context.request = _request(a.tenant, a.token)
            elif tamper == "resource":
                context.resources = copy(context.resources)
            elif tamper == "root":
                context.root = object()
            elif tamper == "tenant":
                context.tenant_id = uuid4()
            else:
                context.db = a.admin
            with pytest.raises(boundary.HumanOperationError):
                context.require()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).email == original_email
    assert context.__dict__ == {}


def test_roster_cannot_invent_audit_action_or_accept_forged_actor_hints(human):
    a = human
    request = _request(a.tenant, a.token)
    request.state.actor_id = uuid4()
    request.state.actor_is_admin = True
    request.state.source_application = "forged.application"
    with pytest.raises(boundary.HumanOperationError):
        with _roster(a, request) as context:
            assert context.actor_id == a.pid and context.actor_is_admin is False
            with pytest.raises(boundary.HumanOperationError):
                context.audit(details={})
    a.db.commit()


@pytest.mark.parametrize(
    "failure",
    [
        HTTPException(status_code=403, detail="Forbidden"),
        HTTPException(status_code=404, detail="Person or role not found"),
        HTTPException(status_code=409, detail="Role already assigned"),
        BadRequestError("only an admin can assign a cohort facilitator"),
        NotFoundError("person not found for tenant"),
        ConflictError("existing account conflict"),
    ],
)
def test_existing_domain_guard_shape_survives_whole_operation_rollback_and_outer_commit(human, failure):
    a = human
    with pytest.raises(type(failure)) as caught:
        with boundary.assign_web_facilitator(
            a.db, request=_request(a.tenant, a.token), cohort_id=uuid4(), person_id=a.pid
        ):
            a.db.get(Party, a.pid).display_name = "MUST ROLLBACK"
            raise failure
    assert caught.value is failure
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).display_name == "Sign In"


@pytest.mark.parametrize("email_length", [160, 161, 254])
def test_fixed_api_audit_actual_actor_label_bound_and_caught_failure_rollback(human, email_length, monkeypatch):
    a = human
    a.db.rollback()
    admin_role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin")).first()
    if admin_role is None:
        admin_role = Role(tenant_id=a.tid, slug="admin", name="Admin")
        a.admin.add(admin_role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=admin_role.id))
    grant_roles = [Role(tenant_id=a.tid, slug="hosted-" + uuid4().hex, name="Hosted grant") for _ in range(2)]
    a.admin.add_all(grant_roles)
    a.admin.flush()
    first_role, second_role = (role.id for role in grant_roles)
    email = "a" * (email_length - 6) + "@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=email))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    with boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=first_role) as context:
        context.grant_role()
        event = context.audits[0][0]
        assert event.actor_type == "user" and event.actor_id == str(a.pid) and event.actor_party_id == a.pid
        assert event.source_application == "dotmac_academy_app"
        assert event.actor_label == (email if email_length <= 160 else None)
        assert event.action == "role.grant" and event.details == {"role_id": str(first_role)}
    before = a.db.scalar(select(text("count(*)")).select_from(AuditEvent))

    def failed_audit(*args, **kwargs):
        raise RuntimeError("hosted injected audit append failure")

    monkeypatch.setattr(boundary, "write_audit_event", failed_audit)
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_api_request(a), person_id=a.pid, role_id=second_role) as context:
            a.db.get(Party, a.pid).display_name = "MUST ROLLBACK"
            with pytest.raises(RuntimeError):
                context.grant_role()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, a.pid).display_name == "Sign In"
    assert (
        a.db.scalar(
            select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == second_role)
        )
        is None
    )
    assert a.db.scalar(select(text("count(*)")).select_from(AuditEvent)) == before
