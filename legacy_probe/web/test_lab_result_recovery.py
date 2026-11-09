"""Hosted PostgreSQL regression coverage for durable lab result recovery."""
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType, UserCredential

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.models.offering import CourseOffering
from app.services import settings_store
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, web_login


def _make_person(admin_session, tenant, email: str) -> Party:
    # Genuine canonical fixture records, following sign_in_account setup.
    now = datetime.now(UTC)
    p = Party(
        tenant_id=tenant.id, party_type=PartyType.person,
        display_name="S L", email=email, is_active=True,
    )
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=p.id, first_name="S", last_name="L"))
    admin_session.flush()
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
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    return p


def _login(app_client, email: str) -> dict[str, str]:
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, "password1")
    return h

def _entitle(admin_session, tenant, person, course):
    """Give the person access to the course via cohort + enrollment + offering."""
    coh = Cohort(tenant_id=tenant.id, name="Lab Cohort", discipline=course.discipline,
                 status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=course.id,
                                     status="active"))
    admin_session.commit()
    return coh

def _seed_lab(admin_session, tenant, *, with_course=True):
    course = Course(
        tenant_id=tenant.id,
        slug="foundation",
        title="F",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    act = Activity(
        tenant_id=tenant.id,
        course_id=course.id,
        chapter_number=14,
        type="lab",
        title="VLAN Lab",
        pass_threshold=0.5, grading="auto", weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
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

@pytest.fixture
def journey(admin_session, tenant_a, app_client):
    person = _make_person(admin_session, tenant_a, "recovery@a.edu")
    course, activity, _ = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, person, course)
    instance = LabInstance(
        tenant_id=tenant_a.id, activity_id=activity.id, person_id=person.id,
        instance_name="recovery-" + uuid4().hex, seed={}, status="active", consoles={},
    )
    admin_session.add(instance)
    admin_session.commit()
    return person, activity, instance, _login(app_client, person.email)


def _operation(db, instance, *, kind="check", state="succeeded", when=None):
    when = when or datetime.now(UTC)
    operation = LabOperation(
        tenant_id=instance.tenant_id, instance_id=instance.id, requested_by=instance.person_id,
        kind=kind, state=state, requested_at=when - timedelta(seconds=2),
        claimed_at=when - timedelta(seconds=1) if state != "queued" else None,
        finished_at=when + timedelta(seconds=1) if state not in ("queued", "claimed") else None,
        last_error="recovery failure detail" if state == "failed" else None,
        # ck_lab_operations_claim_lease_required: a claimed row carries its lease.
        claim_lease_seconds=settings_store.execution_policy(db).lease_seconds(kind) if state == "claimed" else None,
    )
    db.add(operation)
    db.commit()
    return operation


def _score(db, instance, *, when=None, person_id=None, activity_id=None, tenant_id=None, attempt=1):
    submission = Submission(
        tenant_id=tenant_id or instance.tenant_id,
        activity_id=activity_id or instance.activity_id, person_id=person_id or instance.person_id,
        answers={"instance": str(instance.id)}, attempt_no=attempt,
        created_at=when or datetime.now(UTC),
    )
    db.add(submission)
    db.flush()
    db.add(Score(
        tenant_id=submission.tenant_id, submission_id=submission.id, score=1, max_score=1,
        fraction=1, passed=True, per_item=[{"id": "saved-marker", "label": "saved-marker", "pass": True}],
    ))
    db.commit()


@pytest.mark.parametrize("surface", ["detail", "status"])
def test_saved_checks_survive_repeated_gets(app_client, admin_session, journey, surface):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now)
    _score(admin_session, instance, when=now)
    url = f"/labs/{activity.id}" if surface == "detail" else f"/labs/instances/{instance.id}/status"
    for _ in range(2):
        response = app_client.get(url, headers=headers)
        assert response.status_code == 200
        assert "saved-marker" in response.text
        assert "Last saved checks for this lab environment" in response.text
    assert admin_session.query(Submission).filter_by(person_id=instance.person_id).count() == 1


