"""Genuine hosted policy identity-map canaries; AUTHORED SOURCE ONLY / NOT RUN.

Each stale-row case retains the actual ORM object strongly while a separate
admin root commits a changed policy. Scalar settings reads must use stored
facts without clobbering that caller object's pending or stale state.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.orm import sessionmaker

from app import admission_assessment_context as boundary
from app.models.admissions import Applicant
from app.models.cohort import Cohort
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock
from legacy_probe.services.test_genuine_assessment_submit_context import _audit, _request, _submit, assessment_case
from legacy_probe.services.test_genuine_human_admissions_context import admission_case

assert assessment_case and admission_case and roster_actors  # Genuine hosted fixture registration only.

_KEY = "learning_exam_seconds_per_question"


@pytest.fixture
def fresh_policy_case(assessment_case):
    c = assessment_case
    old = c.a.admin.get(PlatformSetting, _KEY)
    existed, value = old is not None, old.value if old is not None else None
    settings_store.set_many(c.a.admin, {_KEY: "0"})
    c.a.admin.commit()
    try:
        yield c
    finally:
        c.a.db.rollback()
        if existed:
            c.a.admin.execute(update(PlatformSetting).where(PlatformSetting.key == _KEY).values(value=value))
        else:
            c.a.admin.execute(delete(PlatformSetting).where(PlatformSetting.key == _KEY))
        c.a.admin.commit()


def test_strong_stale_policy_object_cannot_hide_committed_change_at_precommit(fresh_policy_case):
    c = fresh_policy_case
    held = c.a.db.get(PlatformSetting, _KEY)
    assert held.value == "0"
    result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
    assert result.applicant.status == "onboarding"
    settings_store.set_many(c.a.admin, {_KEY: "1000"})
    c.a.admin.commit()  # Independent root commits after producer completion.
    assert held.value == "0" and inspect(held).persistent
    with pytest.raises(boundary.AssessmentSubmissionError, match="policy"):
        c.a.db.commit()
    assert held.value == "0"  # Scalar read preserves the old caller object.
    with pytest.raises(boundary.AssessmentSubmissionError):
        c.a.db.commit()
    c.a.db.rollback()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert c.a.db.get(Applicant, c.id).assessment_taken_at is None
    assert _audit(c) == []


def test_strong_stale_policy_object_cannot_supply_initial_grade_policy(fresh_policy_case):
    c = fresh_policy_case
    held = c.a.db.get(PlatformSetting, _KEY)
    assert held.value == "0"
    settings_store.set_many(c.a.admin, {_KEY: "1000"})
    c.a.admin.commit()
    assert held.value == "0"
    result = _submit(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
    assert result.applicant.assessment_score == 1
    assert result.applicant.assessment_valid is False
    assert result.applicant.status == "applied" and _audit(c) == []
    assert held.value == "0"
    c.a.db.commit()


@pytest.mark.parametrize("pending", ["dirty", "new", "deleted"])
def test_scalar_policy_reads_preserve_unflushed_caller_state(fresh_policy_case, pending):
    c = fresh_policy_case
    db = c.a.db
    old_autoflush = db.autoflush
    db.autoflush = False
    try:
        held = db.get(PlatformSetting, _KEY)
        assert held.value == "0"
        if pending == "dirty":
            held.value = "999"
            subject = held
        elif pending == "new":
            subject = PlatformSetting(key=_KEY, value="999")
            db.add(subject)
        else:
            subject = held
            db.delete(subject)
        assert settings_store.get_all(db)[_KEY] == "0"
        assert settings_store.effective(db, require_store=True)[_KEY] == 0
        if pending == "dirty":
            assert held.value == "999" and held in db.dirty
            assert tuple(inspect(held).attrs.value.history.added) == ("999",)
        elif pending == "new":
            assert subject in db.new and subject.value == "999"
        else:
            assert held in db.deleted and held.value == "0"
    finally:
        db.rollback()  # Never flush app_user's intentionally pending policy rows.
        db.autoflush = old_autoflush


def test_real_lock_wait_then_committed_policy_uses_fresh_stored_columns(fresh_policy_case):
    c = fresh_policy_case
    c.a.db.commit()
    UserSession = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    AdminSession = sessionmaker(bind=c.a.admin.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with AdminSession() as session:
            session.scalars(select(Cohort).where(Cohort.id == c.cohort_id).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            session.execute(update(PlatformSetting).where(PlatformSetting.key == _KEY).values(value="1000"))
            session.commit()

    def writer():
        with UserSession() as session:
            resolve_database_runtime().set_tenant(session, c.a.tid)
            held = session.get(PlatformSetting, _KEY)
            assert held.value == "0"
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            result = asyncio.run(
                boundary.submit_applicant_assessment(
                    session, request=_request(c, answers={f"q{i}": ["A"] for i in range(1, 5)})
                )
            )
            assert held.value == "0"  # Strong stale ref remains throughout the wait.
            assert result.applicant.assessment_score == 1
            assert result.applicant.assessment_valid is False and result.applicant.status == "applied"
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        assert locked.wait(10)
        follower = pool.submit(writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    c.a.db.expire_all()
    assert c.a.db.get(Applicant, c.id).assessment_valid is False
    assert _audit(c) == []


# Lab execution tests are authored for the whole hosted factory graph, including
# a006. They deliberately use the genuine existing admin/worker fixtures.
@pytest.fixture
def lab_policy_rows(admin_session):
    keys = (
        "lab_execution_policy_overrides",
        "branding_name",
        "max_concurrent_labs",
        "max_concurrent_labs_per_tenant",
        "lab_idle_minutes",
    )
    before = dict(
        admin_session.execute(
            select(PlatformSetting.key, PlatformSetting.value).where(PlatformSetting.key.in_(keys))
        ).all()
    )
    admin_session.execute(delete(PlatformSetting).where(PlatformSetting.key.in_(keys)))
    admin_session.commit()
    try:
        yield admin_session
    finally:
        admin_session.rollback()
        admin_session.execute(delete(PlatformSetting).where(PlatformSetting.key.in_(keys)))
        admin_session.add_all([PlatformSetting(key=key, value=value) for key, value in before.items()])
        admin_session.commit()


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
    db.add(PlatformSetting(key="lab_execution_policy_overrides", value=raw))
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
    db.add(PlatformSetting(key=key, value=raw))
    db.flush()
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
    assert db.get(PlatformSetting, "branding_name") is None
    assert db.get(PlatformSetting, "lab_execution_policy_overrides") is None


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
    assert db.get(PlatformSetting, "lab_execution_policy_overrides") is not None


@pytest.mark.parametrize(
    "initial,second_action", [("{}", "update"), ("ABSENT", "update"), ("{}", "delete"), (None, "update")]
)
def test_lab_override_whole_object_cas_races_refuse_loser_before_unrelated_writes(
    lab_policy_rows, admin_engine, monkeypatch, initial, second_action
):
    import json
    from threading import Barrier

    db = lab_policy_rows
    key = "lab_execution_policy_overrides"
    if initial != "ABSENT":
        db.add(PlatformSetting(key=key, value=initial))
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
    branding = db.get(PlatformSetting, "branding_name")
    assert (branding.value if branding else None) == (
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
        assert observer.get(PlatformSetting, "lab_execution_policy_overrides").value == "{}"
    db.rollback()
    assert db.get(PlatformSetting, "branding_name").value == "before"
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
    prior = db.get(PlatformSetting, key)
    old = (prior is not None, prior.value if prior is not None else None)
    try:
        db.execute(delete(PlatformSetting).where(PlatformSetting.key == key))
        db.add(PlatformSetting(key=key, value=None))
        db.flush()
        with pytest.raises(ValueError, match="split"):
            settings_store.execution_policy(db)
        with pytest.raises(ValueError, match="split"):
            settings_store.set_many(db, {"lab_execution_policy_overrides": "{}"})
        assert db.get(PlatformSetting, "lab_execution_policy_overrides") is None
    finally:
        db.rollback()
        # No helper commit: rollback restores the exact original split-row fact.
        row = db.get(PlatformSetting, key)
        assert (row is not None, row.value if row else None) == old


def test_lab_override_delete_preserves_public_semantics_with_loaded_row(lab_policy_rows):
    db = lab_policy_rows
    settings_store.set_many(db, {"lab_execution_policy_overrides": '{"lab_worker_poll_interval_seconds":9}'})
    db.commit()
    loaded = db.get(PlatformSetting, "lab_execution_policy_overrides")
    assert loaded is not None
    settings_store.delete(db, "lab_execution_policy_overrides")
    assert db.get(PlatformSetting, "lab_execution_policy_overrides") is None
    assert settings_store.execution_policy(db).lab_worker_poll_interval_seconds == 5
    db.rollback()
    assert db.get(PlatformSetting, "lab_execution_policy_overrides").value == '{"lab_worker_poll_interval_seconds":9}'
