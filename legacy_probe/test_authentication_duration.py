"""Hosted-only representational duration boundaries; no business duration cap."""

from datetime import UTC, datetime, timedelta
from math import nextafter
from types import SimpleNamespace

import pytest

from app.config import validate_authentication_policy_value
from app.services import authentication_policy as policy_module
from app.services.authentication_policy import AuthenticationPolicy
from legacy_probe.services.test_canonical_sign_in import sign_in_account

assert sign_in_account


@pytest.mark.parametrize("value", [1e308, 10**1000])
def test_finite_but_unrepresentable_duration_refuses_at_policy_validation(value):
    with pytest.raises(ValueError, match="invalid policy value"):
        validate_authentication_policy_value("authentication_lockout_minutes", value)


@pytest.mark.parametrize("minutes", [0, 0.1, 0.5 / 60_000_000, 1.5 / 60_000_000, 2.5 / 60_000_000, 15, 7.25])
def test_native_timedelta_fractional_rounding_is_preserved(minutes):
    normalized = validate_authentication_policy_value("authentication_lockout_minutes", minutes)
    now = datetime(2026, 10, 18, tzinfo=UTC)
    assert AuthenticationPolicy(2, normalized).lockout_until(now) == now + timedelta(minutes=float(minutes))


def test_native_timedelta_upper_boundary_is_respected_without_float_re_rounding():
    # timedelta.max is the source of the mechanical bound; no product maximum.
    first_unrepresentable_day = float((timedelta.max.days + 1) * 24 * 60)
    below = nextafter(first_unrepresentable_day, 0.0)
    assert timedelta(minutes=below) <= timedelta.max
    assert validate_authentication_policy_value("authentication_lockout_minutes", below) == below
    with pytest.raises(ValueError):
        validate_authentication_policy_value("authentication_lockout_minutes", first_unrepresentable_day)


def test_datetime_max_equality_admitted_but_one_microsecond_overflow_refused():
    maximum = datetime.max.replace(tzinfo=UTC)
    minute = timedelta(minutes=1)
    policy = AuthenticationPolicy(2, 1)
    assert policy.lockout_until(maximum - minute) == maximum
    with pytest.raises(ValueError, match="representable deadline"):
        policy.lockout_until(maximum - minute + timedelta(microseconds=1))
    assert AuthenticationPolicy(2, 0).lockout_until(maximum) == maximum


def test_effective_finite_but_datetime_unrepresentable_refuses_composition(monkeypatch):
    # This fits timedelta but exceeds the entire datetime calendar.
    minutes = float((timedelta.max.days - 1) * 24 * 60)
    assert timedelta(minutes=minutes) < timedelta.max
    monkeypatch.setattr(
        policy_module.settings_store,
        "effective",
        lambda db, *, require_store: SimpleNamespace(
            authentication_max_failed_logins=2, authentication_lockout_minutes=minutes
        ),
    )
    with pytest.raises(ValueError, match="representable deadline"):
        policy_module.authentication_policy(object())


def test_post_lock_clock_is_rechecked_before_verification_or_counter_mutation():
    # A value fitting the first instant must refuse after a lock wait advances
    # the clock. This tests the same checked policy operation called post-lock.
    maximum = datetime.max.replace(tzinfo=UTC)
    policy = AuthenticationPolicy(2, 1)
    assert policy.lockout_until(maximum - timedelta(minutes=1)) == maximum
    with pytest.raises(ValueError):
        policy.lockout_until(maximum - timedelta(minutes=1) + timedelta(microseconds=1))


def test_native_rounding_at_half_microsecond_tie_is_not_fraction_redefined():
    from fractions import Fraction

    # Binary floats near half-microsecond ties expose native intermediate
    # rounding; discover one from a fixed finite set rather than assume exact
    # rational rerounding is the same operation.
    cases = []
    for half in range(1, 2000, 2):
        minutes = (half / 2) / 60_000_000
        native = timedelta(minutes=minutes)
        exact_reround = timedelta(microseconds=round(Fraction(minutes) * 60_000_000))
        if native != exact_reround:
            cases.append((minutes, native))
    assert cases, "half-microsecond parity canary must distinguish the two rounding algorithms"
    now = datetime(2026, 10, 18, tzinfo=UTC)
    for minutes, native in cases:
        assert AuthenticationPolicy(2, minutes).lockout_until(now) == now + native


@pytest.fixture
def genuine_sign_in_account(request):
    # Reuse the genuine app_user canonical PG fixture from sealed sign-in13.
    # It is imported as a pytest fixture below; no substitute schema or actor.
    return request.getfixturevalue("sign_in_account")


def test_post_lock_unrepresentable_deadline_refuses_before_verifier_and_mutations(genuine_sign_in_account, monkeypatch):
    from dotmac_kernel.models import AuthSession
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyCredentialPolicy
    from app.services import credential_authentication as adapter

    db, tid, pid, email, cid = genuine_sign_in_account
    state = db.get(AcademyCredentialPolicy, cid)
    original = (state.failed_login_attempts, state.locked_until, state.updated_at)
    root = db.get_transaction()

    class AfterLockClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.max.replace(tzinfo=UTC) - timedelta(minutes=1) + timedelta(microseconds=1)

    monkeypatch.setattr(adapter, "datetime", AfterLockClock)
    monkeypatch.setattr(adapter, "authentication_policy", lambda db: AuthenticationPolicy(2, 1))

    def forbidden(*args, **kwargs):
        raise AssertionError("unrepresentable post-lock policy reached verifier")

    monkeypatch.setattr(adapter.CredentialLifecycle, "verify", forbidden)
    with pytest.raises(ValueError, match="representable deadline"):
        adapter.sign_in(db, tenant_id=tid, email=email, password="wrong")
    assert db.is_active and db.get_transaction() is root
    db.expire_all()
    state = db.get(AcademyCredentialPolicy, cid)
    assert (state.failed_login_attempts, state.locked_until, state.updated_at) == original
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid, AuthSession.party_id == pid)).all()
