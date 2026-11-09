"""Tests for the email subsystem: inert sender + auto-on-pass notification."""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.services import email as email_mod
from app.services.email import (
    notify_score_if_first_pass,
    recipient_allows,
    send_email,
)
from legacy_probe.conftest import make_person_party


def _seed(db, tid):
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    db.add(c)
    db.flush()
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                   title="Ch1 Test", pass_threshold=0.6, grading="auto", weight=1.0)
    db.add(act)
    db.flush()
    p = make_person_party(db, tenant_id=tid, email="learner@stu.edu", first_name="Lea", last_name="Rner")
    db.add(p)
    db.flush()
    return c, act, p

def _score(db, tid, act, p, *, frac, passed, attempt_no=1):
    sub = Submission(
        tenant_id=tid,
        activity_id=act.id,
        person_id=p.id,
        answers={},
        attempt_no=attempt_no,
    )
    db.add(sub)
    db.flush()
    s = Score(tenant_id=tid, submission_id=sub.id, score=frac * 10, max_score=10,
              fraction=frac, passed=passed, per_item=[], source="auto")
    db.add(s)
    db.flush()
    return s

def test_send_email_inert_when_unconfigured(monkeypatch):
    """No smtp_host => returns False, never raises, never touches the network."""
    # send_email resolves SMTP config via settings_store.effective(), which reads
    # the canonical app.config.settings singleton.
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "smtp_host", "", raising=False)
    assert send_email("x@y.z", "Subject", "<p>hi</p>") is False

def test_notify_first_pass_queues_once(admin_session, tenant_a):
    c, act, p = _seed(admin_session, tenant_a.id)
    s = _score(admin_session, tenant_a.id, act, p, frac=1.0, passed=True)

    queued = notify_score_if_first_pass(admin_session, score=s, activity=act, person=p)
    assert queued is True
    outbox = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "score_pass")
    ).one()
    assert outbox.recipient == "learner@stu.edu"
    assert outbox.status == "pending"
    admin_session.rollback()

def test_notify_second_pass_not_sent(admin_session, tenant_a, monkeypatch):
    calls = []
    monkeypatch.setattr(email_mod, "send_email",
                        lambda to, subject, html, text_body=None: calls.append((to, subject)) or True)
    c, act, p = _seed(admin_session, tenant_a.id)
    _score(admin_session, tenant_a.id, act, p, frac=1.0, passed=True)   # earlier pass exists
    s2 = _score(
        admin_session,
        tenant_a.id,
        act,
        p,
        frac=0.9,
        passed=True,
        attempt_no=2,
    )

    sent = notify_score_if_first_pass(admin_session, score=s2, activity=act, person=p)
    assert sent is False
    assert calls == []
    admin_session.rollback()

def test_notify_failing_score_not_sent(admin_session, tenant_a, monkeypatch):
    calls = []
    monkeypatch.setattr(email_mod, "send_email",
                        lambda to, subject, html, text_body=None: calls.append((to, subject)) or True)
    c, act, p = _seed(admin_session, tenant_a.id)
    s = _score(admin_session, tenant_a.id, act, p, frac=0.2, passed=False)

    sent = notify_score_if_first_pass(admin_session, score=s, activity=act, person=p)
    assert sent is False
    assert calls == []
    admin_session.rollback()

def test_recipient_allows_defaults_to_opted_in():
    class P:
        prefs = {}

    class Q:
        prefs = {"email_results": False}

    class R:
        prefs = None

    assert recipient_allows(P(), "email_results") is True
    assert recipient_allows(Q(), "email_results") is False
    assert recipient_allows(R(), "email_results") is True  # tolerant of None

def test_notify_respects_recipient_opt_out(admin_session, tenant_a, monkeypatch):
    calls = []
    monkeypatch.setattr(email_mod, "send_email",
                        lambda to, subject, html, text_body=None: calls.append(to) or True)
    c, act, p = _seed(admin_session, tenant_a.id)
    p.prefs = {"email_results": False}
    admin_session.flush()
    s = _score(admin_session, tenant_a.id, act, p, frac=1.0, passed=True)

    sent = notify_score_if_first_pass(admin_session, score=s, activity=act, person=p)
    assert sent is False
    assert calls == []
    admin_session.rollback()

def test_email_digest_cli_queues_each_instructor(admin_session, tenant_a):
    c, act, p = _seed(admin_session, tenant_a.id)
    coh = Cohort(tenant_id=tenant_a.id, name="Abuja 2026", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    inst = make_person_party(admin_session, tenant_id=tenant_a.id, email="instructor@a.edu", first_name="In", last_name="Structor")
    admin_session.add(inst)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=inst.id,
                                 role_in_cohort="instructor", status="active"))
    admin_session.commit()

    from app.cli import _email_digest
    _email_digest(argparse.Namespace())

    admin_session.rollback()
    queued = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "weekly_digest")
    ).all()
    assert "instructor@a.edu" in [row.recipient for row in queued]

def test_email_digest_skips_opted_out_instructor(admin_session, tenant_a):
    coh = Cohort(tenant_id=tenant_a.id, name="Abuja 2026", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    inst = make_person_party(admin_session, tenant_id=tenant_a.id, email="optout@a.edu", first_name="Opt", last_name="Out", prefs={"email_digest": False})
    admin_session.add(inst)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=inst.id,
                                 role_in_cohort="instructor", status="active"))
    admin_session.commit()

    from app.cli import _email_digest
    _email_digest(argparse.Namespace())

    admin_session.rollback()
    queued = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "weekly_digest")
    ).all()
    assert "optout@a.edu" not in [row.recipient for row in queued]
