"""Maintained full-assembly browser journeys; execution is hosted-CI only.

Each subprocess binds the ordinary application to its isolated genesis database.
Tenant resolution, facet admission, sessions, CSRF, transactions and domain owners
are genuine. Only fixture setup/inspection uses app_admin. No mail, ERP or lab
worker is started. Lab worker projections below are explicit fixture transitions,
not claims of provider execution or grading.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import indent
from uuid import uuid4

from dotmac_learning.participation_models import Cohort, CohortTrack, Track

from tests.issue2_wave2_support import _bank_with_questions
from tests.issue2_wave3_support import current_account, grant_std, make_person_party

assert current_account  # Imported pytest fixture.


_BOOT = r"""
import os
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyRoleGrant, Role
from dotmac_learning.participation_models import Enrollment

from app.main import app
from app.models.account_token import AccountToken
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint
from app.models.lab import LabInstance, LabOperation
from app.models.onboarding import OnboardingTask

tid = UUID(os.environ["JOURNEY_TENANT"])
origin = os.environ["JOURNEY_ORIGIN"]
engine = create_engine(os.environ["JOURNEY_ADMIN_URL"])

@contextmanager
def inspect_db():
    with Session(engine, expire_on_commit=False) as db, db.begin():
        db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})
        yield db

def rows(model, **values):
    with inspect_db() as db:
        return list(db.scalars(select(model).filter_by(tenant_id=tid, **values)).all())

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)

def emailed_link(kind, path, email):
    mail = rows(EmailOutbox, kind=kind, recipient=email)
    assert len(mail) == 1, (kind, len(mail))
    assert mail[0].status == "pending" and mail[0].attempts == 0 and mail[0].sent_at is None
    parser = Links()
    parser.feed(mail[0].html_body)
    links = [link for link in parser.links if urlsplit(link).path == path]
    assert links and all(link == links[0] for link in links)
    assert links[0].startswith(origin + path + "?")
    return links[0]

def csrf_get(path):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    assert client.cookies.get("csrf_token")
    return response

def post(path, data=None, *, htmx=False):
    csrf = client.cookies.get("csrf_token")
    assert csrf
    return client.post(path, data=data or {}, headers={
        "x-csrf-token": csrf,
        "Origin": origin,
        **({"HX-Request": "true"} if htmx else {}),
    })

def login(email, password):
    csrf_get("/login")
    response = post("/login", {"email": email, "password": password})
    assert response.status_code == 303, response.status_code
    assert client.cookies.get("session")

"""


def _run(a, script, **extra):
    """A fresh process is necessary because Kernel seals one runtime per process."""
    env = {
        **os.environ,
        "DATABASE_URL": a.app_engine.url.render_as_string(hide_password=False),
        "PLATFORM_DATABASE_URL": a.platform_engine.url.render_as_string(hide_password=False),
        "ENVIRONMENT": "dev",
        "CSRF_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "false",
        "JOURNEY_ADMIN_URL": a.admin.get_bind().url.render_as_string(hide_password=False),
        "JOURNEY_ORIGIN": f"http://{a.tenant.slug}.localhost",
        "JOURNEY_TENANT": str(a.tid),
        "JOURNEY_EMAIL": a.email,
        **{key: str(value) for key, value in extra.items()},
    }
    source = (
        _BOOT
        + "\nwith TestClient(app, base_url=origin, follow_redirects=False) as client:\n"
        + indent(script, "    ")
        + '\nengine.dispose()\nprint("HTTP-JOURNEY-OK")\n'
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter/source, isolated hosted fixture
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "HTTP-JOURNEY-OK" in result.stdout


_ADMISSIONS = r"""
email = "intake@example.test"
page = csrf_get("/apply")
assert os.environ["JOURNEY_CHOICE"] in page.text
form = {
    "first_name": "Journey", "last_name": "Applicant", "email": email,
    "track_choice": os.environ["JOURNEY_CHOICE"], "has_device": "yes",
}
# The actual CSRF middleware must refuse a valid form without proof and leave no applicant.
assert client.post("/apply", data=form).status_code == 403
assert rows(Applicant, email=email) == []
submitted = post("/apply", form)
assert submitted.status_code == 200 and "Application received" in submitted.text
applicant = rows(Applicant, email=email)[0]
assert applicant.status == "applied" and applicant.source == "website" and applicant.person_id is None
assert applicant.has_device is True
assert len(rows(EmailOutbox, recipient=email, kind="application_received")) == 1
exam = emailed_link("entrance_invite", "/apply/assessment", email)
token = parse_qs(urlsplit(exam).query)["token"][0]
csrf_get(exam)
assert post("/apply/assessment/start", {"token": token}).status_code == 303
assert csrf_get(exam).status_code == 200
graded = post("/apply/assessment", {"token": token, "q1": "A", "q2": "A", "q3": "A", "q4": "A"})
assert graded.status_code == 200 and "Assessment submitted" in graded.text
applicant = rows(Applicant, email=email)[0]
assert applicant.assessment_taken_at is not None and applicant.assessment_score == 1.0
assert applicant.status == "applied"  # Manual review; this cohort has no auto-accept threshold.

