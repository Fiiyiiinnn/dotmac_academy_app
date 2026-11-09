# Maintained port of legacy_probe/services/test_reminder_resend_v4_sql_authority.py (issue #2 wave 5).
"""Genuine hosted V4 SQL/authority matrix. AUTHORED NOT RUN.

Sensitivity: run the two named P1 cases against the immutable V3 source in a
separately authorized hosted job, then V4. Do not mutate the V3 reference or
interpret unexecuted sensitivity predictions as qualification.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_delivery_resend import ReminderResendError
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import DDL, Integer, bindparam, event, inspect, literal_column, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, PendingRollbackError, ProgrammingError
from sqlalchemy.sql.elements import ReleaseSavepointClause, RollbackToSavepointClause, SavepointClause

from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.reminder import ReminderLog
from app.services import notifications, settings_store
from tests.test_issue2_ical_owner import (
    admin_session,
    app_client,
    app_user_session,
    current_account,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert admin_session and app_client and app_user_session and sign_in_account and tenant_a and tenant_b
from tests.test_issue2_ical_owner import _allow, _restore_policy  # noqa: E402
from tests.test_issue2_reminder_resend_request_owner import _log  # noqa: E402
from tests.test_issue2_reminder_resend_v3_caller_transactions import _invoke, _observe  # noqa: E402

assert current_account and _restore_policy
REFERENCE_V3_MANIFEST = "/private/tmp/academy-admin-lab-reminders-v3-source-manifest-20261019.json"
REFERENCE_V3_SHA256 = "3128924a14234b07e5a2c23ed9081841e34080058d10df4aca4a145607f16733"


def _driver_spy(monkeypatch, connection):
    calls = []
    dialect = connection.dialect
    for name in ("do_execute", "do_execute_no_params", "do_executemany"):
        actual = getattr(dialect, name)

        def observed(cursor, statement, *args, _actual=actual):
            calls.append(str(statement))
            return _actual(cursor, statement, *args)

        monkeypatch.setattr(dialect, name, observed)
    return calls


def _refused_effects(a, lid):
    a.db.rollback()
    _observe(a, lid, "queued", 0)
    assert (
        a.admin.scalar(
            select(EmailOutbox.id).where(
                EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key.like(f"reminder_resend:{lid}:%")
            )
        )
        is None
    )


def test_p1_sensitivity_active_actual_enqueue_then_textualselect_commit(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    import app.services.reminder_resend as command

    actual = command.enqueue_email
    calls = _driver_spy(monkeypatch, a.db.connection())

    def injecting(db, **kwargs):
        result = actual(db, **kwargs)
        db.connection().execute(text("COMMIT").columns())
        return result

    monkeypatch.setattr(command, "enqueue_email", injecting)
    with pytest.raises(ReminderResendError):
        _invoke(a, lid)
    assert not any(statement.strip().upper() == "COMMIT" for statement in calls)
    _refused_effects(a, lid)


def test_p1_sensitivity_returned_ddl_commit(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    calls = _driver_spy(monkeypatch, a.db.connection())
    with pytest.raises(ReminderResendError):
        a.db.connection().execute(DDL("COMMIT"))
    assert not any(statement.strip().upper() == "COMMIT" for statement in calls)
    _refused_effects(a, lid)


def _construction(connection, family, sql):
    if family == "text":
        return text(sql)
    if family == "textual":
        return text(sql).columns()
    if family == "nested":
        return select(text(sql).columns(value=Integer).subquery())
    if family == "ddl":
        return DDL(sql)
    if family == "compiled":
        return text(sql).compile(dialect=connection.dialect)
    if family == "compiled_ddl":
        return DDL(sql).compile(dialect=connection.dialect)
    if family == "literal":
        return select(literal_column(f"1; {sql}"))
    if family == "prefix":
        return select(literal_column("1")).prefix_with(sql)
    if family == "suffix":
        return select(literal_column("1")).suffix_with(sql)
    raise AssertionError(family)


@pytest.mark.parametrize(
    "family", ["text", "textual", "nested", "ddl", "compiled", "compiled_ddl", "literal", "prefix", "suffix", "driver"]
)
@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT",
        "ROLLBACK",
        "END",
        "ABORT",
        "BEGIN",
        "START TRANSACTION",
        "SAVEPOINT fake",
        "RELEASE SAVEPOINT fake",
        "PREPARE TRANSACTION 'fake'",
        "COMMIT PREPARED 'fake'",
        "/*x*/COMMIT",
        "SELECT 1; COMMIT",
    ],
)
@pytest.mark.parametrize("phase", ["enqueue_email", "write_audit_event", "returned"])
def test_statement_families_refuse_before_actual_dispatch(current_account, monkeypatch, family, sql, phase):
    a = current_account
    _allow(a)
    lid = _log(a)
    connection = a.db.connection()
    calls = _driver_spy(monkeypatch, connection)

    def attempt():
        if family == "driver":
            connection.exec_driver_sql(sql)
        else:
            connection.execute(_construction(connection, family, sql))

    if phase == "returned":
        _invoke(a, lid)
        start = len(calls)
        with pytest.raises(ReminderResendError):
            attempt()
        assert len(calls) == start
    else:
        import app.services.reminder_resend as command

        actual = getattr(command, phase)

        def injecting(db, **kwargs):
            result = actual(db, **kwargs)
            start = len(calls)
            try:
                attempt()
            finally:
                assert len(calls) == start
            return result

        monkeypatch.setattr(command, phase, injecting)
        with pytest.raises(ReminderResendError):
            _invoke(a, lid)
    _refused_effects(a, lid)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT 1 INTO"v4_escape"',
        'SELECT 1 INTO"pg_temp"."v4_escape"',
        'SELECT 1 INTO "pg_temp"."v4_escape"',
        'SELECT 1 AS "value"INTO"pg_temp"."v4_escape"',
    ],
)
@pytest.mark.parametrize("family", ["text", "textual", "nested", "compiled", "driver"])
@pytest.mark.parametrize("phase", ["enqueue_email", "write_audit_event", "returned", "late_cursor"])
def test_select_into_quoted_adjacency_refuses_before_original_driver(current_account, monkeypatch, sql, family, phase):
    a = current_account
    _allow(a)
    lid = _log(a)
    connection = a.db.connection()
    calls = _driver_spy(monkeypatch, connection)

    def attempt():
        if family == "driver":
            connection.exec_driver_sql(sql)
        else:
            connection.execute(_construction(connection, family, sql))

    if phase in ("returned", "late_cursor"):
        _invoke(a, lid)  # Actual enqueue, log change and public audit already exist.

        def transform(conn, cursor, statement, parameters, context, many):
            return sql, parameters

        if phase == "late_cursor":
            event.listen(connection, "before_cursor_execute", transform, retval=True)
        start = len(calls)
        try:
            with pytest.raises(ReminderResendError):
                if phase == "late_cursor":
                    if family == "driver":
                        connection.exec_driver_sql("SELECT 1")
                    else:
                        connection.execute(_construction(connection, family, "SELECT 1"))
                else:
                    attempt()
            assert len(calls) == start
        finally:
            if phase == "late_cursor":
                event.remove(connection, "before_cursor_execute", transform)
    else:
        import app.services.reminder_resend as command

        actual = getattr(command, phase)
        entered = []

        def injecting(db, **kwargs):
            result = actual(db, **kwargs)
            entered.append(True)  # Genuine writer returned before the injection.
            start = len(calls)
            try:
                attempt()
            finally:
                assert len(calls) == start
            return result

        monkeypatch.setattr(command, phase, injecting)
        with pytest.raises(ReminderResendError):
            _invoke(a, lid)
        assert entered == [True]
    _refused_effects(a, lid)


def test_actual_settings_and_bound_values_are_real_positive_sql(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    # Genuine Settings get_all uses a nested TextualSelect in Core SELECT.
    assert settings_store.effective(a.db, require_store=True).reminders_admin_access_roles
    value = 'bound ; COMMIT -- /* INTO"pg_temp"."v4_escape" characters stay parameters */'
    assert a.db.scalar(select(bindparam("safe_value")), {"safe_value": value}) == value
    assert a.db.scalar(text("SELECT :safe_value"), {"safe_value": value}) == value
    assert a.db.connection().exec_driver_sql("SELECT 1").scalar_one() == 1
    assert a.db.scalar(text('SELECT :into AS "INTO"'), {"into": value}) == value
    notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title=value)
    a.db.commit()
    _observe(a, lid, "sent", 1)


@pytest.mark.parametrize("event_name", ["before_execute", "before_cursor_execute"])
@pytest.mark.parametrize("transformed", ["COMMIT", "SELECT 1; COMMIT"])
def test_last_cursor_sentinel_checks_later_transformations(current_account, event_name, transformed, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    connection = a.db.connection()
    calls = _driver_spy(monkeypatch, connection)
    if event_name == "before_execute":

        def transform(conn, clause, multi, params, options):
            return text(transformed), multi, params
    else:

        def transform(conn, cursor, statement, params, context, many):
            return transformed, params

    event.listen(connection, event_name, transform, retval=True)
    start = len(calls)
    try:
        with pytest.raises(ReminderResendError):
            connection.execute(select(literal_column("1")))
        assert len(calls) == start
    finally:
        event.remove(connection, event_name, transform)
    _refused_effects(a, lid)


@pytest.mark.parametrize("event_name", ["do_execute", "do_execute_no_params", "do_executemany"])
@pytest.mark.parametrize("registration", ["before", "later_instance", "later_class", "cursor_callback"])
def test_dialect_replacement_listener_bodies_never_run_for_guarded_dispatch(current_account, event_name, registration):
    a = current_account
    _allow(a)
    lid = _log(a)
    connection = a.db.connection()
    dialect = connection.dialect
    calls = []

    def replacing(*args):
        calls.append("body")
        return True

    target = type(dialect) if registration == "later_class" else dialect
    registered = []

    def install():
        event.listen(target, event_name, replacing, insert=True)
        registered.append((target, event_name, replacing))

    if registration == "before":
        install()
    try:
        if registration == "before":
            with pytest.raises(ReminderResendError):
                _invoke(a, lid)
        else:
            _invoke(a, lid)
            if registration == "cursor_callback":

                def callback(*args):
                    if not registered:
                        install()

                event.listen(connection, "before_cursor_execute", callback)
                registered.append((connection, "before_cursor_execute", callback))

                # Separate marker: callback list already contains its own hook.
                def late_install(*args):
                    if len(registered) == 1:
                        install()

                event.remove(connection, "before_cursor_execute", callback)
                registered.pop()
                event.listen(connection, "before_cursor_execute", late_install)
                registered.append((connection, "before_cursor_execute", late_install))
            else:
                install()
            with pytest.raises(ReminderResendError):
                connection.execute(select(literal_column("1")))
        assert calls == []
    finally:
        for listener_target, name, listener in reversed(registered):
            event.remove(listener_target, name, listener)
    _refused_effects(a, lid)


@pytest.mark.parametrize("family", ["save", "release", "rollback"])
def test_matching_name_handbuilt_controls_have_no_native_permit(current_account, family):
    a = current_account
    _allow(a)
    lid = _log(a)
    outer = a.db.begin_nested()
    _invoke(a, lid)
    physical = a.db.connection().get_nested_transaction()
    cls = {"save": SavepointClause, "release": ReleaseSavepointClause, "rollback": RollbackToSavepointClause}[family]
    with pytest.raises(ReminderResendError):
        a.db.connection().execute(cls(physical._savepoint))
    outer.rollback()
    a.db.rollback()
    _observe(a, lid, "queued", 0)


@pytest.mark.parametrize(
    "method",
    [
        "begin_twophase",
        "commit_prepared",
        "rollback_prepared",
        "recover_twophase",
        "detach",
        "prepare",
        "mode",
        "schema",
    ],
)
def test_unsupported_completion_family_never_reaches_original(current_account, monkeypatch, method):
    a = current_account
    _allow(a)
    lid = _log(a)
    connection = a.db.connection()
    calls = []
    target = a.db if method == "prepare" else connection
    name = "execution_options" if method in ("mode", "schema") else method
    actual = getattr(target, name)

    def original(*args, **kwargs):
        calls.append("original")
        return actual(*args, **kwargs)

    monkeypatch.setattr(target, name, original)
    _invoke(a, lid)  # The unit captures this actual original wrapper.
    with pytest.raises(ReminderResendError):
        if method == "mode":
            target.execution_options(isolation_level="AUTOCOMMIT")
        elif method == "schema":
            target.execution_options(schema_translate_map={None: "other"})
        elif method in ("commit_prepared", "rollback_prepared"):
            getattr(target, name)("fake", recover=True)
        else:
            getattr(target, name)()
    assert calls == []
    _refused_effects(a, lid)


def _pending(a, mutation, allowed=None):
    """Pending change; ``allowed`` is the ``(grant_id, slug)`` that ``_allow`` returned."""
    if mutation == "session":
        row = a.db.get(AuthSession, a.session_id)
        row.revoked_at = datetime.now(UTC)
    elif mutation == "party":
        row = a.db.get(Party, a.pid)
        row.is_active = False
    elif mutation == "profile":
        row = a.db.get(AcademyAccountProfile, a.pid)
        row.status = "suspended"
    elif mutation == "person":
        row = a.db.get(PartyPerson, a.pid)
        row.first_name = "Pending benign name"
    elif mutation == "policy":
        from dotmac_kernel.settings_models import DomainSetting

        row = a.db.scalars(
            select(DomainSetting).where(
                DomainSetting.domain == "academy",
                DomainSetting.key == "reminders_admin_access_roles",
            )
        ).one()
        row.value_json = []
        row.value_text = None
    elif mutation == "grant":
        row = a.db.get(PartyRoleGrant, allowed[0])
        a.db.delete(row)
    elif mutation == "role":
        grant = a.db.get(PartyRoleGrant, allowed[0])
        row = a.db.get(Role, grant.role_id)
        row.slug = "unselected-role"
    elif mutation == "pk":
        row = a.db.get(AuthSession, a.session_id)
        from uuid import uuid4

        row.id = uuid4()
    else:
        from uuid import uuid4

        row = Role(tenant_id=a.tid, slug=uuid4().hex, name="pending new authority")
        a.db.add(row)
    return row


@pytest.mark.parametrize("mutation", ["session", "party", "profile", "person", "policy", "grant", "role", "pk", "new"])
def test_initial_preflight_preserves_native_pending_state_and_history(current_account, mutation):
    a = current_account
    allowed = _allow(a)
    lid = _log(a)
    row = _pending(a, mutation, allowed)
    before = dict(inspect(row).dict)
    before_history = tuple((attr.key, attr.history) for attr in inspect(row).attrs if attr.history.has_changes())
    was_new, was_deleted = row in a.db.new, row in a.db.deleted
    with a.db.no_autoflush:
        with pytest.raises(ReminderResendError):
            _invoke(a, lid)
    assert dict(inspect(row).dict) == before
    assert (
        tuple((attr.key, attr.history) for attr in inspect(row).attrs if attr.history.has_changes()) == before_history
    )
    assert (row in a.db.new, row in a.db.deleted) == (was_new, was_deleted)
    assert row in a.db.new or row in a.db.dirty or row in a.db.deleted
    a.db.rollback()
    _observe(a, lid, "queued", 0)


def _observe_native_authority_denial(a, changed, monkeypatch):
    """Record only immutable booleans before native rollback; call real authority.

    No row, credential, request, SQL parameter or exception object is retained.
    The fresh denial is observed inside the genuine after-flush authority call.
    """
    from dotmac_delivery_resend import pending_authority
    from fastapi import HTTPException

    from app.services import reminder_resend as command

    actual = command._authority
    evidence = []

    def observing(db, *args, **kwargs):
        before = (bool(db._flushing), not pending_authority(db), bool(changed()))
        try:
            return actual(db, *args, **kwargs)
        except (HTTPException, ReminderResendError) as exc:
            evidence.append((*before, isinstance(exc, HTTPException)))
            raise  # Preserve the original exception and native flush rollback.

    monkeypatch.setattr(command, "_authority", observing)
    return evidence


def _assert_native_flush_rollback(a, row, evidence, lid):
    assert evidence == [(True, True, True, True)]
    assert not a.db.is_active
    assert a.db.in_transaction() and not a.db.get_transaction().is_active
    # Inspection is non-loading: native rollback expires fields/restores deletes.
    assert inspect(row).persistent and inspect(row).expired
    assert not inspect(row).deleted
    _refused_effects(a, lid)  # Caller explicitly rolls back; separate admin readback.
    assert a.db.is_active and not a.db.in_transaction()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT 1")) == 1  # Same caller Session recovers normally.
    a.db.rollback()


# "policy" is superseded here: platform-scope domain_settings writes are
# platform_api-only under kernel RLS, so an app_user caller's pending
# policy mutation can never flush into effect (stronger than the legacy
# premise). The pending-detection invariant stays proven by the
# [policy] preflight case above.
@pytest.mark.parametrize("mutation", ["session", "party", "profile", "grant", "role"])
def test_native_logical_flush_preserves_revocation_then_refuses_fresh_authority(current_account, mutation, monkeypatch):
    from fastapi import HTTPException

    a = current_account
    allowed = _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    if mutation in ("grant", "role"):
        # The account also holds the standard student grant. It must grant nothing
        # under the policy, so the pending change below is the only authority.
        remaining = a.admin.scalars(
            select(Role.slug)
            .join(PartyRoleGrant, PartyRoleGrant.role_id == Role.id)
            .where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == a.pid, PartyRoleGrant.id != allowed[0])
        ).all()
        a.admin.rollback()
        assert remaining and allowed[1] not in remaining
    row = _pending(a, mutation, allowed)

    def changed():
        state = inspect(row)
        if mutation == "session":
            return state.dict.get("revoked_at") is not None
        if mutation == "party":
            return state.dict.get("is_active") is False
        if mutation == "profile":
            return state.dict.get("status") == "suspended"
        if mutation == "policy":
            return state.dict.get("value") == "[]"
        if mutation == "grant":
            return state.deleted
        return state.dict.get("slug") == "unselected-role"

    evidence = _observe_native_authority_denial(a, changed, monkeypatch)
    if mutation == "policy":
        # Canonical app_user cannot write platform_settings. No post-flush
        # authority observation can honestly attest a successful forbidden DML.
        connection = a.db.connection()
        target = connection.get_transaction()
        with _native_abort_observer(a, connection, target, monkeypatch) as native:
            with pytest.raises(ProgrammingError) as caught:
                a.db.commit()
            _assert_selected_abort(a, native, caught.value, target, "42501")
            assert evidence == []
            assert inspect(row).persistent and inspect(row).expired
            _independent_absent(a, lid)
        _native_caller_reuse(a)
    else:
        with pytest.raises((HTTPException, ReminderResendError)):
            a.db.commit()
        _assert_native_flush_rollback(a, row, evidence, lid)
        if mutation == "role":
            # Nothing renamed may stay committed on the shared tenant roles.
            a.admin.rollback()
            assert (
                a.admin.scalar(select(Role.id).where(Role.tenant_id == a.tid, Role.slug == "unselected-role")) is None
            )
            assert a.admin.scalar(select(Role.id).where(Role.tenant_id == a.tid, Role.slug == allowed[1]))
            a.admin.rollback()


@pytest.mark.parametrize("entry", ["session", "root", "context"])
@pytest.mark.parametrize("field", ["name", "prefs", "avatar"])
def test_benign_pending_authority_changes_survive_native_logical_commit(current_account, entry, field):
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.commit()
    context = a.db.begin()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    _invoke(a, lid)
    if field == "name":
        row = a.db.get(PartyPerson, a.pid)
        row.first_name = "Retained benign first name"
        expected = row.first_name
    else:
        row = a.db.get(AcademyAccountProfile, a.pid)
        key = "prefs" if field == "prefs" else "avatar_path"
        expected = {"sample": "bound ; --"} if field == "prefs" else "https://example.test/avatar.png"
        setattr(row, key, expected)
    if entry == "session":
        a.db.commit()
    elif entry == "root":
        context.commit()
    else:
        context.__exit__(None, None, None)
    _observe(a, lid, "sent", 1)
    model = PartyPerson if field == "name" else AcademyAccountProfile
    stored = a.admin.get(model, a.pid)
    assert getattr(stored, "first_name" if field == "name" else key) == expected


def test_direct_physical_commit_refuses_pending_authority_without_erasure(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    row = _pending(a, "session")
    expected = row.revoked_at
    with pytest.raises(ReminderResendError):
        a.db.connection().commit()
    assert row.revoked_at == expected and row in a.db.dirty
    _refused_effects(a, lid)


@pytest.mark.parametrize("event_name", ["before_commit", "after_flush_postexec", "commit"])
@pytest.mark.parametrize("core", [False, True])
def test_callback_authority_mutations_cannot_borrow_stale_proof(current_account, event_name, core, monkeypatch):
    from fastapi import HTTPException

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    connection = a.db.connection()
    row = a.db.get(AuthSession, a.session_id)
    target = connection if event_name == "commit" else a.db
    evidence = _observe_native_authority_denial(a, lambda: inspect(row).dict.get("revoked_at") is not None, monkeypatch)

    def mutate(*args):
        if core:
            try:
                connection.execute(
                    update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC))
                )
            except ReminderResendError:
                return  # Caught late violation remains latched at final dispatch.
        else:
            row.revoked_at = datetime.now(UTC)

    if event_name == "after_flush_postexec":
        caller = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="before callback")
        caller.title = "actual native commit flush"
    event.listen(target, event_name, mutate)
    try:
        with pytest.raises((ReminderResendError, HTTPException)):
            a.db.commit()
        if not core and event_name != "commit":
            _assert_native_flush_rollback(a, row, evidence, lid)
        else:
            assert core or row.revoked_at is not None
    finally:
        event.remove(target, event_name, mutate)
    _refused_effects(a, lid)


def test_constructor_partial_registration_unwinds_only_own_patches(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    from dotmac_delivery_resend import fence as boundary

    original = boundary.ReminderUnit.patch
    commit = a.db.commit
    calls = []

    def partial(unit, obj, name):
        original(unit, obj, name)
        calls.append(name)
        if len(calls) == 3:
            raise ReminderResendError("injected constructor failure")

    monkeypatch.setattr(boundary.ReminderUnit, "patch", partial)
    with pytest.raises(ReminderResendError):
        _invoke(a, lid)
    assert a.db.commit == commit
    assert boundary._unit(a.db) is None
    a.db.commit()
    _observe(a, lid, "queued", 0)


@pytest.mark.parametrize("mode", ["autocommit", "twophase"])
def test_unsupported_caller_configuration_refuses_actual_command_before_enqueue(current_account, monkeypatch, mode):
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.rollback()
    import app.services.reminder_resend as command

    actual = command.enqueue_email
    calls = []

    def observed(db, **kwargs):
        calls.append("enqueue")
        return actual(db, **kwargs)

    monkeypatch.setattr(command, "enqueue_email", observed)
    original_bind = a.db.bind
    if mode == "twophase":
        a.db.twophase = True
        try:
            with pytest.raises(ReminderResendError):
                _invoke(a, lid)
        finally:
            a.db.twophase = False
    else:
        with original_bind.connect() as connection:
            connection.execution_options(isolation_level="AUTOCOMMIT")
            a.db.bind = connection
            try:
                with pytest.raises(ReminderResendError):
                    _invoke(a, lid)
            finally:
                a.db.rollback()
                a.db.bind = original_bind
    assert calls == []
    _refused_effects(a, lid)


@pytest.mark.parametrize("kind", ["select", "insert"])
def test_independent_dml_cte_cannot_hide_in_otherwise_admitted_statement(current_account, kind):
    from sqlalchemy import insert

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    hidden = update(ReminderLog).where(ReminderLog.id == lid).values(title="hidden CTE mutation").cte("hidden")
    statement = select(literal_column("1")) if kind == "select" else insert(ReminderLog)
    statement = statement.add_cte(hidden)
    with pytest.raises(ReminderResendError):
        a.db.execute(statement)
    _refused_effects(a, lid)


@pytest.mark.parametrize("style", ["no_parameters", "executemany", "insertmanyvalues"])
def test_final_sentinel_covers_real_dispatch_styles_and_transforms(current_account, monkeypatch, style):
    from uuid import uuid4

    from sqlalchemy import insert

    from app.models.notification import Notification

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    connection = a.db.connection()
    calls = _driver_spy(monkeypatch, connection)

    def transform(conn, cursor, statement, parameters, context, many):
        return "COMMIT", parameters

    event.listen(connection, "before_cursor_execute", transform, retval=True)
    start = len(calls)
    try:
        with pytest.raises(ReminderResendError):
            if style == "no_parameters":
                connection.execution_options(no_parameters=True).execute(text("SELECT 1"))
            else:
                statement = insert(Notification)
                if style == "insertmanyvalues":
                    statement = statement.returning(Notification.id)
                connection.execute(
                    statement,
                    [
                        dict(id=uuid4(), tenant_id=a.tid, person_id=a.pid, kind="caller", title=f"batch-{index}")
                        for index in range(3)
                    ],
                )
        assert len(calls) == start
    finally:
        event.remove(connection, "before_cursor_execute", transform)
    _refused_effects(a, lid)


@pytest.mark.parametrize("insert_first", [False, True])
def test_after_flush_callback_order_preserves_native_pending_barrier(current_account, insert_first):
    from fastapi import HTTPException
    from sqlalchemy.orm import Session

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    auth = a.db.get(AuthSession, a.session_id)
    caller = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="before order test")
    caller.title = "native flush trigger"
    changed = []

    def mutate(db, context):
        if db is a.db and not changed:
            auth.revoked_at = datetime.now(UTC)
            changed.append(True)

    event.listen(Session, "after_flush_postexec", mutate, insert=insert_first)
    try:
        with pytest.raises((ReminderResendError, HTTPException)):
            a.db.commit()
        assert changed == [True]
    finally:
        event.remove(Session, "after_flush_postexec", mutate)
    _refused_effects(a, lid)


def test_native_control_reentrant_callback_cannot_borrow_clause_or_name(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    connection = a.db.connection()
    attempted = []

    def reenter(conn, clause, multi, params, options):
        if getattr(clause, "__visit_name__", "") == "savepoint" and not attempted:
            attempted.append(True)
            with pytest.raises(ReminderResendError):
                conn.execute(clause)  # Same object/name, wrong execution depth.

    event.listen(connection, "before_execute", reenter)
    try:
        with pytest.raises(ReminderResendError):
            with a.db.begin_nested():
                a.db.execute(select(literal_column("1")))
    finally:
        event.remove(connection, "before_execute", reenter)
    assert attempted == [True]
    _refused_effects(a, lid)


def test_unregistered_connection_keeps_native_dialect_listener_behavior(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    dialect_class = type(a.db.connection().dialect)
    bodies = []

    def unrelated(cursor, statement, parameters, context):
        bodies.append(context.root_connection)
        return None

    event.listen(dialect_class, "do_execute", unrelated)
    try:
        assert a.admin.execute(select(literal_column("1"))).scalar_one() == 1
        assert bodies and all(connection is not a.db.connection() for connection in bodies)
    finally:
        event.remove(dialect_class, "do_execute", unrelated)
    a.db.commit()
    _observe(a, lid, "sent", 1)


def test_native_control_permit_cannot_be_reused_after_original_dispatch(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    connection = a.db.connection()
    clauses = []

    def observe(conn, clause, multi, params, options):
        if getattr(clause, "__visit_name__", "") == "savepoint":
            clauses.append(clause)

    event.listen(connection, "before_execute", observe)
    try:
        with a.db.begin_nested():
            a.db.execute(select(literal_column("1")))
    finally:
        event.remove(connection, "before_execute", observe)
    assert len(clauses) == 1
    with pytest.raises(ReminderResendError):
        connection.execute(clauses[0])
    _refused_effects(a, lid)


# Native failure matrix: hosted PostgreSQL only, AUTHORED NOT RUN. All observers
# call actual installed methods; exception identities exist only in test scope.
def _actual_effects(a, lid):
    log = a.db.get(ReminderLog, lid)
    assert log.status == "sent" and log.sent_at is not None
    outbox = a.db.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == log.outbox_key)
    ).one()
    audit = a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == a.tid, AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend"
        )
    ).one()
    assert outbox.recipient == a.email and outbox.payload == {}
    assert audit.actor_party_id == a.pid and audit.actor_type == "user"
    assert audit.actor_id == str(a.pid) and audit.source_application == "dotmac_academy_app"
    assert audit.details == {"event_kind": "graded"}
    return log, outbox, audit


def _independent_absent(a, lid):
    # Fresh independent transaction BEFORE caller explicit cleanup.
    _observe(a, lid, "queued", 0)
    assert (
        a.admin.scalar(
            select(EmailOutbox.id).where(
                EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key.like(f"reminder_resend:{lid}:%")
            )
        )
        is None
    )
    a.admin.rollback()


def _duplicate_occurrence(a, committed_id):
    a.admin.rollback()
    original = a.admin.get(ReminderLog, committed_id)
    data = {c.key: getattr(original, c.key) for c in ReminderLog.__table__.columns}
    data["id"] = uuid4()
    duplicate = ReminderLog(**data)
    assert duplicate.id != original.id
    assert any(c.name == "uq_reminder_log_occurrence" for c in ReminderLog.__table__.constraints)
    assert a.admin.scalar(text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_reminder_log_occurrence'")) == 1
    a.admin.rollback()
    a.db.add(duplicate)
    return duplicate


@contextmanager
def _native_abort_observer(a, connection, target, monkeypatch, *, skip=False, replacement=None, callback=None):
    evidence = {"selected": [], "original": [], "driver": [], "soft": [], "callback": []}
    dialect = connection.dialect
    root = connection.get_transaction()
    method = "do_rollback" if target is root else "do_rollback_to_savepoint"
    actual = getattr(dialect, method)

    def driver(value, *args):
        if method == "do_rollback":
            matches = getattr(value, "dbapi_connection", value) is connection.connection.dbapi_connection
        else:
            matches = value is connection and args == (target._savepoint,)
        result = actual(value, *args)
        if matches:
            evidence["driver"].append(target)
        return result

    def error(context):
        if context.connection is not connection:
            return None
        if callback is not None:
            callback(evidence)
        selected = replacement if replacement is not None else context.sqlalchemy_exception
        evidence["selected"].append(selected)
        evidence["original"].append(context.original_exception)
        return replacement

    def soft(db, transaction):
        if transaction.parent is not None and transaction.origin.name == "SUBTRANSACTION":
            evidence["soft"].append(transaction.parent._rollback_exception)

    # Restore the live guard before its owning transaction retires it.
    # A local patch context cannot resurrect that guard at pytest teardown.
    with monkeypatch.context() as observer_patch:
        observer_patch.setattr(dialect, method, driver)
        event.listen(connection.engine, "handle_error", error, retval=True)
        event.listen(a.db, "after_soft_rollback", soft)
        if skip:
            connection.execution_options(skip_user_error_events=True)
        try:
            yield evidence
        finally:
            if skip and not connection.closed:
                connection.execution_options(skip_user_error_events=False)
            event.remove(connection.engine, "handle_error", error)
            event.remove(a.db, "after_soft_rollback", soft)
            for values in evidence.values():
                values.clear()  # No retained driver exceptions/tracebacks/parameters.


def _assert_selected_abort(a, evidence, caught, target, sqlstate, *, skip=False):
    assert evidence["driver"] == [target]
    assert not target.is_active
    assert evidence["soft"] == [caught]
    assert evidence["selected"] == [] if skip else evidence["selected"] == [caught]
    assert isinstance(caught, DBAPIError)
    if not skip:
        assert caught.orig is evidence["original"][0]
    assert caught.orig.sqlstate == sqlstate
    if sqlstate == "23505":
        assert isinstance(caught, IntegrityError)
        assert caught.orig.diag.constraint_name == "uq_reminder_log_occurrence"
    else:
        from psycopg.errors import InsufficientPrivilege

        assert isinstance(caught, ProgrammingError) and isinstance(caught.orig, InsufficientPrivilege)
    assert not a.db.is_active


def _native_caller_reuse(a):
    a.db.rollback()
    assert a.db.is_active and not a.db.in_transaction()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT 1")) == 1
    a.db.rollback()


@pytest.mark.parametrize("entry", ["session", "root", "flush", "no_autoflush", "context"])
@pytest.mark.parametrize("failure", ["constraint", "permission"])
@pytest.mark.parametrize("skip", [False, True])
def test_genuine_native_root_flush_abort_preserves_selected_exception(
    current_account, monkeypatch, request, entry, failure, skip
):
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    a.db.commit()
    context = a.db.begin()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    _invoke(a, lid)
    _actual_effects(a, lid)
    row = _duplicate_occurrence(a, conflict) if failure == "constraint" else _pending(a, "policy")
    if failure == "permission":
        # Kernel RLS would silently filter the app_user UPDATE to zero
        # rows; revoke the table privilege so the flush aborts with the
        # genuine 42501 this case selects (genesis DB is disposable).
        a.admin.execute(text("REVOKE UPDATE ON public.domain_settings FROM app_user"))
        a.admin.commit()
    connection = a.db.connection()
    target = connection.get_transaction()
    # The context exit releases the connection, and the pool's reset-on-return then calls the same
    # dialect.do_rollback the observer wraps. Count those resets for this DBAPI connection so the
    # one native rollback of the root is still proven exactly once.
    resets = []
    dbapi_connection = connection.connection.dbapi_connection

    seen = {}

    def on_reset(dbapi_conn, _record, reset_state):
        if dbapi_conn is dbapi_connection:
            # The root was already rolled back natively, so Connection.close() had no transaction to
            # close inline and checks in with transaction_was_reset=False: the pool performs its own
            # reset-on-return rollback right after this event. (Had Connection.close() rolled a
            # transaction back inline it would check in with transaction_reset=True, giving
            # transaction_was_reset=True.) Record the native-driver count at this moment.
            resets.append(
                (
                    reset_state.transaction_was_reset,
                    reset_state.terminate_only,
                    reset_state.asyncio_safe,
                    len(seen["native"]["driver"]),
                )
            )

    pool = connection.engine.pool
    event.listen(pool, "reset", on_reset)
    request.addfinalizer(lambda: event.remove(pool, "reset", on_reset))
    with _native_abort_observer(a, connection, target, monkeypatch, skip=skip) as native:
        seen["native"] = native
        with pytest.raises(DBAPIError) as caught:
            if entry == "session":
                a.db.commit()
            elif entry == "root":
                context.commit()
            elif entry == "context":
                context.__exit__(None, None, None)
            elif entry == "no_autoflush":
                with a.db.no_autoflush:
                    a.db.flush()
            else:
                a.db.flush()
        # A context has already done its native logical cleanup.
        if entry != "context":
            _assert_selected_abort(
                a, native, caught.value, target, "23505" if failure == "constraint" else "42501", skip=skip
            )
            assert a.db.in_transaction() and not a.db.get_transaction().is_active
        else:
            # Exactly one native rollback of the root had happened when the connection was checked in
            # (transaction_was_reset=False: Connection.close() reset nothing inline, so the pool performs
            # its own rollback right after the reset event); the second driver call is
            # the pool's own reset-on-return rollback afterwards. A double native rollback would show
            # driver=2 already at the reset event.
            assert resets == [(False, False, True, 1)]
            assert len(native["driver"]) == 2
            assert not target.is_active
            assert native["soft"][0] is caught.value
            assert caught.value.orig.sqlstate == ("23505" if failure == "constraint" else "42501")
            if not skip:
                assert native["selected"] == [caught.value]
                assert caught.value.orig is native["original"][0]
            assert not a.db.in_transaction()
        assert inspect(row).transient if failure == "constraint" else inspect(row).expired
        _independent_absent(a, lid)
    _native_caller_reuse(a)


def test_returned_command_privileged_policy_revocation_uses_fresh_authority(current_account, monkeypatch):
    from fastapi import HTTPException

    from app.services import reminder_resend as command

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    # admin_session uses MIGRATION_DATABASE_URL: the non-superuser app_admin (the
    # pin requires it not to be a superuser). It can still write platform_settings,
    # which is all the revocation below needs.
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    a.admin.rollback()
    assert a.admin.scalar(text("SELECT current_user")) == "app_admin"
    assert a.admin.scalar(text("SELECT has_table_privilege(current_user, 'public.domain_settings', 'UPDATE')"))
    a.admin.rollback()
    settings_store.set_many(a.admin, {"reminders_admin_access_roles": "[]"})
    a.admin.commit()
    a.admin.expire_all()
    assert settings_store.get_all(a.admin)["reminders_admin_access_roles"] == []
    a.admin.rollback()
    actual = command._authority
    denied = []

    def observed(*args, **kwargs):
        try:
            return actual(*args, **kwargs)
        except HTTPException as exc:
            denied.append(exc)
            raise

    monkeypatch.setattr(command, "_authority", observed)
    try:
        with pytest.raises(HTTPException) as caught:
            a.db.commit()
        assert caught.value.status_code == 403 and denied == [caught.value]
        _independent_absent(a, lid)
        a.db.rollback()
        _independent_absent(a, lid)
    finally:
        denied.clear()


@pytest.mark.parametrize("entry", ["flush", "savepoint", "context"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("earlier_receipt", [False, True])
def test_genuine_native_containing_savepoint_abort_preserves_outer_receipt(
    current_account, monkeypatch, entry, depth, earlier_receipt
):
    a = current_account
    _allow(a)
    lid, earlier, conflict = _log(a), _log(a), _log(a)
    if earlier_receipt:
        _invoke(a, earlier)
        _actual_effects(a, earlier)
    outer = a.db.begin_nested()
    a.db.connection()
    inner = a.db.begin_nested() if depth == 2 else outer
    a.db.connection()
    _invoke(a, lid)
    _actual_effects(a, lid)
    bad = _duplicate_occurrence(a, conflict)
    connection = a.db.connection()
    target = connection.get_nested_transaction()
    root = connection.get_transaction()
    with _native_abort_observer(a, connection, target, monkeypatch) as native:
        with pytest.raises(IntegrityError) as caught:
            if entry == "flush":
                a.db.flush()
            elif entry == "savepoint":
                inner.commit()
            else:
                inner.__exit__(None, None, None)
        assert native["driver"] == [target] and not target.is_active
        assert native["soft"][0] is caught.value
        assert caught.value.orig.sqlstate == "23505"
        assert root.is_active and inspect(bad).transient
        _independent_absent(a, lid)
        if entry != "context":
            assert not inner.is_active
            from dotmac_delivery_resend.fence import _unit

            unit = _unit(a.db)
            assert target in unit.waiting_aborts and target in unit.rollback_proofs
            inner.rollback()  # Actual containing logical cleanup retires it.
    # Finish the observation while the original unit still owns its guard.
    dialect = connection.dialect
    if depth == 2:
        assert outer.is_active
        outer.commit()
    a.db.commit()
    _independent_absent(a, lid)
    if earlier_receipt:
        _observe(a, earlier, "sent", 1)
    # A new real resend must remain admissible on the same shared dialect
    # after the original unit and the observation have both been retired.
    fresh = _log(a)
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.connection().dialect is dialect
    _invoke(a, fresh)
    _actual_effects(a, fresh)
    a.db.commit()
    _observe(a, fresh, "sent", 1)
    assert (
        a.admin.scalar(
            select(EmailOutbox.id).where(
                EmailOutbox.tenant_id == a.tid,
                EmailOutbox.idempotency_key.like(f"reminder_resend:{fresh}:%"),
            )
        )
        is not None
    )


@pytest.mark.parametrize("replacement_mode", ["return", "raise"])
def test_native_abort_preserves_preexisting_error_handler_selection(current_account, monkeypatch, replacement_mode):
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    _duplicate_occurrence(a, conflict)
    connection = a.db.connection()
    target = connection.get_transaction()
    chosen = RuntimeError("test-selected replacement")

    def replace(context):
        if context.connection is connection:
            if replacement_mode == "raise":
                raise chosen
            return chosen
        return None

    event.listen(connection.engine, "handle_error", replace, retval=True)
    try:
        with _native_abort_observer(a, connection, target, monkeypatch, replacement=chosen) as native:
            with pytest.raises(RuntimeError) as caught:
                a.db.flush()
            assert caught.value is chosen
            assert native["driver"] == [target] and not target.is_active
            assert native["soft"] == [chosen]
            _independent_absent(a, lid)
    finally:
        event.remove(connection.engine, "handle_error", replace)
    _native_caller_reuse(a)


@pytest.mark.parametrize("callback", ["before_flush", "mapper", "handle_error", "rollback"])
@pytest.mark.parametrize("operation", ["commit", "rollback"])
def test_native_abort_callback_cannot_borrow_completion(current_account, monkeypatch, callback, operation):
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    _duplicate_occurrence(a, conflict)
    connection = a.db.connection()
    target = connection.get_transaction()
    attempts = []

    def attempt(*args):
        with pytest.raises(ReminderResendError):
            getattr(target, operation)()
        attempts.append(True)

    owner = connection if callback == "rollback" else ReminderLog if callback == "mapper" else a.db
    event_name = "before_insert" if callback == "mapper" else callback
    if callback != "handle_error":
        event.listen(owner, event_name, attempt)
    try:
        with _native_abort_observer(
            a, connection, target, monkeypatch, callback=attempt if callback == "handle_error" else None
        ) as native:
            expected = ReminderResendError if callback in ("before_flush", "mapper") else IntegrityError
            with pytest.raises(expected) as caught:
                a.db.flush()
            if callback in ("before_flush", "mapper"):
                # A caught early denial poisons admission BEFORE DBAPI SQL.
                # Do not fabricate a later database error in that case.
                assert native["selected"] == []
                assert native["driver"] == [target] and not target.is_active
                assert native["soft"] == [caught.value]
            else:
                _assert_selected_abort(a, native, caught.value, target, "23505")
            assert attempts == [True]
            _independent_absent(a, lid)
    finally:
        if callback != "handle_error":
            event.remove(owner, event_name, attempt)
    _native_caller_reuse(a)


def test_genuine_active_helper_flush_error_recovers_earlier_receipt(current_account, monkeypatch):
    a = current_account
    _allow(a)
    earlier, lid = _log(a), _log(a)
    _invoke(a, earlier)
    _actual_effects(a, earlier)
    from dotmac_delivery_resend.fence import _unit

    import app.services.reminder_resend as command

    actual = command.write_audit_event
    observed = []

    def fail_after_actual(db, **kwargs):
        result = actual(db, **kwargs)
        _log, _outbox, audit = _actual_effects(a, lid)
        data = {c.key: getattr(audit, c.key) for c in AuditEvent.__table__.columns}
        # A separate mapped insertion with the genuine writer's full values.
        db.add(AuditEvent(**data))
        connection = db.connection()
        target = connection.get_nested_transaction()
        with _native_abort_observer(a, connection, target, monkeypatch) as native:
            try:
                db.flush()
            except IntegrityError as exc:
                assert exc.orig.sqlstate == "23505"
                assert native["driver"] == [target] and not target.is_active
                assert native["soft"] == [exc]
                observed.append(True)
                raise
        return result

    monkeypatch.setattr(command, "write_audit_event", fail_after_actual)
    with pytest.raises(IntegrityError):
        _invoke(a, lid)
    assert observed == [True]
    unit = _unit(a.db)
    assert unit is not None and not unit.failed and not unit.corrupt
    assert unit.helper is None and not unit.internal_aborts and unit.native_abort is None
    a.db.commit()
    _observe(a, earlier, "sent", 1)
    _independent_absent(a, lid)


def test_native_rollback_dispatch_failure_never_invents_recovery(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    _duplicate_occurrence(a, conflict)
    from dotmac_delivery_resend.fence import _unit

    unit = _unit(a.db)
    connection = a.db.connection()
    target = connection.get_transaction()
    actual = connection.dialect.do_rollback
    chosen = RuntimeError("injected rollback dispatch fault")

    def failed(value):
        raise chosen  # Sensitivity injection, not a genuine PostgreSQL error.

    monkeypatch.setattr(connection.dialect, "do_rollback", failed)
    try:
        with pytest.raises(RuntimeError) as caught:
            a.db.flush()
        assert caught.value is chosen
        assert target not in unit.rollback_proofs and unit.failed
        assert not unit.internal_aborts and unit.native_abort is None
        with pytest.raises(ReminderResendError):
            a.db.commit()
    finally:
        monkeypatch.setattr(connection.dialect, "do_rollback", actual)
        # Native RootTransaction deactivates even when its driver rollback
        # fails. Explicit caller invalidation discards that failed driver;
        # it does not create a rollback proof for the failed operation.
        connection.invalidate()
        a.db.rollback()
    _independent_absent(a, lid)


def test_internal_flush_registration_lifecycle_and_later_patch(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    from dotmac_delivery_resend.fence import _unit

    unit = _unit(a.db)
    for index in range(3):
        notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title=f"flush {index}")
        a.db.flush()
        assert not unit.internal_aborts and unit.native_abort is None
    guarded = a.db.commit
    calls = []

    def later():
        calls.append(True)
        return guarded()

    a.db.commit = later
    try:
        a.db.commit()
        assert unit.ended and not unit.internal_aborts and unit.native_abort is None
        assert unit.frame is None and unit.db is None and a.db.commit is later
        _observe(a, lid, "sent", 1)
        resolve_database_runtime().set_tenant(a.db, a.tid)
        a.db.commit()
        assert calls == [True, True]
    finally:
        # Keep the established native pass-through semantics for fixture cleanup.
        a.db.commit = guarded


@pytest.mark.parametrize("position", ["before_observer", "after_observer"])
def test_handle_error_callback_order_does_not_supply_abort_authority(current_account, monkeypatch, position):
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    _duplicate_occurrence(a, conflict)
    connection = a.db.connection()
    target = connection.get_transaction()
    attempts = []

    def callback(context):
        if context.connection is connection:
            with pytest.raises(ReminderResendError):
                target.rollback()
            attempts.append(True)
        return None

    if position == "before_observer":
        event.listen(connection.engine, "handle_error", callback)
    try:
        with _native_abort_observer(a, connection, target, monkeypatch) as native:
            if position == "after_observer":
                event.listen(connection.engine, "handle_error", callback)
            try:
                with pytest.raises(IntegrityError) as caught:
                    a.db.commit()
                _assert_selected_abort(a, native, caught.value, target, "23505")
                assert attempts == [True]
                _independent_absent(a, lid)
            finally:
                if position == "after_observer":
                    event.remove(connection.engine, "handle_error", callback)
    finally:
        if position == "before_observer":
            event.remove(connection.engine, "handle_error", callback)
    _native_caller_reuse(a)


def test_internal_registration_constructor_failure_releases_only_own_state(current_account, monkeypatch):
    from dotmac_delivery_resend import fence as boundary

    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    unit = boundary._unit(a.db)
    actual = boundary._InternalAbort
    chosen = RuntimeError("injected internal registration construction failure")

    def failed(*args, **kwargs):
        raise chosen

    pending = notifications.notify(
        a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="pending constructor canary"
    )
    pending.title = "actual pending constructor canary flush"
    monkeypatch.setattr(boundary, "_InternalAbort", failed)
    try:
        with pytest.raises(RuntimeError) as caught:
            a.db.flush()
        assert caught.value is chosen
        assert not unit.internal_aborts and unit.native_abort is None
        assert boundary._unit(a.db) is unit
    finally:
        monkeypatch.setattr(boundary, "_InternalAbort", actual)
        a.db.rollback()
    assert unit.ended and not unit.internal_aborts and unit.native_abort is None
    _independent_absent(a, lid)


def test_independent_session_internal_flush_abort_leaves_other_unit_guarded(current_account, tenant_b, monkeypatch):
    from types import SimpleNamespace

    from dotmac_delivery_resend.fence import _unit
    from sqlalchemy.orm import sessionmaker

    from tests.test_issue2_ical_owner import create_roster_actor

    a = current_account
    _grant, slug = _allow(a)
    lid, conflict = _log(a), _log(a)
    setup = SimpleNamespace(db=a.db, admin=a.admin, tid=tenant_b.id, tenant=tenant_b)
    actor = create_roster_actor(setup, slug)
    a.db.commit()
    Factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    with Factory() as other:
        b = SimpleNamespace(
            db=other,
            admin=a.admin,
            pid=actor.pid,
            tid=tenant_b.id,
            tenant=tenant_b,
            token=actor.request.cookies["session"],
        )
        second = _log(b)
        resolve_database_runtime().set_tenant(a.db, a.tid)
        resolve_database_runtime().set_tenant(other, tenant_b.id)
        _invoke(a, lid)
        _invoke(b, second)
        first_unit, other_unit = _unit(a.db), _unit(other)
        assert first_unit.connection.dialect is other_unit.connection.dialect
        notifications.notify(other, tenant_id=b.tid, person_id=b.pid, kind="caller", title="interleaved other flush")
        other.flush()
        assert not other_unit.internal_aborts and other_unit.native_abort is None
        _duplicate_occurrence(a, conflict)
        connection = a.db.connection()
        target = connection.get_transaction()
        with _native_abort_observer(a, connection, target, monkeypatch) as native:
            with pytest.raises(IntegrityError) as caught:
                a.db.flush()
            _assert_selected_abort(a, native, caught.value, target, "23505")
            assert first_unit.ended and first_unit.native_abort is None
            assert not first_unit.internal_aborts and first_unit.db is None
            assert _unit(other) is other_unit and not other_unit.ended
            assert other_unit.frame is None and not other_unit.failed
            a.db.rollback()
            other.commit()
        _independent_absent(a, lid)
        _observe(b, second, "sent", 1)
        assert other_unit.ended and not other_unit.internal_aborts and other_unit.native_abort is None


@pytest.mark.parametrize("operation", ["commit", "rollback"])
def test_postexec_caught_completion_denial_cannot_authorize_later_commit(current_account, operation):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    row = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="postexec setup")
    row.title = "actual pending flush"
    connection = a.db.connection()
    target = connection.get_transaction()
    attempts = []

    def attempt(*args):
        with pytest.raises(ReminderResendError):
            getattr(target, operation)()
        attempts.append(True)

    event.listen(a.db, "after_flush_postexec", attempt)
    try:
        a.db.flush()
        assert attempts == [True] and target.is_active
        with pytest.raises(ReminderResendError):
            a.db.commit()
        _independent_absent(a, lid)
    finally:
        event.remove(a.db, "after_flush_postexec", attempt)
        a.db.rollback()
    assert not target.is_active
    _independent_absent(a, lid)


def test_refused_internal_abort_never_strands_wrapper_for_caller_rollback(current_account, monkeypatch):
    # A2-2: a native internal abort refused at the boundary association must not leave the
    # wrapper installed; the caller's later root rollback has to remain possible.
    a = current_account
    _allow(a)
    lid, conflict = _log(a), _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    from dotmac_delivery_resend.fence import _unit

    unit = _unit(a.db)
    a.db.begin_nested()  # after-return savepoint: SQLAlchemy creates the SAVEPOINT lazily
    # Fault injection: the after-return savepoint never gets an association/pair.
    monkeypatch.setattr(unit, "capture_association", lambda logical: None)
    _duplicate_occurrence(a, conflict)
    with pytest.raises(ReminderResendError, match="boundary association changed"):
        a.db.flush()
    assert unit.failed and unit.corrupt and not unit.internal_aborts
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    _independent_absent(a, lid)
    _observe(a, conflict, "queued", 0)


def test_forged_internal_rollback_during_flush_is_refused_and_caller_can_roll_back(current_account):
    # A2-2 (narrow variant): a forged call refused during an active flush keeps the wrapper for
    # the genuine flush abort; the flush completes, commit stays refused, rollback works.
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    from dotmac_delivery_resend.fence import _unit

    unit = _unit(a.db)
    attempts = []

    def forge(session, flush_context):
        with pytest.raises(ReminderResendError, match="no exact native flush origin"):
            flush_context.transaction.rollback()
        # Non-vacuous: the refused forged call must have kept the wrapper for the genuine abort.
        assert unit.internal_aborts.get(flush_context.transaction) is not None
        assert "rollback" in flush_context.transaction.__dict__
        attempts.append(True)

    event.listen(a.db, "after_flush", forge)
    try:
        # notify() flushes itself; it must be the flush the listener observes.
        notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="forged abort")
    finally:
        event.remove(a.db, "after_flush", forge)
    assert attempts == [True] and unit.failed and unit.corrupt
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    assert not unit.internal_aborts and unit.native_abort is None
    _independent_absent(a, lid)


def test_forged_internal_rollback_propagating_out_of_flush_keeps_native_abort_path(current_account):
    # R2b: the refused forged call escapes the listener. The genuine flush abort must still find
    # the wrapper (an "always remove" variant would instead raise "flush callback attempted
    # transaction completion"), and the caller can then roll back.
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    _actual_effects(a, lid)
    from dotmac_delivery_resend.fence import _unit

    unit = _unit(a.db)

    def forge(session, flush_context):
        flush_context.transaction.rollback()

    event.listen(a.db, "after_flush", forge)
    try:
        with pytest.raises(ReminderResendError, match="no exact native flush origin"):
            # notify() flushes itself; it must be the flush the listener observes.
            notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="forged abort escapes")
    finally:
        event.remove(a.db, "after_flush", forge)
    assert unit.failed and unit.corrupt
    # With no caller savepoint, the genuine abort rolls back the physical root and clears the unit;
    # the Session root is left inactive, so commit is refused by SQLAlchemy itself.
    assert _unit(a.db) is None and a.db.in_transaction() and not a.db.get_transaction().is_active
    with pytest.raises(PendingRollbackError):
        a.db.commit()
    a.db.rollback()
    assert not unit.internal_aborts and unit.native_abort is None
    _independent_absent(a, lid)
