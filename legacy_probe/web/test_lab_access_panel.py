"""Device-access panel on the lab page: owner-only, template-sourced, escaped."""

from __future__ import annotations

from app.models.lab import LabInstance
from legacy_probe.web.test_labs_web import _entitle, _login, _make_person, _seed_lab

ACCESS = [
    {"node": "r1", "protocol": "webfig", "username": "admin", "default_password": "labpw-1",
     "note": "Practice router."},
    {"node": "pc1", "protocol": "console", "note": "No login needed."},
]


def _instance(admin_session, tenant, act, person, name, status="active"):
    inst = LabInstance(tenant_id=tenant.id, activity_id=act.id, person_id=person.id,
                       instance_name=name, seed={}, status=status, consoles={})
    admin_session.add(inst)
    admin_session.commit()
    return inst


def test_owner_with_live_instance_sees_device_access(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "own@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc1")
    h = _login(app_client, "own@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "data-lab-access" in r.text
    assert "<code>admin</code>" in r.text and "<code>labpw-1</code>" in r.text
    assert "No login needed." in r.text


def test_no_device_access_before_launch(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "pre@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    h = _login(app_client, "pre@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "data-lab-access" not in r.text and "labpw-1" not in r.text


def test_another_learner_without_an_instance_does_not_see_it(app_client, admin_session, tenant_a):
    owner = _make_person(admin_session, tenant_a, "o@a.edu")
    other = _make_person(admin_session, tenant_a, "x@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, owner, course)
    _entitle(admin_session, tenant_a, other, course)
    _instance(admin_session, tenant_a, act, owner, "dal-acc2")

    app_client.cookies.clear()
    h = _login(app_client, "x@a.edu")
    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "labpw-1" not in r.text and "data-lab-access" not in r.text


def test_reaped_instance_shows_no_device_access(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "reaped@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc3", status="reaped")
    h = _login(app_client, "reaped@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert "labpw-1" not in r.text


def test_template_without_access_renders_no_panel(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "none@a.edu")
    course, act, _tpl = _seed_lab(admin_session, tenant_a)
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc4")
    h = _login(app_client, "none@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert r.status_code == 200
    assert "data-lab-access" not in r.text and "Device access" not in r.text


def test_values_are_html_escaped(app_client, admin_session, tenant_a):
    """Defence in depth: even a row that bypassed the loader cannot inject markup."""
    p = _make_person(admin_session, tenant_a, "esc@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = [{"node": "r1", "protocol": "console", "username": "<script>alert(1)</script>"}]
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc5")
    h = _login(app_client, "esc@a.edu")

    r = app_client.get(f"/labs/{act.id}", headers=h)
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_status_fragment_never_carries_device_access(app_client, admin_session, tenant_a):
    """The htmx polling partial must not become a second disclosure path."""
    p = _make_person(admin_session, tenant_a, "frag@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    inst = _instance(admin_session, tenant_a, act, p, "dal-acc6")
    h = _login(app_client, "frag@a.edu")

    r = app_client.get(f"/labs/instances/{inst.id}/status", headers=h)
    assert r.status_code == 200
    assert "labpw-1" not in r.text


def test_error_instance_shows_no_device_access(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "err@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc7", status="error")
    h = _login(app_client, "err@a.edu")

    assert "labpw-1" not in app_client.get(f"/labs/{act.id}", headers=h).text


def test_provisioning_instance_shows_device_access(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "prov@a.edu")
    course, act, tpl = _seed_lab(admin_session, tenant_a)
    tpl.access = ACCESS
    admin_session.commit()
    _entitle(admin_session, tenant_a, p, course)
    _instance(admin_session, tenant_a, act, p, "dal-acc8", status="provisioning")
    h = _login(app_client, "prov@a.edu")

    assert "labpw-1" in app_client.get(f"/labs/{act.id}", headers=h).text
