"""Service-level tests for app/services/announcements.py."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel import audit_actions, source_applications
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson
from sqlalchemy import select, text

from app.models.identity_extensions import AcademyAccountProfile
from app.models.notification import Notification
from app.services import announcements as ann_svc


def _set_tenant(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})


def _make_person(db, tenant, suffix=""):
    pid = uuid4()
    person = Party(
        id=pid,
        tenant_id=tenant.id,
        party_type="person",
        email=f"{pid}{suffix}@ann.test",
        display_name="Ann Test",
        person_profile=PartyPerson(first_name="Ann", last_name="Test"),
    )
    db.add(person)
    db.flush()
    db.add(AcademyAccountProfile(tenant_id=tenant.id, party_id=person.id, status="active", prefs={}, created_at=datetime.now(UTC), updated_at=datetime.now(UTC)))
    db.flush()
    return person


def _make_cohort(db, tenant, name="Cohort"):
    learning.create_cohort(
        db,
        learning.CreateCohortCommand(tenant_id=tenant.id, name=name, discipline="net"),
    )
    db.flush()
    cohort_id = db.execute(
        text(
            "SELECT id FROM mod_learning.cohorts "
            "WHERE tenant_id = :tenant_id AND name = :name "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"tenant_id": tenant.id, "name": name},
    ).scalar_one()
    return SimpleNamespace(id=cohort_id)


def _enroll(db, tenant, person, cohort):
    result = learning.bulk_enroll(
        db,
        learning.BulkEnrollCommand(
            tenant_id=tenant.id, cohort_id=cohort.id, emails=(person.email,), pacing_default_open=True
        ),
        authority=learning.RosterAuthority(tenant_id=tenant.id, actor_is_admin=True),
        initial_state=learning.BulkEnrollmentInitialState(role_in_cohort="student", status="active"),
        accounts=learning.AccountPresenceSelection(
            tenant_id=tenant.id,
            person_ids=select(Party.id).join(PartyPerson, PartyPerson.party_id == Party.id).join(
                AcademyAccountProfile,
                (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
            ).where(Party.tenant_id == tenant.id, Party.party_type == "person"),
        ),
    )
    assert result.enrolled == (person.email,)


@pytest.fixture(autouse=True)
def installed_announcement_attribution(monkeypatch):
    """Install the real Learning vocabulary and host; restore process state afterward."""
    # Monkeypatch records the prior process globals for teardown; installation
    # itself goes through the public Kernel APIs exercised by the command.
    monkeypatch.setattr(audit_actions, "_active_registry", audit_actions._active_registry)
    monkeypatch.setattr(source_applications, "_active_registry", source_applications._active_registry)
    monkeypatch.setattr(source_applications, "_host_application", source_applications._host_application)
    audit_actions.install_audit_actions(audit_actions.AuditActionRegistry.from_manifests([learning.module]))
    source_applications.install_source_applications(source_applications.SourceApplicationRegistry(["academy"]))
    source_applications.install_host_application("academy")


@pytest.fixture
def db_session(admin_session):
    return admin_session


def test_create_returns_announcement(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)

    ann = ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Hello World",
        body_md="**Bold** text",
    )
    db_session.flush()

    assert ann.id is not None
    assert ann.tenant_id == tenant_a.id
    assert ann.title == "Hello World"
    assert "<strong>Bold</strong>" in ann.body_html
    assert ann.cohort_id is None


def test_create_tenant_wide_emits_notifications_to_enrolled(db_session, tenant_a):
    """Tenant-wide announcement notifies all actively-enrolled persons."""
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    cohort = _make_cohort(db_session, tenant_a)
    student1 = _make_person(db_session, tenant_a)
    student2 = _make_person(db_session, tenant_a)
    unenrolled = _make_person(db_session, tenant_a)

    _enroll(db_session, tenant_a, student1, cohort)
    _enroll(db_session, tenant_a, student2, cohort)
    # unenrolled gets no notification

    ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Tenant-wide notice",
        body_md="For everyone",
    )
    db_session.flush()

    notifs = db_session.scalars(
        select(Notification)
        .where(Notification.tenant_id == tenant_a.id)
        .where(Notification.kind == "announcement")
        .where(Notification.title == "Tenant-wide notice")
    ).all()
    notified_ids = {n.person_id for n in notifs}
    assert student1.id in notified_ids
    assert student2.id in notified_ids
    assert unenrolled.id not in notified_ids


def test_create_cohort_targeted_emits_notifications_only_to_that_cohort(db_session, tenant_a):
    """Cohort-targeted announcement notifies only that cohort's active students."""
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    cohort_a = _make_cohort(db_session, tenant_a, "Alpha")
    cohort_b = _make_cohort(db_session, tenant_a, "Beta")
    student_a = _make_person(db_session, tenant_a)
    student_b = _make_person(db_session, tenant_a)

    _enroll(db_session, tenant_a, student_a, cohort_a)
    _enroll(db_session, tenant_a, student_b, cohort_b)

    ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Cohort A only",
        body_md="For cohort A",
        cohort_id=cohort_a.id,
    )
    db_session.flush()

    notifs = db_session.scalars(
        select(Notification)
        .where(Notification.tenant_id == tenant_a.id)
        .where(Notification.kind == "announcement")
        .where(Notification.title == "Cohort A only")
    ).all()
    notified_ids = {n.person_id for n in notifs}
    assert student_a.id in notified_ids
    assert student_b.id not in notified_ids