@pytest.mark.parametrize("surface", ["detail", "status"])
@pytest.mark.parametrize("state", ["queued", "claimed", "failed", "cancelled"])
def test_latest_check_action_recovers_on_get(app_client, admin_session, journey, surface, state):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now - timedelta(minutes=1))
    _score(admin_session, instance, when=now - timedelta(minutes=1))
    operation = _operation(admin_session, instance, state=state, when=now)
    url = f"/labs/{activity.id}" if surface == "detail" else f"/labs/instances/{instance.id}/status"
    response = app_client.get(url, headers=headers)
    assert response.status_code == 200
    assert "saved-marker" in response.text
    if state in ("queued", "claimed"):
        assert f'hx-get="/labs/operations/{operation.id}"' in response.text
    else:
        assert f"Checks {state}." in response.text
        assert f'hx-get="/labs/operations/{operation.id}"' not in response.text
    if state == "failed":
        assert "recovery failure detail" in response.text


@pytest.mark.parametrize("state", ["queued", "failed", "succeeded"])
def test_reset_boundary_hides_old_live_configuration_result(app_client, admin_session, journey, state):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now - timedelta(minutes=1))
    _score(admin_session, instance, when=now - timedelta(minutes=1))
    reset = _operation(admin_session, instance, kind="deploy", state=state, when=now)
    for url in (f"/labs/{activity.id}", f"/labs/instances/{instance.id}/status"):
        response = app_client.get(url, headers=headers)
        assert response.status_code == 200
        assert "saved-marker" not in response.text
        if state == "queued":
            assert f'hx-get="/labs/operations/{reset.id}"' in response.text
        elif state == "failed":
            assert "Lab operation failed." in response.text
    assert admin_session.query(Submission).filter_by(person_id=instance.person_id).count() == 1


def test_new_instance_does_not_inherit_previous_instance_checks(app_client, admin_session, journey):
    _, activity, instance, headers = journey
    _operation(admin_session, instance)
    _score(admin_session, instance)
    instance.status = "reaped"
    fresh = LabInstance(
        tenant_id=instance.tenant_id, activity_id=activity.id, person_id=instance.person_id,
        instance_name="fresh-" + uuid4().hex, seed={}, status="active", consoles={},
    )
    admin_session.add(fresh)
    admin_session.commit()
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert response.status_code == 200
    assert str(fresh.id) in response.text
    assert "saved-marker" not in response.text


@pytest.mark.parametrize("wrong", ["person", "activity", "tenant", "instance", "window"])
def test_recovery_rejects_unrelated_results(app_client, admin_session, journey, tenant_a, tenant_b, wrong):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now)
    kwargs = {"when": now}
    if wrong == "person":
        other = _make_person(admin_session, tenant_a, "other@a.edu")
        kwargs["person_id"] = other.id
    elif wrong == "activity":
        other = Activity(tenant_id=instance.tenant_id, course_id=activity.course_id, title="Other", type="lab", grading="auto", weight=1.0)
        admin_session.add(other)
        admin_session.commit()
        kwargs["activity_id"] = other.id
    elif wrong == "tenant":
        _, other, _ = _seed_lab(admin_session, tenant_b)
        other_person = _make_person(admin_session, tenant_b, "other@b.edu")
        kwargs.update(tenant_id=tenant_b.id, activity_id=other.id, person_id=other_person.id)
    elif wrong == "window":
        kwargs["when"] = now + timedelta(minutes=1)
    elif wrong == "instance":
        # A different instance's score must not be selected merely by activity.
        instance = LabInstance(tenant_id=instance.tenant_id, activity_id=activity.id, person_id=instance.person_id,
                               instance_name="other-" + uuid4().hex, seed={}, status="reaped", consoles={})
        admin_session.add(instance)
        admin_session.commit()
    _score(admin_session, instance, **kwargs)
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert response.status_code == 200
    assert "saved-marker" not in response.text
    assert "saved result is unavailable" in response.text


