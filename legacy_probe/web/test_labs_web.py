"""Student lab portal: web routes enqueue; the lab worker owns execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType, UserCredential
from dotmac_learning import (
    ActivityWrite,
    CourseWrite,
    CreateCohortCommand,
    create_activity,
    create_cohort,
    create_course,
)
from dotmac_learning.models import Score, Submission
from dotmac_learning.participation_models import Cohort, CourseOffering, Enrollment

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.services import settings_store
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, web_login


def _make_party(admin_session, tenant, email: str, *, first_name="S", last_name="L") -> Party:
    party = Party(
        tenant_id=tenant.id, party_type=PartyType.person,
        display_name=f"{first_name} {last_name}", email=email, is_active=True,
    )
    admin_session.add(party)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    admin_session.flush()
    return party


def _make_person(admin_session, tenant, email: str) -> Party:
    # Genuine identity and credential rows, as in the canonical sign-in fixture.
    now = datetime.now(UTC)
    p = _make_party(admin_session, tenant, email)
    admin_session.add(AcademyAccountProfile(
        tenant_id=tenant.id, party_id=p.id, status="active", prefs={},
        created_at=now, updated_at=now,
    ))
    credential = UserCredential(
        tenant_id=tenant.id, party_id=p.id, password_hash=hash_password("password1"),
    )
    admin_session.add(credential)
    admin_session.flush()
    admin_session.add(AcademyCredentialPolicy(
        tenant_id=tenant.id, credential_id=credential.id, email=email,
        failed_login_attempts=0, locked_until=None, lifecycle_version=0,
        reset_required=False, created_at=now, updated_at=now,
    ))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    admin_session.commit()
    return p


def _login(app_client, email: str) -> dict[str, str]:
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, "password1")
    return h


def _entitle(admin_session, tenant, person, course):
    """Give the person access to the course via cohort + enrollment + offering."""
    create_cohort(admin_session, CreateCohortCommand(
        tenant_id=tenant.id, name="Lab Cohort", discipline=course.discipline,
    ))
    # create_cohort stages one public owner row without flushing or returning it.
    coh = next(row for row in admin_session.new if isinstance(row, Cohort))
    admin_session.flush()
    # Preserve the exact historical active entitlement fixture. Admission and
    # invitation commands would add unrelated policy/assignment behavior.
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=course.id,
                                     status="active"))
    admin_session.commit()
    return coh


def _seed_lab(admin_session, tenant, *, with_course=True):
    course = create_course(admin_session, CourseWrite(
        id=uuid4(), status="published", listed=False,
        tenant_id=tenant.id,
        slug="foundation",
        title="F",
        discipline="networking",
        source_ref="x",
        version=1,
    ))
    act = create_activity(admin_session, ActivityWrite(
        id=uuid4(), assessment_mode="graded", grading="auto", weight=1.0,
        tenant_id=tenant.id,
        course_id=course.id,
        chapter_number=14,
        type="lab",
        title="VLAN Lab",
        pass_threshold=0.5,
    ))
    tpl = LabTemplate(
        tenant_id=tenant.id,
        course_id=course.id,
        chapter_number=14,
        activity_id=act.id,
        slug="vlan",
        title="VLAN Lab",
        topology="name: x",
        instructions_html="<p>Configure the router.</p>",
        checks=[
            {
                "id": "c1",
                "type": "command",
                "node": "r1",
                "command": "true",
                "assert": {"exit_code": 0},
            }
        ],
        seed_spec={},
        limits={},
        source_hash="abc",
        version=1,
    )
    admin_session.add(tpl)
    admin_session.commit()
    return course, act, tpl


def _seed_seeded_lab(admin_session, tenant):
    """A lab template whose instructions carry a `{{lan_octet}}` placeholder,
    for the interpolation/placeholder web tests."""
    course = create_course(admin_session, CourseWrite(
        id=uuid4(), status="published", listed=False,
        tenant_id=tenant.id, slug="foundation-seeded", title="F", discipline="networking",
        source_ref="x", version=1,
    ))
    act = create_activity(admin_session, ActivityWrite(
        id=uuid4(), assessment_mode="graded", grading="auto", weight=1.0,
        tenant_id=tenant.id, course_id=course.id, chapter_number=14, type="lab",
        title="Seeded Lab", pass_threshold=0.5,
    ))
    tpl = LabTemplate(
        tenant_id=tenant.id, course_id=course.id, chapter_number=14, activity_id=act.id,
        slug="seeded", title="Seeded Lab", topology="name: x",
        instructions_html="<p>Configure the client at 10.0.{{lan_octet}}.10.</p>",
        instructions_md="Configure the client at 10.0.{{lan_octet}}.10.",
        checks=[], seed_spec={"lan_octet": {"type": "int", "min": 2, "max": 9}},
        limits={}, source_hash="abc", version=1,
    )
    admin_session.add(tpl)
    admin_session.commit()
    return course, act, tpl


def _csrf(app_client, path, h):
    r = app_client.get(path, headers=h)
    return r, app_client.cookies.get("csrf_token") or r.cookies.get("csrf_token", "")


def test_dropped_owner_loses_live_instance_access(app_client, admin_session, tenant_a, monkeypatch):
    """Revocation propagates to live instances: a dropped owner is 403 on status/check."""
    p = _make_person(admin_session, tenant_a, "drop@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    coh = _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "drop@a.edu")
    # Launch while entitled.
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    app_client.post(f"/labs/{act.id}/launch", headers={**h, "x-csrf-token": csrf})
    inst = admin_session.query(LabInstance).filter(
        LabInstance.activity_id == act.id, LabInstance.person_id == p.id).one()

    # Instructor drops the learner; the live instance must now be off-limits.
    enr = admin_session.query(Enrollment).filter(
        Enrollment.cohort_id == coh.id, Enrollment.person_id == p.id).one()
    enr.status = "dropped"
    admin_session.commit()

    assert app_client.get(f"/labs/instances/{inst.id}/status", headers=h).status_code == 403
    r = app_client.post(f"/labs/instances/{inst.id}/check", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 403


def test_unentitled_student_forbidden_on_lab(app_client, admin_session, tenant_a, monkeypatch):
    """Slice 1: no offering for the lab's course → 403 on detail and launch, no instance."""
    _make_person(admin_session, tenant_a, "nolab@a.edu")
    _, act, _ = _seed_lab(admin_session, tenant_a)  # no enrollment/offering
    h = _login(app_client, "nolab@a.edu")
    assert app_client.get(f"/labs/{act.id}", headers=h).status_code == 403
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(f"/labs/{act.id}/launch", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 403
    n = admin_session.query(LabInstance).filter(LabInstance.activity_id == act.id).count()
    assert n == 0


def test_launch_creates_instance_and_returns_status(
    app_client, admin_session, tenant_a, monkeypatch
):
    p = _make_person(admin_session, tenant_a, "owner@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "owner@a.edu")

    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    r = app_client.post(
        f"/labs/{act.id}/launch", headers={**h, "x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert "Status" in r.text

    rows = (
        admin_session.query(LabInstance)
        .filter(LabInstance.activity_id == act.id, LabInstance.person_id == p.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status in ("queued", "provisioning")


def test_duplicate_launch_reuses_the_current_instance(
    app_client, admin_session, tenant_a
):
    person = _make_person(admin_session, tenant_a, "duplicate-launch@a.edu")
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    headers = _login(app_client, person.email)
    _, csrf = _csrf(app_client, f"/labs/{activity.id}", headers)

    first = app_client.post(
        f"/labs/{activity.id}/launch",
        headers={**headers, "x-csrf-token": csrf},
    )
    second = app_client.post(
        f"/labs/{activity.id}/launch",
        headers={**headers, "x-csrf-token": csrf},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    instances = admin_session.query(LabInstance).filter_by(
        tenant_id=tenant_a.id,
        person_id=person.id,
        activity_id=activity.id,
    ).all()
    assert len(instances) == 1
    assert (
        admin_session.query(LabOperation)
        .filter_by(instance_id=instances[0].id)
        .count()
        == 1
    )


def test_check_enqueues_worker_operation_without_writing_score(
    app_client, admin_session, tenant_a, monkeypatch
):
    p = _make_person(admin_session, tenant_a, "owner2@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "owner2@a.edu")

    # Launch, then simulate the worker bringing the instance up (active + consoles).
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    app_client.post(f"/labs/{act.id}/launch", headers={**h, "x-csrf-token": csrf})
    inst = (
        admin_session.query(LabInstance)
        .filter(LabInstance.activity_id == act.id, LabInstance.person_id == p.id)
        .one()
    )
    inst.status = "active"
    inst.consoles = {"r1": {"kind": "linux", "mgmt": "172.20.20.3"}}
    launch_operation = admin_session.query(LabOperation).filter_by(instance_id=inst.id).one()
    launch_operation.state = "succeeded"
    launch_operation.finished_at = launch_operation.requested_at
    admin_session.commit()

    r = app_client.post(
        f"/labs/instances/{inst.id}/check", headers={**h, "x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert "Waiting to run checks" in r.text
    operation = (
        admin_session.query(LabOperation)
        .filter_by(instance_id=inst.id, state="queued")
        .one()
    )
    assert operation.kind == "check"
    assert operation.state == "queued"


def test_check_refuses_degraded_active_instance(
    app_client, admin_session, tenant_a, monkeypatch
):
    person = _make_person(admin_session, tenant_a, "degraded-check@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=person.id,
        instance_name="dal-degraded-check",
        seed={},
        status="active",
        error="destroy failed; external resources may still exist",
        consoles={"r1": {"kind": "linux", "mgmt": "172.20.20.3"}},
    )
    admin_session.add(instance)
    admin_session.commit()

    headers = _login(app_client, "degraded-check@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act.id}", headers)
    response = app_client.post(
        f"/labs/instances/{instance.id}/check",
        headers={**headers, "x-csrf-token": csrf},
    )

    assert response.status_code == 409
    assert (
        admin_session.query(LabOperation)
        .filter_by(instance_id=instance.id)
        .count()
        == 0
    )

    status_response = app_client.get(
        f"/labs/instances/{instance.id}/status", headers=headers
    )
    assert status_response.status_code == 200
    assert "unresolved infrastructure error" in status_response.text
    assert "Run checks" not in status_response.text
    assert "Reset lab" in status_response.text


def test_cross_person_check_forbidden(app_client, admin_session, tenant_a, monkeypatch):
    owner = _make_person(admin_session, tenant_a, "owner3@a.edu")
    _make_person(admin_session, tenant_a, "intruder@a.edu")
    _, act, _ = _seed_lab(admin_session, tenant_a)
    inst = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=act.id,
        person_id=owner.id,
        instance_name="dal-x",
        seed={},
        status="active",
        consoles={"r1": {"kind": "linux", "mgmt": "172.20.20.3"}},
    )
    admin_session.add(inst)
    admin_session.commit()
    admin_session.refresh(inst)

    h = _login(app_client, "intruder@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    r = app_client.post(
        f"/labs/instances/{inst.id}/check", headers={**h, "x-csrf-token": csrf}
    )
    assert r.status_code == 403


def test_cross_tenant_check_not_found(
    app_client, admin_session, tenant_a, tenant_b, monkeypatch
):
    _make_person(admin_session, tenant_a, "owner4@a.edu")
    p_b = _make_party(admin_session, tenant_b, email="b@b.edu", first_name="B", last_name="B")
    admin_session.add(p_b)
    admin_session.flush()
    _, act_b, _ = _seed_lab(admin_session, tenant_b)
    inst = LabInstance(
        tenant_id=tenant_b.id,
        activity_id=act_b.id,
        person_id=p_b.id,
        instance_name="dal-y",
        seed={},
        status="active",
        consoles={"r1": {"kind": "linux", "mgmt": "172.20.20.3"}},
    )
    admin_session.add(inst)
    admin_session.commit()
    admin_session.refresh(inst)

    h = _login(app_client, "owner4@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act_b.id}", h)
    # tenant_a user hitting tenant_b's instance route → 404.
    r = app_client.post(
        f"/labs/instances/{inst.id}/check", headers={**h, "x-csrf-token": csrf}
    )
    assert r.status_code == 404


def test_detail_shows_neutral_placeholder_before_launch(app_client, admin_session, tenant_a, monkeypatch):
    """No instance yet → a neutral placeholder, never raw/unfilled instructions."""
    p = _make_person(admin_session, tenant_a, "prelaunch@a.edu")
    course, act, _tpl = _seed_seeded_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "prelaunch@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "{{lan_octet}}" not in r.text
    assert "Launch the lab" in r.text


def test_detail_after_launch_interpolates_this_instances_seed(app_client, admin_session, tenant_a, monkeypatch):
    """Once an instance exists, GET renders instructions substituted with THAT
    instance's seed — no literal placeholder left in the page."""
    p = _make_person(admin_session, tenant_a, "postlaunch@a.edu")
    course, act, _tpl = _seed_seeded_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "postlaunch@a.edu")

    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    launch_resp = app_client.post(f"/labs/{act.id}/launch", headers={**h, "x-csrf-token": csrf})
    assert launch_resp.headers.get("HX-Refresh") == "true"

    inst = (
        admin_session.query(LabInstance)
        .filter(LabInstance.activity_id == act.id, LabInstance.person_id == p.id)
        .one()
    )
    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "{{lan_octet}}" not in r.text
    assert f"10.0.{inst.seed['lan_octet']}.10" in r.text


def test_two_learners_same_template_get_different_rendered_instructions(
    app_client, admin_session, tenant_a, monkeypatch
):
    p1 = _make_person(admin_session, tenant_a, "learner1@a.edu")
    p2 = _make_person(admin_session, tenant_a, "learner2@a.edu")
    course, act, tpl = _seed_seeded_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p1, course)
    _entitle(admin_session, tenant_a, p2, course)

    inst1 = LabInstance(tenant_id=tenant_a.id, activity_id=act.id, person_id=p1.id,
                        instance_name="dal-l1", seed={"lan_octet": 2}, status="active",
                        consoles={})
    inst2 = LabInstance(tenant_id=tenant_a.id, activity_id=act.id, person_id=p2.id,
                        instance_name="dal-l2", seed={"lan_octet": 8}, status="active",
                        consoles={})
    admin_session.add(inst1)
    admin_session.add(inst2)
    admin_session.commit()

    # A shared app_client requires clearing cookies before switching who's
    # logged in (established pattern — see tests/web/test_entitlements.py);
    # each GET happens right after that person's login, before switching to
    # the next, so a stale session cookie can't make one request masquerade
    # as the other person.
    app_client.cookies.clear()
    h1 = _login(app_client, "learner1@a.edu")
    r1 = app_client.get(f"/labs/{act.id}", headers=h1)
    app_client.cookies.clear()
    h2 = _login(app_client, "learner2@a.edu")
    r2 = app_client.get(f"/labs/{act.id}", headers=h2)
    assert "10.0.2.10" in r1.text
    assert "10.0.8.10" in r2.text
    assert r1.text != r2.text


def test_error_retry_enqueues_deploy_and_leaves_admission_to_worker(
    app_client, admin_session, tenant_a, monkeypatch
):
    p = _make_person(admin_session, tenant_a, "reset-ok@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    inst = LabInstance(
        tenant_id=tenant_a.id, activity_id=act.id, person_id=p.id,
        instance_name="dal-reset-ok", seed={}, status="error", error="stale",
        consoles={"r1": {"kind": "linux", "mgmt": "172.20.20.3"}},
    )
    admin_session.add(inst)
    admin_session.commit()
    admin_session.refresh(inst)

    h = _login(app_client, "reset-ok@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    r = app_client.post(f"/labs/instances/{inst.id}/reset", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 200
    admin_session.refresh(inst)
    assert inst.status == "error"
    operation = admin_session.query(LabOperation).filter_by(instance_id=inst.id).one()
    assert operation.kind == "deploy"
    assert operation.state == "queued"


def test_duplicate_reset_returns_the_existing_open_operation(
    app_client, admin_session, tenant_a, monkeypatch
):
    """Repeated clicks collapse at the database boundary and remain HTTP 200."""
    p = _make_person(admin_session, tenant_a, "reset-fail@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    inst = LabInstance(
        tenant_id=tenant_a.id, activity_id=act.id, person_id=p.id,
        instance_name="dal-reset-fail", seed={}, status="active",
        consoles={"r1": {"kind": "linux", "mgmt": "172.20.20.3"}},
    )
    admin_session.add(inst)
    admin_session.commit()
    admin_session.refresh(inst)

    h = _login(app_client, "reset-fail@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    first = app_client.post(f"/labs/instances/{inst.id}/reset", headers={**h, "x-csrf-token": csrf})
    second = app_client.post(f"/labs/instances/{inst.id}/reset", headers={**h, "x-csrf-token": csrf})
    assert first.status_code == 200
    assert second.status_code == 200
    assert admin_session.query(LabOperation).filter_by(instance_id=inst.id).count() == 1


def test_reset_on_reaped_instance_is_refused(app_client, admin_session, tenant_a, monkeypatch):
    """A destroyed (reaped) instance must not be resurrected by reset()."""
    p = _make_person(admin_session, tenant_a, "reset-reaped@a.edu")
    course, act, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    inst = LabInstance(
        tenant_id=tenant_a.id, activity_id=act.id, person_id=p.id,
        instance_name="dal-reaped", seed={}, status="reaped", consoles={},
    )
    admin_session.add(inst)
    admin_session.commit()
    admin_session.refresh(inst)

    h = _login(app_client, "reset-reaped@a.edu")
    _, csrf = _csrf(app_client, f"/labs/{act.id}", h)
    r = app_client.post(f"/labs/instances/{inst.id}/reset", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 409
    admin_session.refresh(inst)
    assert inst.status == "reaped"


def test_reset_refuses_when_a_check_operation_is_already_open(
    app_client, admin_session, tenant_a
):
    person = _make_person(admin_session, tenant_a, "reset-during-check@a.edu")
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name="dal-reset-during-check",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    operation = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="check",
        requested_by=person.id,
    )
    admin_session.add(operation)
    admin_session.commit()

    headers = _login(app_client, person.email)
    _, csrf = _csrf(app_client, f"/labs/{activity.id}", headers)
    response = app_client.post(
        f"/labs/instances/{instance.id}/reset",
        headers={**headers, "x-csrf-token": csrf},
    )

    assert response.status_code == 409
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 1
    assert operation.kind == "check"


@pytest.mark.parametrize(
    ("status", "error", "operation_state"),
    [
        ("resetting", None, "claimed"),
        ("provisioning", "prior deploy failed", "claimed"),
        ("queued", "capacity deferred", "queued"),
    ],
)
def test_reset_retry_reuses_an_open_operation_after_reservation_or_deferral(
    app_client, admin_session, tenant_a, status, error, operation_state
):
    person = _make_person(
        admin_session, tenant_a, f"retry-claimed-{status}@a.edu"
    )
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name=f"dal-retry-claimed-{status}",
        seed={},
        status=status,
        consoles={},
        error=error,
    )
    admin_session.add(instance)
    admin_session.flush()
    operation = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="deploy",
        state=operation_state,
        requested_by=person.id,
        claimed_by="worker" if operation_state == "claimed" else None,
        # ck_lab_operations_claim_lease_required: a claimed row carries its lease.
        claim_lease_seconds=(
            settings_store.execution_policy(admin_session).lease_seconds("deploy")
            if operation_state == "claimed"
            else None
        ),
    )
    admin_session.add(operation)
    admin_session.commit()

    headers = _login(app_client, person.email)
    _, csrf = _csrf(app_client, f"/labs/{activity.id}", headers)
    response = app_client.post(
        f"/labs/instances/{instance.id}/reset",
        headers={**headers, "x-csrf-token": csrf},
    )

    assert response.status_code == 200
    assert str(operation.id) in response.text
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 1


def test_check_refuses_when_a_deploy_operation_is_already_open(
    app_client, admin_session, tenant_a
):
    person = _make_person(admin_session, tenant_a, "check-during-reset@a.edu")
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name="dal-check-during-reset",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    operation = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="deploy",
        requested_by=person.id,
    )
    admin_session.add(operation)
    admin_session.commit()

    headers = _login(app_client, person.email)
    _, csrf = _csrf(app_client, f"/labs/{activity.id}", headers)
    response = app_client.post(
        f"/labs/instances/{instance.id}/check",
        headers={**headers, "x-csrf-token": csrf},
    )

    assert response.status_code == 409
    assert admin_session.query(LabOperation).filter_by(instance_id=instance.id).count() == 1
    assert operation.kind == "deploy"


def test_completed_check_operation_poll_renders_its_durable_score(
    app_client, admin_session, tenant_a
):
    person = _make_person(admin_session, tenant_a, "poll-owner@a.edu")
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name="dal-poll-score",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    checked_at = datetime.now(UTC)
    operation = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="check",
        state="succeeded",
        requested_by=person.id,
        claimed_at=checked_at - timedelta(seconds=1),
        finished_at=checked_at + timedelta(seconds=1),
    )
    submission = Submission(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        answers={"instance": str(instance.id), "seed": {}},
        attempt_no=1,
        created_at=checked_at,
    )
    admin_session.add_all([operation, submission])
    admin_session.flush()
    admin_session.add(
        Score(
            tenant_id=tenant_a.id,
            submission_id=submission.id,
            score=1,
            max_score=1,
            fraction=1,
            passed=True,
            per_item=[],
            source="auto",
        )
    )
    later_submission = Submission(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=person.id,
        answers={"instance": str(instance.id), "seed": {}},
        attempt_no=2,
        created_at=checked_at + timedelta(minutes=1),
    )
    admin_session.add(later_submission)
    admin_session.flush()
    admin_session.add(
        Score(
            tenant_id=tenant_a.id,
            submission_id=later_submission.id,
            score=0,
            max_score=1,
            fraction=0,
            passed=False,
            per_item=[],
            source="auto",
        )
    )
    admin_session.commit()

    headers = _login(app_client, person.email)
    response = app_client.get(f"/labs/operations/{operation.id}", headers=headers)
    assert response.status_code == 200
    assert "Passed" in response.text
    assert "1.0/1.0" in response.text


def test_operation_poll_rejects_another_person(app_client, admin_session, tenant_a):
    owner = _make_person(admin_session, tenant_a, "poll-real-owner@a.edu")
    intruder = _make_person(admin_session, tenant_a, "poll-intruder@a.edu")
    _course, activity, _ = _seed_lab(admin_session, tenant_a)
    instance = LabInstance(
        tenant_id=tenant_a.id,
        activity_id=activity.id,
        person_id=owner.id,
        instance_name="dal-poll-owner",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    operation = LabOperation(
        tenant_id=tenant_a.id,
        instance_id=instance.id,
        kind="check",
        requested_by=owner.id,
    )
    admin_session.add(operation)
    admin_session.commit()

    headers = _login(app_client, intruder.email)
    response = app_client.get(f"/labs/operations/{operation.id}", headers=headers)
    assert response.status_code == 403


def test_operation_poll_hides_another_tenant(
    app_client, admin_session, tenant_a, tenant_b
):
    viewer = _make_person(admin_session, tenant_a, "poll-viewer@a.edu")
    owner = _make_party(
        admin_session, tenant_b,
        email="poll-owner@b.edu",
        first_name="B",
        last_name="Owner",
    )
    admin_session.add(owner)
    admin_session.flush()
    _course, activity, _ = _seed_lab(admin_session, tenant_b)
    instance = LabInstance(
        tenant_id=tenant_b.id,
        activity_id=activity.id,
        person_id=owner.id,
        instance_name="dal-poll-tenant-b",
        seed={},
        status="active",
        consoles={},
    )
    admin_session.add(instance)
    admin_session.flush()
    operation = LabOperation(
        tenant_id=tenant_b.id,
        instance_id=instance.id,
        kind="check",
        requested_by=owner.id,
    )
    admin_session.add(operation)
    admin_session.commit()

    headers = _login(app_client, viewer.email)
    response = app_client.get(f"/labs/operations/{operation.id}", headers=headers)
    assert response.status_code == 404