def test_create_writes_audit_event(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)

    ann = ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Audit check",
        body_md="body",
    )
    db_session.flush()

    events = db_session.scalars(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_a.id)
        .where(AuditEvent.action == "announcement.created")
        .where(AuditEvent.entity_id == str(ann.id))
    ).all()
    assert len(events) == 1
    assert events[0].actor_party_id == author.id
    assert events[0].actor_type == "user"
    assert events[0].actor_id == str(author.id)
    assert events[0].source_application == "academy"


def test_for_person_tenant_wide_visible_to_all_enrolled(db_session, tenant_a):
    """Tenant-wide announcement is returned for any enrolled person."""
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    cohort = _make_cohort(db_session, tenant_a)
    student = _make_person(db_session, tenant_a)
    _enroll(db_session, tenant_a, student, cohort)

    ann = ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Global message",
        body_md="For all",
    )
    db_session.flush()

    items = ann_svc.for_person(db_session, tenant_id=tenant_a.id, person_id=student.id)
    assert any(a.id == ann.id for a in items)


def test_for_person_tenant_wide_visible_without_enrollment(db_session, tenant_a):
    """Tenant-wide announcement is returned even with no enrollments."""
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    unenrolled = _make_person(db_session, tenant_a)

    ann = ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Open message",
        body_md="Anyone can see",
    )
    db_session.flush()

    items = ann_svc.for_person(db_session, tenant_id=tenant_a.id, person_id=unenrolled.id)
    assert any(a.id == ann.id for a in items)


def test_for_person_cohort_targeted_visible_only_to_that_cohort_member(db_session, tenant_a):
    """Cohort-targeted announcement is visible to enrolled member but NOT to other cohort member."""
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    cohort_a = _make_cohort(db_session, tenant_a, "A")
    cohort_b = _make_cohort(db_session, tenant_a, "B")
    student_a = _make_person(db_session, tenant_a)
    student_b = _make_person(db_session, tenant_a)

    _enroll(db_session, tenant_a, student_a, cohort_a)
    _enroll(db_session, tenant_a, student_b, cohort_b)

    ann = ann_svc.create(
        db_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Cohort A only",
        body_md="For A",
        cohort_id=cohort_a.id,
    )
    db_session.flush()

    # student_a (in cohort_a) should see it
    items_a = ann_svc.for_person(db_session, tenant_id=tenant_a.id, person_id=student_a.id)
    assert any(a.id == ann.id for a in items_a)

    # student_b (in cohort_b) should NOT see it
    items_b = ann_svc.for_person(db_session, tenant_id=tenant_a.id, person_id=student_b.id)
    assert not any(a.id == ann.id for a in items_b)


def test_for_person_newest_first(db_session, tenant_a):
    """for_person returns announcements newest first."""
    from datetime import UTC, datetime

    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)
    cohort = _make_cohort(db_session, tenant_a)
    student = _make_person(db_session, tenant_a)
    _enroll(db_session, tenant_a, student, cohort)

    ann1 = ann_svc.create(db_session, tenant_id=tenant_a.id, author_person_id=author.id, title="First", body_md="")
    db_session.execute(
        text(
            "UPDATE mod_learning.announcements SET created_at = :created_at WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"created_at": datetime(2024, 1, 1, tzinfo=UTC), "tenant_id": tenant_a.id, "id": ann1.id},
    )
    ann2 = ann_svc.create(db_session, tenant_id=tenant_a.id, author_person_id=author.id, title="Second", body_md="")
    db_session.execute(
        text(
            "UPDATE mod_learning.announcements SET created_at = :created_at WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"created_at": datetime(2024, 1, 2, tzinfo=UTC), "tenant_id": tenant_a.id, "id": ann2.id},
    )
    db_session.flush()

    items = ann_svc.for_person(db_session, tenant_id=tenant_a.id, person_id=student.id)
    titles = [a.title for a in items]
    assert titles.index("Second") < titles.index("First")


def test_list_for_tenant(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)

    ann_svc.create(db_session, tenant_id=tenant_a.id, author_person_id=author.id, title="T1", body_md="")
    ann_svc.create(db_session, tenant_id=tenant_a.id, author_person_id=author.id, title="T2", body_md="")
    db_session.flush()

    items = ann_svc.list_for_tenant(db_session, tenant_id=tenant_a.id)
    titles = [a.title for a in items]
    assert "T1" in titles
    assert "T2" in titles


def test_delete_removes_announcement(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    author = _make_person(db_session, tenant_a)

    ann = ann_svc.create(db_session, tenant_id=tenant_a.id, author_person_id=author.id, title="To delete", body_md="")
    db_session.flush()

    ann_svc.delete(db_session, tenant_id=tenant_a.id, announcement_id=ann.id)
    db_session.flush()

    items = ann_svc.list_for_tenant(db_session, tenant_id=tenant_a.id)
    assert not any(a.id == ann.id for a in items)


def test_delete_nonexistent_is_noop(db_session, tenant_a):
    """Deleting a nonexistent announcement does not raise."""
    _set_tenant(db_session, tenant_a.id)
    ann_svc.delete(db_session, tenant_id=tenant_a.id, announcement_id=uuid4())