login(os.environ["JOURNEY_EMAIL"], "chosen-password9")
detail = csrf_get(f"/admin/applications/{applicant.id}")
assert email in detail.text
accepted = post(f"/admin/applications/{applicant.id}/action", {"action": "accept", "reason": "Reviewed intake"})
assert accepted.status_code == 303
assert rows(Applicant, email=email)[0].status == "onboarding"
tasks = {task.key: task.status for task in rows(OnboardingTask, applicant_id=applicant.id)}
assert tasks == {"entrance_assessment": "done", "confirm_details": "pending", "orientation": "pending"}
portal = emailed_link("onboarding_invite", "/onboarding", email)
onboarding_token = parse_qs(urlsplit(portal).query)["token"][0]
assert post("/logout").status_code == 303
csrf_get(portal)
assert post("/onboarding/confirm", {"token": onboarding_token}).status_code == 303
assert rows(Applicant, email=email)[0].status == "onboarding"
assert rows(Enrollment, cohort_id=UUID(os.environ["JOURNEY_COHORT"])) == []
assert post("/onboarding/orientation", {"token": onboarding_token}).status_code == 303
enrolled = rows(Applicant, email=email)[0]
assert enrolled.status == "enrolled" and enrolled.person_id is not None
party = rows(Party, id=enrolled.person_id)
assert len(party) == 1 and party[0].email == email
enrollments = rows(Enrollment, person_id=enrolled.person_id)
assert len(enrollments) == 1
assert (str(enrollments[0].cohort_id), str(enrollments[0].track_id), enrollments[0].status) == (
    os.environ["JOURNEY_COHORT"], os.environ["JOURNEY_TRACK"], "active")
with inspect_db() as db:
    roles = set(db.scalars(select(Role.slug).join(PartyRoleGrant, PartyRoleGrant.role_id == Role.id).where(
        PartyRoleGrant.tenant_id == tid, PartyRoleGrant.party_id == enrolled.person_id)))
