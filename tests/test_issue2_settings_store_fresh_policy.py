"""Maintained port of legacy_probe/services/test_settings_store_fresh_policy.py.

Issue #2 wave 4: DISPOSITIONS row -> test_settings_store_fresh_read_never_stale_or_fallback,
plus the lab execution policy / CAS contract (ruling 6: ``_lab_proposal`` /
``LabExecutionPolicyConflict`` are implemented in settings_store).
PlatformSetting fixtures port to kernel DomainSetting via settings_store.
The legacy genuine assessment-submit cases are ported at the store level
here (fresh scalar reads against a retained stale ORM object); the submit
boundary itself is disposition row
services/test_genuine_assessment_submit_context.py. Kernel storage forbids
NULL-valued rows, so stored-NULL params assert the insert is refused.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dotmac_kernel.settings_models import DomainSetting
from sqlalchemy import create_engine, delete, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.services import settings_store

_KEY = "learning_exam_seconds_per_question"


def _factory(genesis_database):
    admin, _superuser, _name = genesis_database
    return sessionmaker(bind=admin, autoflush=False)


@pytest.fixture
def admin_session(genesis_database):
    db = _factory(genesis_database)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


@pytest.fixture
def admin_engine(genesis_database):
    return genesis_database[0]


@pytest.fixture
def lab_worker_session(genesis_database):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name, username="academy_lab_worker", password=None)
    engine = create_engine(url.render_as_string(hide_password=False), future=True)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def _row(db, key):
    return db.scalars(
        select(DomainSetting).where(
            DomainSetting.domain == "academy",
            DomainSetting.scope_kind == "platform",
            DomainSetting.key == key,
        )
    ).one_or_none()


def _store_raw(db, key, raw):
    db.execute(
        delete(DomainSetting).where(
            DomainSetting.domain == "academy",
            DomainSetting.scope_kind == "platform",
            DomainSetting.key == key,
        )
    )
    db.add(
        DomainSetting(tenant_id=None, scope_kind="platform", domain="academy", key=key, value_text=raw, value_json=None)
    )
    db.flush()


@pytest.fixture
def fresh_policy_factory(genesis_database):
    factory = _factory(genesis_database)
    with factory() as db:
        _store_raw(db, _KEY, "0")  # raw text, as legacy stored it (float key would store "0.0")
        db.commit()
    return factory


def test_settings_store_fresh_read_never_stale_or_fallback(fresh_policy_factory, monkeypatch):
    factory = fresh_policy_factory
    with factory() as db:
        held = _row(db, _KEY)
        assert held.value_text == "0"
        with factory() as other:
            _store_raw(other, _KEY, "1000")
            other.commit()
        assert held.value_text == "0" and inspect(held).persistent
        # Scalar column reads observe the committed change, never the stale object.
        assert settings_store.get_all(db)[_KEY] == "1000"
        assert settings_store.effective(db, require_store=True)[_KEY] == 1000
        assert held.value_text == "0"  # Scalar read preserves the old caller object.

        def denied(db_):
            raise RuntimeError("store denied")

        monkeypatch.setattr(settings_store, "get_all", denied)
        with pytest.raises(RuntimeError, match="store denied"):
            settings_store.effective(db, require_store=True)
    with pytest.raises(ValueError, match="session"):
        settings_store.effective(None, require_store=True)


def test_strong_stale_policy_object_cannot_hide_committed_change_at_precommit(fresh_policy_factory):
    factory = fresh_policy_factory
    with factory() as db:
        held = _row(db, _KEY)
        assert held.value_text == "0"
        assert settings_store.effective(db, require_store=True)[_KEY] == 0
        with factory() as other:
            _store_raw(other, _KEY, "1000")
            other.commit()  # Independent root commits after producer read.
        assert held.value_text == "0" and inspect(held).persistent
        # A pre-commit re-read in the caller's transaction sees fresh policy.
        assert settings_store.effective(db, require_store=True)[_KEY] == 1000
        assert held.value_text == "0"
        db.rollback()


def test_strong_stale_policy_object_cannot_supply_initial_grade_policy(fresh_policy_factory):
    factory = fresh_policy_factory
    with factory() as db:
        held = _row(db, _KEY)
        assert held.value_text == "0"
        with factory() as other:
            _store_raw(other, _KEY, "1000")
            other.commit()
        assert held.value_text == "0"
        assert settings_store.effective(db, require_store=True)[_KEY] == 1000
        assert held.value_text == "0"
        db.rollback()


@pytest.mark.parametrize("pending", ["dirty", "new", "deleted"])
def test_scalar_policy_reads_preserve_unflushed_caller_state(fresh_policy_factory, pending):
    factory = fresh_policy_factory
    with factory() as db:
        try:
            held = _row(db, _KEY)
            assert held.value_text == "0"
            if pending == "dirty":
                held.value_text = "999"
                subject = held
            elif pending == "new":
                subject = DomainSetting(
                    tenant_id=None, scope_kind="platform", domain="academy", key=_KEY, value_text="999", value_json=None
                )
                db.add(subject)
            else:
                subject = held
                db.delete(subject)
            assert settings_store.get_all(db)[_KEY] == "0"
            assert settings_store.effective(db, require_store=True)[_KEY] == 0
            if pending == "dirty":
                assert held.value_text == "999" and held in db.dirty
                assert tuple(inspect(held).attrs.value_text.history.added) == ("999",)
            elif pending == "new":
                assert subject in db.new and subject.value_text == "999"
            else:
                assert held in db.deleted and held.value_text == "0"
        finally:
            db.rollback()  # Never flush the intentionally pending policy rows.


def test_real_lock_wait_then_committed_policy_uses_fresh_stored_columns(fresh_policy_factory):
    factory = fresh_policy_factory
    locked, release = Event(), Event()
    observed = {}

    def holder():
        with factory() as session:
            row = session.scalars(select(DomainSetting).where(DomainSetting.key == _KEY).with_for_update()).one()
            row.value_text = "1000"
            session.flush()
            locked.set()
            assert release.wait(10)
            session.commit()

    def writer():
        with factory() as session:
            held = _row(session, _KEY)
            assert held.value_text == "0"
            assert locked.wait(10)
            # The holder's uncommitted change is invisible; no stale fallback either.
            observed["during"] = settings_store.get_all(session)[_KEY]
            release.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        follower = pool.submit(writer)
        follower.result(timeout=20)
        leader.result(timeout=20)
    assert observed["during"] == "0"
    with factory() as session:
        assert settings_store.get_all(session)[_KEY] == "1000"
        assert settings_store.effective(session, require_store=True)[_KEY] == 1000


# Lab execution tests use the maintained genesis admin/worker sessions; each
# test gets a fresh genesis database, so no row snapshot/restore is needed.
@pytest.fixture
def lab_policy_rows(admin_session):
    yield admin_session
    admin_session.rollback()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "null",
        "[]",
        "true",
        "false",
        "1",
        "1.5",
        "{",
        '{"extra":1}',
        '{"lab_worker_poll_interval_seconds":true}',
        '{"lab_worker_poll_interval_seconds":1.5}',
        '{"lab_worker_poll_interval_seconds":null}',
        '{"lab_worker_poll_interval_seconds":"2"}',
        '{"lab_worker_poll_interval_seconds":1,"lab_worker_poll_interval_seconds":2}',
        '{"lab_operation_lease_seconds_by_kind":{"deploy":1200,"destroy":600}}',
        '{"lab_operation_lease_seconds_by_kind":{"deploy":1200,"destroy":600,"check":600,"extra":1}}',
        '{"lab_operation_lease_seconds_by_kind":{"deploy":1200,"destroy":600,"check":600,"check":700}}',
        '{"lab_operation_lease_seconds_by_kind":{"deploy":true,"destroy":600,"check":600}}',
        '{"lab_operation_unknown_lease_seconds":2147483648}',
    ],
)
def test_lab_execution_stored_malformed_values_never_fall_back(lab_policy_rows, raw):
    db = lab_policy_rows
    if raw is None:
        # Kernel storage forbids the NULL-valued row outright.
        with pytest.raises(IntegrityError):
            _store_raw(db, "lab_execution_policy_overrides", None)
        db.rollback()
        return
    _store_raw(db, "lab_execution_policy_overrides", raw)
    db.commit()
    with pytest.raises(ValueError):
        settings_store.execution_policy(db)
    db.rollback()


@pytest.mark.parametrize(
    "key,raw",
    [
        ("max_concurrent_labs", "true"),
        ("max_concurrent_labs", "1.5"),
        ("max_concurrent_labs", None),
        ("max_concurrent_labs", "0"),
        ("max_concurrent_labs_per_tenant", "-1"),
        ("lab_idle_minutes", "0"),
    ],
)
def test_lab_capacity_raw_values_refuse_before_generic_coercion(lab_policy_rows, key, raw):
    db = lab_policy_rows
    if raw is None:
        with pytest.raises(IntegrityError):
            _store_raw(db, key, None)
        db.rollback()
        return
    _store_raw(db, key, raw)
    with pytest.raises(ValueError):
        settings_store.execution_policy(db)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lab_operation_lease_seconds_by_kind": {"deploy": 950, "destroy": 600, "check": 600}},
        {"lab_command_timeout_seconds_by_action": {"inspect": 100, "deploy": 900, "destroy": 420, "check": 480}},
        {"lab_command_timeout_seconds_by_action": {"inspect": 60, "deploy": 900, "destroy": 420, "check": 593}},
        {"lab_operation_lease_seconds_by_kind": {"deploy": 970, "destroy": 600, "check": 600}},
        {"lab_operation_lease_seconds_by_kind": {"deploy": 1200, "destroy": 550, "check": 600}},
        {"lab_operation_lease_seconds_by_kind": {"deploy": 1200, "destroy": 600, "check": 490}},
        {"lab_operation_unknown_lease_seconds": 70},
    ],
)
def test_lab_combined_wait_and_two_grace_budget_refuses_before_any_write(lab_policy_rows, overrides):
    import json

    db = lab_policy_rows
    with pytest.raises(ValueError):
        settings_store.set_many(
            db, {"branding_name": "must-not-write", "lab_execution_policy_overrides": json.dumps(overrides)}
        )
    assert _row(db, "branding_name") is None
    assert _row(db, "lab_execution_policy_overrides") is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("lab_worker_poll_interval_seconds", True),
        ("lab_operation_unknown_max_attempts", 1.5),
        ("lab_idle_minutes", None),
        ("max_concurrent_labs", False),
        ("lab_command_timeout_seconds_by_action", '{"inspect":60,"deploy":900,"destroy":420,"check":480,"check":490}'),
    ],
)
def test_lab_environment_values_use_the_same_strict_raw_validators(key, value):
    from app.config import Settings

    with pytest.raises(ValueError):
        Settings(**{key: value})


def test_lab_snapshot_copies_maps_and_preserves_zero_capacity_inheritance(lab_policy_rows, monkeypatch):
    from app.config import settings

    db = lab_policy_rows
    settings_store.set_many(db, {"max_concurrent_labs": "7", "max_concurrent_labs_per_tenant": "0"})
    effective = settings_store.effective(db, require_store=True)
    from app.config import LabExecutionPolicy

    policy = LabExecutionPolicy.from_settings(effective)
    effective.lab_operation_lease_seconds_by_kind["deploy"] = 1
    monkeypatch.setattr(settings, "lab_operation_lease_seconds_by_kind", {"deploy": 1800, "destroy": 600, "check": 600})
    assert policy.lease_seconds("deploy") == 1200
    assert policy.max_concurrent_labs == 7
    assert policy.max_concurrent_labs_per_tenant == 0
    assert policy.lease_seconds("unknown") == 600
    assert policy.max_attempts("unknown") == 3
    with pytest.raises(TypeError):
        policy.lab_operation_lease_seconds_by_kind["deploy"] = 2
    with pytest.raises(AttributeError):
        policy.lab_worker_poll_interval_seconds = 1


def test_lab_missing_store_and_actual_worker_read_denial_propagate(lab_policy_rows, lab_worker_session, monkeypatch):
    with pytest.raises(ValueError, match="session"):
        settings_store.execution_policy(None)
    # The genuine worker role must read the actual global Settings table.
    assert lab_worker_session.scalar(text("SELECT current_user")) == "academy_lab_worker"
    assert settings_store.execution_policy(lab_worker_session).lease_seconds("deploy") == 1200

    def denied(db):
        raise PermissionError("store denied")

    monkeypatch.setattr(settings_store, "get_all", denied)
    with pytest.raises(PermissionError, match="store denied"):
        settings_store.execution_policy(lab_worker_session)


def test_lab_delete_revalidates_merged_defaults_before_effects(lab_policy_rows, monkeypatch):
    from app.config import settings

    db = lab_policy_rows
    settings_store.set_many(
        db,
        {
            "lab_execution_policy_overrides": '{"lab_operation_lease_seconds_by_kind":{"deploy":1200,"destroy":600,"check":600}}'
        },
    )
    db.commit()
    monkeypatch.setattr(settings, "lab_operation_lease_seconds_by_kind", {"deploy": 970, "destroy": 600, "check": 600})
    with pytest.raises(ValueError):
        settings_store.delete(db, "lab_execution_policy_overrides")
    assert _row(db, "lab_execution_policy_overrides") is not None


# The legacy (None, "update") param stored a NULL overrides row; kernel
# storage forbids NULL-valued rows, so that base state cannot exist in v2.
@pytest.mark.parametrize("initial,second_action", [("{}", "update"), ("ABSENT", "update"), ("{}", "delete")])
def test_lab_override_whole_object_cas_races_refuse_loser_before_unrelated_writes(
    lab_policy_rows, admin_engine, monkeypatch, initial, second_action
):
    import json
    from threading import Barrier

    db = lab_policy_rows
    key = "lab_execution_policy_overrides"
    if initial != "ABSENT":
        settings_store.set_many(db, {key: initial})
        db.commit()
    barrier = Barrier(2)
    original = settings_store._lab_proposal

    def observed(session, updates, *, removed=None):
        result = original(session, updates, removed=removed)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(settings_store, "_lab_proposal", observed)
    factory = sessionmaker(bind=admin_engine, autoflush=False)

    def writer(index):
        with factory() as session:
            try:
                if index == 1 and second_action == "delete":
                    settings_store.delete(session, key)
                else:
                    settings_store.set_many(
                        session,
                        {
                            key: json.dumps({"lab_worker_poll_interval_seconds": index + 7}),
                            "branding_name": f"winner-{index}",
                        },
                    )
                assert session.in_transaction()
                session.commit()
                return index, "won"
            except settings_store.LabExecutionPolicyConflict:
                assert session.in_transaction()  # No hidden abort, commit or retry.
                session.rollback()
                return index, "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(writer, 0), pool.submit(writer, 1)
        results = [a.result(timeout=20), b.result(timeout=20)]
    assert sorted(result for _, result in results) == ["conflict", "won"]
    winner = next(index for index, result in results if result == "won")
    db.expire_all()
    assert settings_store.get_all(db).get("branding_name") == (
        None if winner == 1 and second_action == "delete" else f"winner-{winner}"
    )
    monkeypatch.setattr(settings_store, "_lab_proposal", original)
    settings_store.set_many(db, {key: '{"lab_worker_poll_interval_seconds":11}'})
    assert settings_store.execution_policy(db).lab_worker_poll_interval_seconds == 11
    db.rollback()


def test_lab_cas_keeps_caller_transaction_and_unrelated_settings(lab_policy_rows, admin_engine):
    db = lab_policy_rows
    settings_store.set_many(db, {"branding_name": "before", "lab_execution_policy_overrides": "{}"})
    db.commit()
    settings_store.set_many(db, {"lab_execution_policy_overrides": '{"lab_worker_poll_interval_seconds":9}'})
    assert db.in_transaction()
    with sessionmaker(bind=admin_engine)() as observer:
        assert settings_store.get_all(observer)["lab_execution_policy_overrides"] == {}
    db.rollback()
    assert _row(db, "branding_name").value_text == "before"
    assert settings_store.execution_policy(db).lab_worker_poll_interval_seconds == 5


@pytest.mark.parametrize(
    "name,raw",
    [
        ("LAB_WORKER_POLL_INTERVAL_SECONDS", "true"),
        ("LAB_OPERATION_UNKNOWN_LEASE_SECONDS", "1.5"),
        ("LAB_IDLE_MINUTES", "null"),
        ("LAB_COMMAND_TIMEOUT_SECONDS_BY_ACTION", '{"inspect":60,"deploy":900,"destroy":420,"check":480,"check":490}'),
        ("LAB_OPERATION_LEASE_SECONDS_BY_KIND", '{"deploy":1200,"destroy":600,"check":true}'),
    ],
)
def test_lab_environment_source_preserves_raw_values_and_duplicate_keys(monkeypatch, name, raw):
    from app.config import Settings

    monkeypatch.setenv(name, raw)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_lab_execution_split_rows_refuse_instead_of_silently_becoming_defaults(lab_policy_rows):
    db = lab_policy_rows
    key = "lab_worker_poll_interval_seconds"
    assert _row(db, key) is None  # Fresh genesis database: no split row yet.
    try:
        _store_raw(db, key, "7")
        with pytest.raises(ValueError, match="split"):
            settings_store.execution_policy(db)
        with pytest.raises(ValueError, match="split"):
            settings_store.set_many(db, {"lab_execution_policy_overrides": "{}"})
        assert _row(db, "lab_execution_policy_overrides") is None
    finally:
        db.rollback()
        # No helper commit: rollback restores the exact original split-row fact.
        assert _row(db, key) is None


def test_lab_override_delete_preserves_public_semantics_with_loaded_row(lab_policy_rows):
    db = lab_policy_rows
    settings_store.set_many(db, {"lab_execution_policy_overrides": '{"lab_worker_poll_interval_seconds":9}'})
    db.commit()
    loaded = _row(db, "lab_execution_policy_overrides")
    assert loaded is not None
    settings_store.delete(db, "lab_execution_policy_overrides")
    assert _row(db, "lab_execution_policy_overrides") is None
    assert settings_store.execution_policy(db).lab_worker_poll_interval_seconds == 5
    db.rollback()
    assert settings_store.get_all(db)["lab_execution_policy_overrides"] == {"lab_worker_poll_interval_seconds": 9}
