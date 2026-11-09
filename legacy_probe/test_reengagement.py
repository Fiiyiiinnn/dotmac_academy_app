"""Stranded-learner detection and the activation re-invite wave.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from legacy_probe.conftest import make_person_credential, make_person_party


def _cohort(admin_session, tenant, name="Stranded"):
    from app.models.cohort import Cohort

    c = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active")
    admin_session.add(c)
    admin_session.commit()
    return c

def _enrolled(admin_session, tenant, cohort, email, *, role="student", status="active"):
    from app.models.cohort import Enrollment

    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="En", last_name="Rolled", status="active")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant.id, cohort_id=cohort.id, person_id=p.id, role_in_cohort=role, status=status
        )
    )
    admin_session.commit()
    return p

def _give_password(admin_session, tenant, person):
    from app.services.security import hash_password

    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=person.id, email=person.email, password_hash=hash_password("correct horse battery staple"))
    )
    admin_session.commit()

def _give_session(admin_session, tenant, person):
    from app.models.auth import AuthSession

    admin_session.add(
        AuthSession(
            tenant_id=tenant.id,
            party_id=person.id,
            token_hash="a" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    admin_session.commit()

def test_stranded_excludes_anyone_who_can_or_did_log_in(app_client, tenant_a, admin_session):
    from app.services.reengagement import stranded_learners

    cohort = _cohort(admin_session, tenant_a)
    _enrolled(admin_session, tenant_a, cohort, "stranded@a.ex")
    with_password = _enrolled(admin_session, tenant_a, cohort, "haspw@a.ex")
    logged_in = _enrolled(admin_session, tenant_a, cohort, "loggedin@a.ex")
    _give_password(admin_session, tenant_a, with_password)
    _give_session(admin_session, tenant_a, logged_in)

    emails = {p.email for p in stranded_learners(admin_session, tenant_id=tenant_a.id)}
    assert emails == {"stranded@a.ex"}

def test_stranded_ignores_instructors_and_dropped_enrolments(app_client, tenant_a, admin_session):
    from app.services.reengagement import stranded_learners

    cohort = _cohort(admin_session, tenant_a, name="Roles")
    _enrolled(admin_session, tenant_a, cohort, "teacher@a.ex", role="instructor")
    _enrolled(admin_session, tenant_a, cohort, "dropped@a.ex", status="withdrawn")
    keep = _enrolled(admin_session, tenant_a, cohort, "keep@a.ex")

    found = stranded_learners(admin_session, tenant_id=tenant_a.id)
    assert [p.id for p in found] == [keep.id]

def test_reinvite_queues_one_activation_email_each(app_client, tenant_a, admin_session):
    from sqlalchemy import select

    from app.models.account_token import AccountToken
    from app.models.email_outbox import EmailOutbox
    from app.services.reengagement import reinvite_stranded

    cohort = _cohort(admin_session, tenant_a, name="Wave")
    _enrolled(admin_session, tenant_a, cohort, "w1@a.ex")
    _enrolled(admin_session, tenant_a, cohort, "w2@a.ex")

    res = reinvite_stranded(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io/"
    )
    admin_session.commit()
    assert res == {"targets": 2, "queued": 2, "failed": 0}

    mails = list(
        admin_session.scalars(
            select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id).where(EmailOutbox.kind == "account_invite")
        )
    )
    assert {m.recipient for m in mails} == {"w1@a.ex", "w2@a.ex"}
    assert all(m.subject == "Activate your Dotmac Academy account" for m in mails)
    # The link must be the real accept route, and the raw token never leaks a
    # trailing double slash from the base URL.
    assert all("/accept-invite?token=" in m.text_body for m in mails)
    assert all("//accept-invite" not in m.text_body for m in mails)

    tokens = list(
        admin_session.scalars(
            select(AccountToken).where(AccountToken.tenant_id == tenant_a.id).where(AccountToken.kind == "invite")
        )
    )
    assert len(tokens) == 2
    assert all(t.used_at is None and t.expires_at > datetime.now(UTC) for t in tokens)

def test_reinvite_scoped_to_one_cohort(app_client, tenant_a, admin_session):
    from app.services.reengagement import reinvite_stranded

    target = _cohort(admin_session, tenant_a, name="Target")
    other = _cohort(admin_session, tenant_a, name="Other")
    _enrolled(admin_session, tenant_a, target, "in@a.ex")
    _enrolled(admin_session, tenant_a, other, "out@a.ex")

    res = reinvite_stranded(
        admin_session, tenant_id=tenant_a.id, base_url="https://academy.dotmac.io", cohort_id=target.id
    )
    admin_session.commit()
    assert res["targets"] == 1 and res["queued"] == 1
