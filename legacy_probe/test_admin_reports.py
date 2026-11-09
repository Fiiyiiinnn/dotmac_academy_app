"""Admin activity report — snapshot numbers and recipient selection.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from legacy_probe.conftest import make_person_party


def _mk_applicant(admin_session, tenant, email, **kw):
    from app.models.admissions import Applicant

    a = Applicant(
        tenant_id=tenant.id,
        email=email,
        first_name="Rep",
        last_name="Ort",
        status=kw.pop("status", "applied"),
        **kw,
    )
    admin_session.add(a)
    admin_session.commit()
    return a

def test_snapshot_counts_window_activity(app_client, tenant_a, admin_session):
    from app.models.rbac import AuditEvent
    from app.services.admin_reports import activity_snapshot

    now = datetime.now(UTC)
    accepted = _mk_applicant(
        admin_session,
        tenant_a,
        "rep1@a.ex",
        status="onboarding",
        assessment_taken_at=now,
        assessment_score=0.9,
        assessment_valid=True,
    )
    waitlisted = _mk_applicant(
        admin_session,
        tenant_a,
        "rep2@a.ex",
        status="waitlisted",
        assessment_taken_at=now,
        assessment_score=0.3,
        assessment_valid=True,
    )
    _mk_applicant(
        admin_session,
        tenant_a,
        "rep3@a.ex",
        status="applied",
        assessment_taken_at=now,
        assessment_score=0.9,
        assessment_valid=False,
    )
    admin_session.add_all(
        [
            AuditEvent(
                tenant_id=tenant_a.id,
                action="applicant.transition",
                entity_type="applicant",
                entity_id=str(accepted.id),
                details={
                    "from_status": "applied",
                    "to_status": "onboarding",
                    "source": "assessment_policy",
                },
            ),
            AuditEvent(
                tenant_id=tenant_a.id,
                action="applicant.transition",
                entity_type="applicant",
                entity_id=str(waitlisted.id),
                details={
                    "from_status": "applied",
                    "to_status": "waitlisted",
                    "source": "assessment_policy",
                },
            ),
        ]
    )
    admin_session.commit()

    snap = activity_snapshot(admin_session, tenant_id=tenant_a.id, since=now - timedelta(hours=24))
    assert snap["new_applications"] == 3
    assert snap["sittings"] == 3
    assert snap["sittings_valid"] == 2
    assert snap["auto_accepted"] == 1
    assert snap["auto_waitlisted"] == 1
    assert snap["invalid_awaiting_review"] == 1
    assert snap["pipeline"]["applied"] == 1
    assert snap["pipeline"]["onboarding"] == 1

    # Outside the window nothing counts.
    old = activity_snapshot(admin_session, tenant_id=tenant_a.id, since=now + timedelta(hours=1))
    assert old["new_applications"] == 0
    assert old["sittings"] == 0
    # ...but the pipeline totals are still the current state.
    assert old["pipeline"]["applied"] == 1

def test_admin_recipients_only_active_admins(app_client, tenant_a, admin_session):
    from app.services.admin_reports import admin_recipients
    from legacy_probe.conftest import make_canonical_account as create_user

    create_user(
        admin_session,
        tenant_id=tenant_a.id,
        email="boss@a.ex",
        first_name="Bo",
        last_name="Ss",
        password="correct horse battery staple",
        role="admin",
    )
    create_user(
        admin_session,
        tenant_id=tenant_a.id,
        email="teach@a.ex",
        first_name="Te",
        last_name="Ach",
        password="correct horse battery staple",
        role="instructor",
    )
    admin_session.commit()

    emails = {p.email for p in admin_recipients(admin_session, tenant_id=tenant_a.id)}
    assert "boss@a.ex" in emails
    assert "teach@a.ex" not in emails

# ---------------------------------------------------------------------------
# Learner-side reporting (engagement, top movers, attention list)
# ---------------------------------------------------------------------------

def _mk_learner(admin_session, tenant, cohort, email, *, first="Lea", last="Rner", role="student"):
    from app.models.cohort import Enrollment

    person = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name=first, last_name=last, status="active")
    admin_session.add(person)
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant.id,
            cohort_id=cohort.id,
            person_id=person.id,
            role_in_cohort=role,
            status="active",
        )
    )
    admin_session.commit()
    return person

def _mk_cohort(admin_session, tenant, name="Report Cohort"):
    from app.models.cohort import Cohort

    cohort = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active")
    admin_session.add(cohort)
    admin_session.commit()
    return cohort

def _mk_events(admin_session, tenant, person, *, kind, n, occurred_at):
    from app.models.learning_event import LearningEvent

    admin_session.add_all(
        [
            LearningEvent(
                tenant_id=tenant.id,
                person_id=person.id,
                kind=kind,
                detail={},
                occurred_at=occurred_at,
            )
            for _ in range(n)
        ]
    )
    admin_session.commit()

def test_engagement_counts_roster_students_only(app_client, tenant_a, admin_session):
    from app.services.admin_reports import engagement_snapshot

    now = datetime.now(UTC)
    cohort = _mk_cohort(admin_session, tenant_a)
    busy = _mk_learner(admin_session, tenant_a, cohort, "busy@a.ex")
    stale = _mk_learner(admin_session, tenant_a, cohort, "stale@a.ex")
    _mk_learner(admin_session, tenant_a, cohort, "silent@a.ex")
    # An instructor with activity must not inflate the learner denominator.
    teacher = _mk_learner(admin_session, tenant_a, cohort, "teach2@a.ex", role="instructor")

    _mk_events(admin_session, tenant_a, busy, kind="chapter_viewed", n=2, occurred_at=now)
    _mk_events(admin_session, tenant_a, stale, kind="chapter_viewed", n=1, occurred_at=now - timedelta(days=10))
    _mk_events(admin_session, tenant_a, teacher, kind="chapter_viewed", n=5, occurred_at=now)

    eng = engagement_snapshot(admin_session, tenant_id=tenant_a.id, since=now - timedelta(hours=24))
    assert eng["enrollees"] == 3
    assert eng["ever_active"] == 2  # busy + stale
    assert eng["active_in_window"] == 1  # only busy
    assert eng["never_started"] == 1  # silent

def test_top_movers_ranked_with_submission_counts(app_client, tenant_a, admin_session):
    from app.services.admin_reports import top_movers

    now = datetime.now(UTC)
    cohort = _mk_cohort(admin_session, tenant_a, name="Movers")
    leader = _mk_learner(admin_session, tenant_a, cohort, "leader@a.ex", first="Lee", last="Der")
    runner = _mk_learner(admin_session, tenant_a, cohort, "runner@a.ex", first="Run", last="Ner")
    _mk_learner(admin_session, tenant_a, cohort, "absent@a.ex")

    _mk_events(admin_session, tenant_a, leader, kind="chapter_viewed", n=3, occurred_at=now)
    _mk_events(admin_session, tenant_a, leader, kind="submission_made", n=2, occurred_at=now)
    _mk_events(admin_session, tenant_a, runner, kind="chapter_viewed", n=1, occurred_at=now)
    # Outside the window: must not count towards the ranking.
    _mk_events(admin_session, tenant_a, runner, kind="chapter_viewed", n=9, occurred_at=now - timedelta(days=5))

    movers = top_movers(admin_session, tenant_id=tenant_a.id, since=now - timedelta(hours=24))
    assert [m["email"] for m in movers] == ["leader@a.ex", "runner@a.ex"]
    assert movers[0]["name"] == "Lee Der"
    assert movers[0]["events"] == 5
    assert movers[0]["submissions"] == 2
    assert movers[0]["cohort"] == "Movers"
    assert movers[1]["submissions"] == 0

def test_attention_list_reads_open_success_queue(app_client, tenant_a, admin_session):
    from app.models.success_queue import STATUS_RESOLVED, SuccessQueueEntry
    from app.services.admin_reports import attention_list

    now = datetime.now(UTC)
    cohort = _mk_cohort(admin_session, tenant_a, name="Attention")
    never = _mk_learner(admin_session, tenant_a, cohort, "never@a.ex", first="Nev", last="Er")
    quiet = _mk_learner(admin_session, tenant_a, cohort, "quiet@a.ex", first="Qui", last="Et")
    closed = _mk_learner(admin_session, tenant_a, cohort, "closed@a.ex")

    admin_session.add_all(
        [
            SuccessQueueEntry(
                tenant_id=tenant_a.id,
                person_id=never.id,
                cohort_id=cohort.id,
                reason_kind="inactivity",
                supporting_facts={"days_inactive": 30, "never_active": True},
                severity="high",
                detected_at=now,
            ),
            SuccessQueueEntry(
                tenant_id=tenant_a.id,
                person_id=quiet.id,
                cohort_id=cohort.id,
                reason_kind="below_passing",
                supporting_facts={},
                severity="medium",
                detected_at=now,
            ),
            SuccessQueueEntry(
                tenant_id=tenant_a.id,
                person_id=closed.id,
                cohort_id=cohort.id,
                reason_kind="inactivity",
                supporting_facts={"days_inactive": 9, "never_active": False},
                severity="medium",
                detected_at=now,
                status=STATUS_RESOLVED,
            ),
        ]
    )
    admin_session.commit()

    att = attention_list(admin_session, tenant_id=tenant_a.id)
    assert att["open_total"] == 2  # the resolved entry is not carried
    assert att["by_reason"] == {"inactivity": 1, "below_passing": 1}
    assert att["never_started"] == 1
    # Severity ordering is the queue's, not alphabetical: high before medium.
    assert att["top"][0]["email"] == "never@a.ex"
    assert att["top"][0]["reason"] == "inactive"
    assert att["top"][0]["days_inactive"] == 30
    assert att["top"][0]["never_active"] is True

def test_report_body_carries_learner_sections_and_escapes_names(app_client, tenant_a, admin_session):
    from app.models.success_queue import SuccessQueueEntry
    from app.services.admin_reports import _render, activity_snapshot

    now = datetime.now(UTC)
    cohort = _mk_cohort(admin_session, tenant_a, name="Bodies")
    mover = _mk_learner(admin_session, tenant_a, cohort, "mover@a.ex", first="<b>Evil", last="Name</b>")
    _mk_events(admin_session, tenant_a, mover, kind="chapter_viewed", n=2, occurred_at=now)
    admin_session.add(
        SuccessQueueEntry(
            tenant_id=tenant_a.id,
            person_id=mover.id,
            cohort_id=cohort.id,
            reason_kind="inactivity",
            supporting_facts={"days_inactive": 14, "never_active": False},
            severity="high",
            detected_at=now,
        )
    )
    admin_session.commit()

    snap = activity_snapshot(admin_session, tenant_id=tenant_a.id, since=now - timedelta(hours=24))
    html, text = _render(snap)

    assert "Top movers this window" in html and "Top movers this window" in text
    assert "Needs attention" in html and "Needs attention" in text
    assert "Never started" in text
    # Learner-supplied names are data, never markup.
    assert "<b>Evil" not in html
    assert "&lt;b&gt;Evil" in html
    assert "inactive 14d" in text

# Adapter boundary cases use detached public facts, with no database execution.
def test_engagement_adapter_preserves_scope_and_exact_container(monkeypatch):
    from dotmac_learning import EngagementSnapshot

    from app.services import admin_reports

    db, tenant_id, since = object(), uuid4(), datetime.now(UTC)
    calls = []

    def read(session, **scope):
        calls.append((session, scope))
        return EngagementSnapshot(4, 3, 2, 1)

    monkeypatch.setattr(admin_reports, "learning_engagement_snapshot", read)
    assert admin_reports.engagement_snapshot(db, tenant_id=tenant_id, since=since) == {
        "enrollees": 4,
        "ever_active": 3,
        "active_in_window": 2,
        "never_started": 1,
    }
    assert calls == [(db, {"tenant_id": tenant_id, "since": since})]

def test_top_movers_uses_configured_limit_and_preserves_owner_order(monkeypatch):
    from dotmac_learning import ReportPerson, TopMover

    from app.services import admin_reports

    db, tenant_id, since = object(), uuid4(), datetime.now(UTC)
    calls = []
    first = ReportPerson(uuid4(), tenant_id, "", "", "fallback@a.ex")
    second = ReportPerson(uuid4(), tenant_id, "Run", "Ner", "runner@a.ex")

    def configured(session, *, require_store):
        assert session is db and require_store is True
        return {"learning_top_movers_limit": 2}

    def read(session, **scope):
        calls.append((session, scope))
        return (TopMover(first, "Alpha", 9, 2, 0.8), TopMover(second, "", 4, 0, None))

    monkeypatch.setattr(admin_reports.settings_store, "effective", configured)
    monkeypatch.setattr(admin_reports, "learning_top_movers", read)
    assert admin_reports.top_movers(db, tenant_id=tenant_id, since=since) == [
        {
            "name": "fallback@a.ex",
            "email": "fallback@a.ex",
            "cohort": "Alpha",
            "events": 9,
            "submissions": 2,
            "avg_score": 0.8,
        },
        {"name": "Run Ner", "email": "runner@a.ex", "cohort": "", "events": 4, "submissions": 0, "avg_score": None},
    ]
    assert calls == [(db, {"tenant_id": tenant_id, "since": since, "limit": 2})]

def test_attention_adapter_keeps_labels_unknown_reasons_and_totals(monkeypatch):
    from dotmac_learning import AttentionLearner, AttentionReasonCount, AttentionSnapshot, ReportPerson

    from app.services import admin_reports

    db, tenant_id = object(), uuid4()
    person = ReportPerson(uuid4(), tenant_id, "Nev", "Er", "never@a.ex")
    calls = []

    def configured(session, *, require_store):
        assert session is db and require_store is True
        return {"learning_attention_limit": 2}

    def read(session, **scope):
        calls.append((session, scope))
        return AttentionSnapshot(
            7,
            (AttentionReasonCount("inactivity", 6), AttentionReasonCount("new_reason", 1)),
            3,
            (
                AttentionLearner(person, "Cohort", "inactivity", "high", 30, True),
                AttentionLearner(person, "", "new_reason", "medium", None, False),
            ),
        )

    monkeypatch.setattr(admin_reports.settings_store, "effective", configured)
    monkeypatch.setattr(admin_reports, "learning_attention_list", read)
    assert admin_reports.attention_list(db, tenant_id=tenant_id) == {
        "open_total": 7,
        "by_reason": {"inactivity": 6, "new_reason": 1},
        "never_started": 3,
        "top": [
            {
                "name": "Nev Er",
                "email": "never@a.ex",
                "cohort": "Cohort",
                "reason": "inactive",
                "severity": "high",
                "days_inactive": 30,
                "never_active": True,
            },
            {
                "name": "Nev Er",
                "email": "never@a.ex",
                "cohort": "",
                "reason": "new_reason",
                "severity": "medium",
                "days_inactive": None,
                "never_active": False,
            },
        ],
    }
    assert calls == [(db, {"tenant_id": tenant_id, "limit": 2})]

@pytest.mark.parametrize("name", ["top_movers", "attention_list"])
@pytest.mark.parametrize("limit", [0, 3])
def test_explicit_report_limits_bypass_default_store(monkeypatch, name, limit):
    from dotmac_learning import AttentionSnapshot

    from app.services import admin_reports

    db, tenant_id, since = object(), uuid4(), datetime.now(UTC)
    calls = []

    def unexpected_store(*args, **kwargs):
        raise AssertionError("explicit limits must not read default policy")

    def read(session, **scope):
        calls.append((session, scope))
        return () if name == "top_movers" else AttentionSnapshot(5, (), 0, ())

    monkeypatch.setattr(admin_reports.settings_store, "effective", unexpected_store)
    monkeypatch.setattr(admin_reports, f"learning_{name}", read)
    scope = {"tenant_id": tenant_id, "limit": limit}
    if name == "top_movers":
        scope["since"] = since
    result = getattr(admin_reports, name)(db, **scope)
    assert calls == [(db, scope)]
    assert result == (
        []
        if name == "top_movers"
        else {
            "open_total": 5,
            "by_reason": {},
            "never_started": 0,
            "top": [],
        }
    )

@pytest.mark.parametrize("name", ["top_movers", "attention_list"])
@pytest.mark.parametrize("failure", ["missing", "unavailable"])
def test_omitted_report_limits_refuse_policy_failure_before_owner_read(monkeypatch, name, failure):
    from app.services import admin_reports

    db, tenant_id = object(), uuid4()

    def configured(session, *, require_store):
        assert session is db and require_store is True
        if failure == "unavailable":
            raise RuntimeError("policy store unavailable")
        return {}

    def unexpected_read(*args, **kwargs):
        raise AssertionError("owner must not run without required policy")

    monkeypatch.setattr(admin_reports.settings_store, "effective", configured)
    monkeypatch.setattr(admin_reports, f"learning_{name}", unexpected_read)
    scope = {"tenant_id": tenant_id}
    if name == "top_movers":
        scope["since"] = datetime.now(UTC)
    with pytest.raises(RuntimeError if failure == "unavailable" else KeyError):
        getattr(admin_reports, name)(db, **scope)

def test_activity_snapshot_delegates_learning_counts_with_same_scope(monkeypatch):
    from dotmac_learning import ActivityWindowCounts

    from app.services import admin_reports

    class AdmissionReadSession:
        def scalar(self, statement):
            return None

        def execute(self, statement):
            return self

        def all(self):
            return []

    db, tenant_id, since = AdmissionReadSession(), uuid4(), datetime.now(UTC)
    calls = []
    engagement, movers, attention = {"kept": "engagement"}, [{"kept": "movers"}], {"kept": "attention"}

    def read(session, **scope):
        calls.append((session, scope))
        return ActivityWindowCounts(since, 11, 12, 13)

    def section(value):
        def project(session, **scope):
            assert session is db and scope["tenant_id"] == tenant_id
            return value

        return project

    monkeypatch.setattr(admin_reports, "activity_window_counts", read)
    monkeypatch.setattr(admin_reports, "engagement_snapshot", section(engagement))
    monkeypatch.setattr(admin_reports, "top_movers", section(movers))
    monkeypatch.setattr(admin_reports, "attention_list", section(attention))
    snap = admin_reports.activity_snapshot(db, tenant_id=tenant_id, since=since)
    assert calls == [(db, {"tenant_id": tenant_id, "since": since})]
    assert snap["new_enrollments"] == 11
    assert snap["submissions"] == 12
    assert snap["completions"] == 13
    assert snap["since"] is since
    assert snap["engagement"] is engagement
    assert snap["top_movers"] is movers
    assert snap["attention"] is attention
    assert snap["pipeline"] == {}

@pytest.mark.parametrize("hours", [None, 0, -3])
def test_send_report_resolves_only_omitted_hours_and_preserves_outbox_boundary(monkeypatch, hours):
    from types import SimpleNamespace

    from app.services import admin_reports

    db, tenant_id, admin_id = object(), uuid4(), uuid4()
    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    snapshots, queued, store_calls = [], [], []

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return now

    def configured(session, *, require_store):
        assert session is db and require_store is True
        store_calls.append(session)
        return {"learning_activity_report_hours": 6}

    def snapshot(session, **scope):
        snapshots.append((session, scope))
        return {"sentinel": True}

    def recipients(session, *, tenant_id):
        assert session is db
        return [SimpleNamespace(id=admin_id, email="admin@a.ex")]

    def queue(session, **message):
        queued.append((session, message))
        return True

    def render(snap):
        assert snap == {"sentinel": True}
        return "<p>report</p>", "report"

    monkeypatch.setattr(admin_reports, "datetime", FixedTime)
    monkeypatch.setattr(admin_reports.settings_store, "effective", configured)
    monkeypatch.setattr(admin_reports, "activity_snapshot", snapshot)
    monkeypatch.setattr(admin_reports, "admin_recipients", recipients)
    monkeypatch.setattr(admin_reports, "enqueue_email", queue)
    monkeypatch.setattr(admin_reports, "_render", render)
    assert admin_reports.send_activity_report(db, tenant_id=tenant_id, hours=hours) == 1
    effective_hours = 6 if hours is None else hours
    assert store_calls == ([db] if hours is None else [])
    assert snapshots == [(db, {"tenant_id": tenant_id, "since": now - timedelta(hours=effective_hours)})]
    assert queued == [
        (
            db,
            {
                "tenant_id": tenant_id,
                "idempotency_key": f"admin-report:{admin_id}:2026-10-18:{effective_hours}",
                "kind": "admin_report",
                "recipient": "admin@a.ex",
                "subject": "Academy activity report — 18 Oct 2026",
                "html_body": "<p>report</p>",
                "text_body": "report",
            },
        )
    ]

def test_send_report_refuses_policy_failure_before_snapshot_or_delivery(monkeypatch):
    from app.services import admin_reports

    def unavailable(*args, **kwargs):
        raise RuntimeError("policy store unavailable")

    def unexpected(*args, **kwargs):
        raise AssertionError("snapshot or delivery must not run without hours policy")

    monkeypatch.setattr(admin_reports.settings_store, "effective", unavailable)
    monkeypatch.setattr(admin_reports, "activity_snapshot", unexpected)
    monkeypatch.setattr(admin_reports, "enqueue_email", unexpected)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        admin_reports.send_activity_report(object(), tenant_id=uuid4())

@pytest.mark.parametrize(
    "name,key",
    [
        ("top_movers", "learning_top_movers_limit"),
        ("attention_list", "learning_attention_limit"),
    ],
)
def test_configured_zero_report_limit_is_forwarded_exactly(monkeypatch, name, key):
    from dotmac_learning import AttentionSnapshot

    from app.services import admin_reports

    db, tenant_id = object(), uuid4()
    calls = []

    def configured(session, *, require_store):
        assert session is db and require_store is True
        return {key: 0}

    def read(session, **scope):
        assert session is db and scope["tenant_id"] == tenant_id
        calls.append(scope["limit"])
        return () if name == "top_movers" else AttentionSnapshot(7, (), 1, ())

    monkeypatch.setattr(admin_reports.settings_store, "effective", configured)
    monkeypatch.setattr(admin_reports, f"learning_{name}", read)
    scope = {"tenant_id": tenant_id}
    if name == "top_movers":
        scope["since"] = datetime.now(UTC)
    result = getattr(admin_reports, name)(db, **scope)
    assert calls == [0]
    assert result == (
        []
        if name == "top_movers"
        else {
            "open_total": 7,
            "by_reason": {},
            "never_started": 1,
            "top": [],
        }
    )