def test_missing_operation_window_does_not_guess_a_score(app_client, admin_session, journey):
    _, activity, instance, headers = journey
    operation = _operation(admin_session, instance)
    operation.claimed_at = None
    admin_session.commit()
    _score(admin_session, instance)
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert "saved-marker" not in response.text
    assert "saved result is unavailable" in response.text
    assert "Checks failed" not in response.text


@pytest.mark.parametrize("wrong", ["person", "tenant"])
def test_recovered_status_and_operation_remain_owner_gated(
    app_client, admin_session, journey, tenant_a, tenant_b, wrong
):
    _, _, instance, _ = journey
    operation = _operation(admin_session, instance, state="queued")
    tenant = tenant_a if wrong == "person" else tenant_b
    intruder = _make_person(admin_session, tenant, f"intruder-{wrong}@example.edu")
    # Use a fresh browser session when changing identity, as the lab route tests do.
    app_client.cookies.clear()
    if wrong == "person":
        headers = _login(app_client, intruder.email)
        expected = 403
    else:
        headers = {"Host": "beta.localhost"}
        web_login(app_client, headers, intruder.email, "password1")
        expected = 404
    for url in (f"/labs/instances/{instance.id}/status", f"/labs/operations/{operation.id}"):
        response = app_client.get(url, headers=headers)
        assert response.status_code == expected
        assert "saved-marker" not in response.text


def test_equal_check_request_times_do_not_guess_latest_score(app_client, admin_session, journey):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now)
    _operation(admin_session, instance, when=now)
    _score(admin_session, instance, when=now)
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert response.status_code == 200
    assert "saved-marker" not in response.text


@pytest.mark.parametrize("missing", ["claimed_at", "finished_at", "inverted"])
def test_operation_poll_refuses_incomplete_or_inverted_window(
    app_client, admin_session, journey, missing
):
    _, _, instance, headers = journey
    now = datetime.now(UTC)
    operation = _operation(admin_session, instance, when=now)
    _score(admin_session, instance, when=now)
    if missing == "inverted":
        operation.finished_at = operation.claimed_at - timedelta(seconds=1)
    else:
        setattr(operation, missing, None)
    admin_session.commit()
    response = app_client.get(f"/labs/operations/{operation.id}", headers=headers)
    assert response.status_code == 200
    assert "saved-marker" not in response.text
    assert "saved result is unavailable" in response.text


@pytest.mark.parametrize("kind", ["vr-ros", "vr-vmx", "linux"])
def test_console_startup_guidance_is_specific_to_router_nodes(
    app_client, admin_session, journey, kind
):
    _, activity, instance, headers = journey
    instance.consoles = {"node": {"kind": kind}}
    # Guidance is only shown for a live runtime (the column default is 'absent').
    instance.runtime_presence = "present"
    admin_session.commit()
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert response.status_code == 200
    guidance = "The router may still be starting; retry the console if it is not available."
    assert (guidance in response.text) == kind.startswith("vr-")


def test_launch_copy_explains_device_startup_without_a_time_promise(
    app_client, admin_session, journey
):
    _, activity, instance, headers = journey
    instance.status = "reaped"
    admin_session.commit()
    response = app_client.get(f"/labs/{activity.id}", headers=headers)
    assert response.status_code == 200
    assert "Consoles become available as each device finishes starting." in response.text
    assert "give it a few seconds" not in response.text


class _FragmentElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _assert_poll_owner(html, operation, target):
    elements = _FragmentElements(html).elements
    polls = [attrs for _, attrs in elements if attrs.get("hx-get") == f"/labs/operations/{operation.id}"]
    assert len(polls) == 1
    assert polls[0]["hx-target"] == target
    assert polls[0]["hx-swap"] == "innerHTML"


