"""Task 8 — auth-gated lab console proxy.

The security-critical behaviour is the gate: only the owning, authenticated
person of a tenant-scoped LabInstance may reach a console. The upstream proxy
call is stubbed so no real network is required — we assert the gate let the
request through (non-403/404), not that bytes were shuttled.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType
from fastapi import Response
from sqlalchemy import select

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.lab import LabInstance
from app.models.offering import CourseOffering
from app.web.labs import _console_target
from legacy_probe.conftest import login_location


def _entitled_activity(admin_session, tenant, person_id):
    """Create a lab activity the person is entitled to (course+cohort+offering)."""
    c = Course(tenant_id=tenant.id, slug="lab-course", title="Lab", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    act = Activity(tenant_id=tenant.id, course_id=c.id, chapter_number=1, type="lab",
                   title="Lab", pass_threshold=0.5, grading="auto", weight=1.0)
    coh = Cohort(tenant_id=tenant.id, name="C", discipline="networking", status="active")
    admin_session.add_all([act, coh])
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person_id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=c.id,
                                     status="active"))
    admin_session.flush()
    return act.id


def _make_person(admin_session, tenant, email: str) -> Party:
    from legacy_probe.conftest import make_canonical_account

    p = make_canonical_account(
        admin_session, tenant_id=tenant.id, email=email, first_name="S", last_name="L",
        password="password1", role="student",
    )
    admin_session.commit()
    return p


def _login(app_client, email: str) -> dict[str, str]:
    h = {"Host": "alpha.localhost"}
    app_client.get("/login", headers=h)
    token = app_client.cookies.get("csrf_token")
    assert token
    response = app_client.post(
        "/login", headers={**h, "x-csrf-token": token},
        data={"email": email, "password": "password1"}, follow_redirects=False,
    )
    assert response.status_code == 303
    # The authenticated browser token is bound to its new session cookie.
    app_client.get("/", headers=h)
    return h


def _seed_instance(
    admin_session, tenant, person_id, *, status="active", runtime_presence="present",
    error=None, name="dal-x", consoles=None, activity_id=None,
) -> LabInstance:
    li = LabInstance(
        tenant_id=tenant.id,
        activity_id=activity_id or _entitled_activity(admin_session, tenant, person_id),
        person_id=person_id,
        instance_name=name,
        seed={"o": 5},
        status=status,
        runtime_presence=runtime_presence,
        error=error,
        consoles={"r1": {"kind": "linux", "port": 9001}} if consoles is None else consoles,
    )
    admin_session.add(li)
    admin_session.commit()
    admin_session.refresh(li)
    return li


def test_owner_passes_gate(app_client, admin_session, tenant_a, monkeypatch):
    """Owner reaching their own console → gate passes (stubbed upstream → 200)."""
    p = _make_person(admin_session, tenant_a, "owner@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    h = _login(app_client, "owner@a.edu")

    async def _fake_proxy(request, target, *, webfig=False, reviewed_document=False):
        # ttyd is served under its -b base path, so the index proxies there (not root).
        assert target == f"http://127.0.0.1:9001/labs/instances/{li.id}/console/r1/"
        return Response(content=b"console", status_code=200)

    monkeypatch.setattr("app.web.labs._proxy_http", _fake_proxy)

    r = app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h)
    assert r.status_code == 200
    assert r.status_code not in (403, 404)


def test_other_person_same_tenant_forbidden(app_client, admin_session, tenant_a):
    """A different logged-in person in the same tenant → 403."""
    owner = _make_person(admin_session, tenant_a, "owner2@a.edu")
    _make_person(admin_session, tenant_a, "intruder@a.edu")
    li = _seed_instance(admin_session, tenant_a, owner.id)
    h = _login(app_client, "intruder@a.edu")

    r = app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h)
    assert r.status_code == 403


def test_cross_tenant_not_found(app_client, admin_session, tenant_a, tenant_b):
    """Instance belongs to tenant B; requester authenticated in tenant A → 404."""
    _make_person(admin_session, tenant_a, "owner3@a.edu")
    # Owner person + instance live entirely in tenant_b.
    p_b = Party(tenant_id=tenant_b.id, party_type=PartyType.person, display_name="B B",
                email="owner@b.edu", is_active=True)
    admin_session.add(p_b)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=p_b.id, first_name="B", last_name="B"))
    admin_session.flush()
    li = _seed_instance(admin_session, tenant_b, p_b.id)
    h = _login(app_client, "owner3@a.edu")

    r = app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h)
    assert r.status_code == 404


def test_unknown_node_not_found(app_client, admin_session, tenant_a):
    """Owner but node/target missing → 404."""
    p = _make_person(admin_session, tenant_a, "owner4@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    h = _login(app_client, "owner4@a.edu")

    r = app_client.get(f"/labs/instances/{li.id}/console/nope", headers=h)
    assert r.status_code == 404


@pytest.mark.anyio
def test_unauthenticated_redirects(app_client, admin_session, tenant_a):
    """No session → require_web_user redirects to /login (never reaches proxy)."""
    p = _make_person(admin_session, tenant_a, "owner5@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id)
    h = {"Host": "alpha.localhost"}

    r = app_client.get(
        f"/labs/instances/{li.id}/console/r1", headers=h, follow_redirects=False
    )
    # The kernel facet auth redirects (302) before Academy's own 303 guard.
    assert r.status_code == 302
    assert r.headers["location"] == login_location(f"/labs/instances/{li.id}/console/r1")


# --- non-live instances must never reach a console (security hotfix) --------


async def _must_not_proxy(  # pragma: no cover - failure path
    request, target, *, webfig=False, reviewed_document=False
):
    raise AssertionError(f"proxied a non-live instance to {target}")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "reaped", "runtime_presence": "absent"},
        {"status": "reaped"},  # stale presence must not matter
        {"status": "error", "error": "boom"},
        {"status": "active", "error": "boom"},
        {"status": "active", "runtime_presence": "absent"},
        {"status": "active", "runtime_presence": "unknown"},
        {"status": "resetting"},
        {"status": "provisioning"},
        {"status": "queued"},
    ],
)
def test_non_live_instance_console_is_404(app_client, admin_session, tenant_a, monkeypatch, kwargs):
    p = _make_person(admin_session, tenant_a, "nl@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id, **kwargs)
    h = _login(app_client, "nl@a.edu")
    monkeypatch.setattr("app.web.labs._proxy_http", _must_not_proxy)

    assert app_client.get(f"/labs/instances/{li.id}/console/r1", headers=h).status_code == 404
    assert app_client.get(f"/labs/instances/{li.id}/console/r1/token", headers=h).status_code == 404


def _enroll(admin_session, tenant, activity_id, person_id):
    """Entitle ``person_id`` to the course of ``activity_id`` (existing cohort)."""
    act = admin_session.get(Activity, activity_id)
    coh = admin_session.scalars(
        select(Cohort).where(Cohort.tenant_id == tenant.id)
    ).first()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person_id,
                                 role_in_cohort="student", status="active"))
    admin_session.commit()
    return act


@pytest.mark.parametrize(
    "consoles",
    [
        {"r1": {"kind": "linux", "port": 9001}},
        {"r1": {"kind": "vr-ros", "mgmt": "192.0.2.10"}},
    ],
    ids=["ttyd-port", "routeros-mgmt-ip"],
)
def test_reaped_instance_cannot_reach_target_now_owned_by_another_instance(
    app_client, admin_session, tenant_a, monkeypatch, consoles
):
    """The reaped owner's stored target is byte-for-byte the victim's live target."""
    attacker = _make_person(admin_session, tenant_a, "attacker@a.edu")
    victim = _make_person(admin_session, tenant_a, "victim@a.edu")
    old = _seed_instance(
        admin_session, tenant_a, attacker.id, status="reaped", runtime_presence="absent",
        name="dal-old", consoles=consoles,
    )
    _enroll(admin_session, tenant_a, old.activity_id, victim.id)
    live = _seed_instance(
        admin_session, tenant_a, victim.id, name="dal-live", consoles=consoles,
        activity_id=old.activity_id,
    )
    # Same upstream host:port for both rows, and the victim's is genuinely live.
    assert urlsplit(_console_target(old, "r1")).netloc == urlsplit(_console_target(live, "r1")).netloc
    assert (live.status, live.error, live.runtime_presence) == ("active", None, "present")

    proxied: list[str] = []

    async def _record(request, target, *, webfig=False, reviewed_document=False):
        proxied.append(target)
        return Response(content=b"console", status_code=200)

    monkeypatch.setattr("app.web.labs._proxy_http", _record)

    # Positive control: the victim's own owner reaches the live console.
    h = _login(app_client, "victim@a.edu")
    assert app_client.get(f"/labs/instances/{live.id}/console/r1", headers=h).status_code == 200
    assert len(proxied) == 1
    upstream = urlsplit(proxied[0]).netloc
    assert upstream == urlsplit(_console_target(old, "r1")).netloc

    # The reaped owner is denied and nothing is proxied.
    app_client.cookies.clear()
    h = _login(app_client, "attacker@a.edu")
    assert app_client.get(f"/labs/instances/{old.id}/console/r1", headers=h).status_code == 404
    assert len(proxied) == 1

    # And the denial is the liveness gate: the same row, made live, would be proxied
    # to that very upstream.
    old.status, old.runtime_presence = "active", "present"
    admin_session.commit()
    assert app_client.get(f"/labs/instances/{old.id}/console/r1", headers=h).status_code == 200
    assert urlsplit(proxied[-1]).netloc == upstream


# --- console links render only when the console is reachable -----------------


@pytest.mark.parametrize(
    ("kwargs", "live"),
    [
        ({}, True),
        ({"error": "boom"}, False),
        ({"runtime_presence": "unknown"}, False),
        ({"runtime_presence": "absent"}, False),
    ],
    ids=["live", "active-with-error", "uncertain-runtime", "absent-runtime"],
)
def test_status_fragment_shows_console_links_only_when_live(
    app_client, admin_session, tenant_a, kwargs, live
):
    p = _make_person(admin_session, tenant_a, "links@a.edu")
    li = _seed_instance(admin_session, tenant_a, p.id, **kwargs)
    h = _login(app_client, "links@a.edu")

    r = app_client.get(f"/labs/instances/{li.id}/status", headers=h)
    assert r.status_code == 200
    assert (f"/labs/instances/{li.id}/console/r1" in r.text) is live
    # The link and the gate agree: whatever is linked is reachable.
    from app.web.labs import _console_live

    assert _console_live(li) is live