assert "student" in roles
transitions = rows(AuditEvent, entity_id=str(applicant.id), action="applicant.transition")
assert sorted(event.details["to_status"] for event in transitions) == ["accepted", "enrolled", "onboarding", "screened"]
admin_transitions = [event for event in transitions if event.details["source"] == "admin_web"]
assert len(admin_transitions) == 3
assert all(str(event.actor_party_id) == os.environ["JOURNEY_PERSON"] for event in admin_transitions)
assert all(task.status == "done" for task in rows(OnboardingTask, applicant_id=applicant.id))
setup = emailed_link("enrollment_welcome", "/accept-invite", email)
invite = parse_qs(urlsplit(setup).query)["token"][0]
csrf_get(setup)
invalid = post("/accept-invite", {"token": "invalid-token", "password": "student-password9"}, htmx=True)
assert invalid.status_code == 400 and "Account was not activated" in invalid.text
activated = post("/accept-invite", {"token": invite, "password": "student-password9"}, htmx=True)
assert activated.status_code == 200 and "Account activated" in activated.text
reused = post("/accept-invite", {"token": invite, "password": "replacement-password9"}, htmx=True)
assert reused.status_code == 400 and "Account was not activated" in reused.text
login(email, "student-password9")
assert csrf_get("/account").status_code == 200
# Repeated completion cannot duplicate membership, identity, or welcome delivery.
assert post("/onboarding/orientation", {"token": onboarding_token}).status_code == 303
assert len(rows(Enrollment, person_id=enrolled.person_id)) == 1
assert len(rows(Party, email=email)) == 1
assert len(rows(EmailOutbox, recipient=email, kind="enrollment_welcome")) == 1
assert all(mail.status == "pending" and mail.attempts == 0 for mail in rows(EmailOutbox, recipient=email))
assert rows(LabInstance) == [] and rows(LabOperation) == []
assert rows(ERPCompletionCheckpoint) == []
"""


def test_public_application_admin_review_onboarding_invite_claim_and_login(current_account):
    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid, role="admin")
    bank = _bank_with_questions(a.admin, a.tenant)
    cohort = Cohort(tenant_id=a.tid, name="HTTP intake", discipline="fiber", status="active", entrance_bank_id=bank.id)
    track = Track(tenant_id=a.tid, slug=uuid4().hex, name="HTTP training track", status="active")
    a.admin.add_all([cohort, track])
    a.admin.flush()
    a.admin.add(CohortTrack(tenant_id=a.tid, cohort_id=cohort.id, track_id=track.id, status="active"))
    a.admin.commit()
    _run(
        a,
        _ADMISSIONS,
        JOURNEY_CHOICE=f"{cohort.id}:{track.id}",
        JOURNEY_COHORT=cohort.id,
        JOURNEY_TRACK=track.id,
        JOURNEY_PERSON=a.pid,
    )


_RESET = r"""
email = os.environ["JOURNEY_EMAIL"]
csrf_get("/forgot")
assert client.post("/forgot", data={"email": email}).status_code == 403
assert rows(AccountToken, kind="password_reset") == []
known = post("/forgot", {"email": " " + email.upper() + " "})
unknown = post("/forgot", {"email": "missing@example.test"})
assert known.status_code == unknown.status_code == 200
assert known.content == unknown.content
assert known.headers["content-type"] == unknown.headers["content-type"]
assert email not in known.text and "missing@example.test" not in unknown.text
assert len(rows(AccountToken, kind="password_reset")) == 1
assert len(rows(EmailOutbox, kind="password_reset")) == 1
reset = emailed_link("password_reset", "/reset", email)
token = parse_qs(urlsplit(reset).query)["token"][0]
csrf_get(reset)
bad = post("/reset", {"token": "invalid-token", "password": "changed-password9"}, htmx=True)
assert bad.status_code == 400 and "Password was not changed" in bad.text
changed = post("/reset", {"token": token, "password": "changed-password9"}, htmx=True)
assert changed.status_code == 200 and "Password updated" in changed.text
spent = rows(AccountToken, kind="password_reset")
assert len(spent) == 1 and spent[0].used_at is not None
again = post("/reset", {"token": token, "password": "another-password9"}, htmx=True)
assert again.status_code == 400 and "Password was not changed" in again.text
csrf_get("/login")
old = post("/login", {"email": email, "password": "chosen-password9"})
assert old.status_code == 401 and not client.cookies.get("session")
login(email, "changed-password9")
assert csrf_get("/account").status_code == 200
wrong = post("/account/password", {"current_password": "incorrect-password9",
    "new_password": "final-password9", "confirm_password": "final-password9"}, htmx=True)
assert wrong.status_code == 200 and "Current password is incorrect" in wrong.text
updated = post("/account/password", {"current_password": "changed-password9",
    "new_password": "final-password9", "confirm_password": "final-password9"}, htmx=True)
assert updated.status_code == 200 and "Password updated" in updated.text
assert client.get("/account").status_code == 302  # Current session was revoked by the change.
assert post("/logout").status_code == 303 and not client.cookies.get("session")
csrf_get("/login")
old = post("/login", {"email": email, "password": "changed-password9"})
assert old.status_code == 401 and not client.cookies.get("session")
login(email, "final-password9")
assert csrf_get("/account").status_code == 200
assert post("/logout").status_code == 303 and not client.cookies.get("session")
assert client.get("/account").status_code == 302
assert all(mail.status == "pending" and mail.sent_at is None for mail in rows(EmailOutbox))
"""


def test_forgot_privacy_reset_token_spend_and_subsequent_login(current_account):
    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid)
    a.admin.commit()
    _run(a, _RESET)


_LABS = r"""
activity = os.environ["JOURNEY_ACTIVITY"]
foreign = os.environ["JOURNEY_FOREIGN_INSTANCE"]
login(os.environ["JOURNEY_EMAIL"], "chosen-password9")
csrf_get(f"/labs/{activity}")
assert client.post(f"/labs/{activity}/launch").status_code == 403
assert len(rows(LabInstance)) == 1  # Only the foreign fixture exists.
launched = post(f"/labs/{activity}/launch")
assert launched.status_code == 200 and launched.headers["HX-Refresh"] == "true"
own = [instance for instance in rows(LabInstance) if str(instance.id) != foreign]
assert len(own) == 1
instance = own[0]
assert instance.status == "queued" and instance.runtime_presence == "absent" and instance.consoles == {}
deploys = rows(LabOperation, instance_id=instance.id)
assert len(deploys) == 1 and deploys[0].kind == "deploy" and deploys[0].state == "queued"
assert deploys[0].attempts == 0 and deploys[0].claimed_by is None
assert str(deploys[0].requested_by) == os.environ["JOURNEY_PERSON"]
assert post(f"/labs/{activity}/launch").status_code == 200
assert len(rows(LabInstance)) == 2 and len(rows(LabOperation, instance_id=instance.id)) == 1
status = client.get(f"/labs/instances/{instance.id}/status")
assert status.status_code == 200 and "Waiting for lab capacity" in status.text
assert post(f"/labs/instances/{instance.id}/check").status_code == 409
assert post(f"/labs/instances/{instance.id}/reset").status_code == 409