def test_recovered_check_poll_replaces_prior_score_and_preserves_failure_context(
    app_client, admin_session, journey
):
    _, activity, instance, headers = journey
    now = datetime.now(UTC)
    _operation(admin_session, instance, when=now - timedelta(minutes=1))
    _score(admin_session, instance, when=now - timedelta(minutes=1))
    prior_score = admin_session.query(Score).join(Submission).filter(Submission.person_id == instance.person_id).one()
    prior_score.per_item = [{"id": "prior-result", "label": "prior-result", "pass": True}]
    admin_session.commit()
    pending = _operation(admin_session, instance, when=now, state="queued")
    initial = app_client.get(f"/labs/{activity.id}", headers=headers)
    _assert_poll_owner(initial.text, pending, "#lab-checks")
    assert initial.text.count("prior-result") == 1
    polled = app_client.get(f"/labs/operations/{pending.id}", headers=headers)
    _assert_poll_owner(polled.text, pending, "#lab-checks")
    assert polled.text.count("prior-result") == 1

    pending.state = "failed"
    pending.finished_at = now + timedelta(seconds=1)
    pending.last_error = "check execution failed"
    admin_session.commit()
    failed = app_client.get(f"/labs/operations/{pending.id}", headers=headers)
    assert "Checks failed." in failed.text
    assert "check execution failed" in failed.text
    assert failed.text.count("prior-result") == 1
    assert 'hx-get="/labs/operations/' not in failed.text

    # A subsequent check settles into one new result for the whole target.
    completed = _operation(admin_session, instance, when=now + timedelta(minutes=1))
    _score(admin_session, instance, when=now + timedelta(minutes=1), attempt=2)
    result = app_client.get(f"/labs/operations/{completed.id}", headers=headers)
    assert result.status_code == 200
    assert result.text.count("saved-marker") == 1
    assert "prior-result" not in result.text
    assert "Checks failed." not in result.text
    assert 'id="lab-checks"' not in result.text


@pytest.mark.parametrize("surface", ["detail", "status"])
@pytest.mark.parametrize("settlement", ["succeeded", "failed", "cancelled"])
def test_recovered_reset_has_one_owner_through_terminal_transition(
    app_client, admin_session, journey, surface, settlement
):
    _, activity, instance, headers = journey
    pending = _operation(admin_session, instance, kind="deploy", state="queued")
    # The worker has not reserved/reset the still-active instance yet.
    url = f"/labs/{activity.id}" if surface == "detail" else f"/labs/instances/{instance.id}/status"
    initial = app_client.get(url, headers=headers)
    _assert_poll_owner(initial.text, pending, "#lab-state")
    assert "Run checks" not in initial.text
    assert "Reset lab" not in initial.text
    assert 'id="lab-checks"' not in initial.text
    assert f'hx-get="/labs/instances/{instance.id}/status"' not in initial.text
    polled = app_client.get(f"/labs/operations/{pending.id}", headers=headers)
    _assert_poll_owner(polled.text, pending, "#lab-state")
    assert "Run checks" not in polled.text

    pending.state = settlement
    pending.finished_at = datetime.now(UTC)
    if settlement == "failed":
        pending.last_error = "reset execution failed"
        instance.status = "error"
        instance.error = "reset execution failed"
    admin_session.commit()
    terminal = app_client.get(f"/labs/operations/{pending.id}", headers=headers)
    assert terminal.status_code == 200
    assert f'hx-get="/labs/operations/{pending.id}"' not in terminal.text
    if settlement == "failed":
        assert terminal.text.count("Retry lab") == 1
        assert "Lab operation failed." in terminal.text
        assert 'id="lab-checks"' not in terminal.text
    else:
        assert terminal.text.count("Run checks") == 1
        assert terminal.text.count("Reset lab") == 1
        assert terminal.text.count('id="lab-checks"') == 1
        if settlement == "cancelled":
            assert "Lab operation cancelled." in terminal.text
