"""Cross-role nav integration (Task 10).

Proves the shell wiring is internally consistent:
  * every sidebar link in every area an admin can see resolves to a real route
    (not a 404), so nothing in the nav dead-ends;
  * the brand link points at the first area the current user may enter;
  * a student's shell leaks no Teaching/Admin links.
"""

from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant

from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from app.web.nav import areas_for_roles
from legacy_probe.conftest import make_person_credential, make_person_party, web_login


def _seed_login(app_client, admin_session, tenant, email, role_slug):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Nav", last_name="User")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    admin_session.add(
        PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role_slug].id)
    )
    admin_session.commit()
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, "password1")
    return h

def test_admin_every_sidebar_link_resolves(app_client, admin_session, tenant_a):
    """No nav link dead-ends for an admin (who can see every area)."""
    h = _seed_login(app_client, admin_session, tenant_a, "admin@a.edu", "admin")
    for area in areas_for_roles(is_instructor=True, is_admin=True):
        for item in area["sidebar"]:
            r = app_client.get(item["path"], headers=h, follow_redirects=False)
            assert r.status_code in (200, 302, 303), (
                f"{area['key']} sidebar link {item['path']} -> {r.status_code}"
            )

def test_brand_links_to_first_permitted_area_home(app_client, admin_session, tenant_a):
    h = _seed_login(app_client, admin_session, tenant_a, "brand@a.edu", "admin")
    r = app_client.get("/progress", headers=h)
    assert r.status_code == 200
    first_home = areas_for_roles(is_instructor=True, is_admin=True)[0]["home"]
    assert f'href="{first_home}"' in r.text

def test_student_shell_has_no_teaching_or_admin_links(app_client, admin_session, tenant_a):
    h = _seed_login(app_client, admin_session, tenant_a, "stud@a.edu", "student")
    r = app_client.get("/", headers=h)
    assert r.status_code == 200
    # Neither the higher-area tabs nor their routes leak into a student's shell.
    assert "Teaching" not in r.text
    assert "Admin" not in r.text
    assert "/instructor" not in r.text
    assert "/admin" not in r.text