# A committed fixture projection represents a completed worker cycle. It does
# not invoke a worker, provider, real console or checker. HTTP only queues intent.
with inspect_db() as db:
    db.get(LabOperation, deploys[0].id).state = "succeeded"
    active = db.get(LabInstance, instance.id)
    active.status = "active"
    active.runtime_presence = "present"
checked = post(f"/labs/instances/{instance.id}/check")
assert checked.status_code == 200
checks = rows(LabOperation, instance_id=instance.id, kind="check")
assert len(checks) == 1 and checks[0].state == "queued" and checks[0].attempts == 0
assert post(f"/labs/instances/{instance.id}/check").status_code == 200
assert len(rows(LabOperation, instance_id=instance.id, kind="check")) == 1
assert post(f"/labs/instances/{instance.id}/reset").status_code == 409
assert client.get(f"/labs/operations/{checks[0].id}").status_code == 200
with inspect_db() as db:
    db.get(LabOperation, checks[0].id).state = "succeeded"
reset = post(f"/labs/instances/{instance.id}/reset")
assert reset.status_code == 200
resets = [op for op in rows(LabOperation, instance_id=instance.id, kind="deploy") if op.id != deploys[0].id]
assert len(resets) == 1 and resets[0].state == "queued" and resets[0].attempts == 0

before = len(rows(LabOperation))
for suffix in ("status", "check", "reset"):
    path = f"/labs/instances/{foreign}/{suffix}"
    denied = client.get(path) if suffix == "status" else post(path)
    assert denied.status_code == 403, (suffix, denied.status_code)
assert len(rows(LabOperation)) == before
assert rows(LabInstance, id=UUID(foreign))[0].status == "active"
assert all(op.claimed_by is None and op.attempts == 0 for op in rows(LabOperation))
assert post("/logout").status_code == 303
assert client.get(f"/labs/instances/{instance.id}/status").status_code == 302
"""


def test_lab_launch_poll_check_reset_queue_and_foreign_owner_refusal(current_account):
    from dotmac_learning.models import Activity, Course
    from dotmac_learning.participation_models import CourseOffering, Enrollment

    from app.models.lab import LabInstance, LabTemplate

    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid)
    course = Course(
        tenant_id=a.tid,
        slug=uuid4().hex,
        title="HTTP lab course",
        discipline="networking",
        source_ref="hosted-http-fixture",
        status="published",
        listed=False,
    )
    cohort = Cohort(tenant_id=a.tid, name="HTTP lab cohort", discipline="networking", status="active")
    foreign_person = make_person_party(
        a.admin, tenant_id=a.tid, email="other@example.test", first_name="Other", last_name="Learner"
    )
    a.admin.add_all([course, cohort])
    a.admin.flush()
    activity = Activity(
        tenant_id=a.tid,
        course_id=course.id,
        chapter_number=1,
        type="lab",
        title="HTTP queue lab",
        assessment_mode="graded",
        pass_threshold=0.7,
        grading="auto",
        weight=1.0,
    )
    a.admin.add(activity)
    a.admin.flush()
    a.admin.add_all(
        [
            Enrollment(
                tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid, role_in_cohort="student", status="active"
            ),
            CourseOffering(tenant_id=a.tid, cohort_id=cohort.id, course_id=course.id, status="active"),
            LabTemplate(
                tenant_id=a.tid,
                course_id=course.id,
                activity_id=activity.id,
                slug="http-lab",
                title="HTTP queue lab",
                topology="",
                checks=[],
            ),
        ]
    )
    foreign = LabInstance(
        tenant_id=a.tid,
        activity_id=activity.id,
        person_id=foreign_person.id,
        instance_name=f"dal-{uuid4().hex}",
        status="active",
        runtime_presence="present",
        consoles={},
    )
    a.admin.add(foreign)
    a.admin.commit()
    _run(a, _LABS, JOURNEY_ACTIVITY=activity.id, JOURNEY_FOREIGN_INSTANCE=foreign.id, JOURNEY_PERSON=a.pid)
