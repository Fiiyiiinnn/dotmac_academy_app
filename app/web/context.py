"""Jinja context processor injecting nav state into every TemplateResponse.

`nav_context` is registered on `app.web.templating.templates`, so it runs for
EVERY rendered page. It must never raise — a nav glitch must not turn a working
page into a 500 — so the whole body is wrapped and any failure (no tenant, no
session, DB error) falls back to safe empty defaults.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import Request

from app.services import notifications as notif_svc
from app.services import web_auth
from app.services.facilitator_access import facilitator_access
from app.services.settings_store import effective
from app.web import nav


def _empty(request: Request) -> dict:
    """Safe defaults for unauthenticated / no-tenant / error cases."""
    return {
        "current_person": None,
        "is_instructor": False,
        "is_admin": False,
        "has_global_staff_access": False,
        "current_area": nav.area_for_path(request.url.path),
        "nav_areas": [],
        "nav_sidebar": [],
        "branding_name": effective(None).branding_name,
        "avatar_url": None,
        "unread_notifications": 0,
    }


def nav_context(request: Request) -> dict:
    """Build the nav state dict for the current request. Never raises."""
    try:
        tenant = getattr(request.state, "tenant", None)
        token = request.cookies.get(web_auth.COOKIE)
        if tenant is None or not token:
            return _empty(request)

        runtime = resolve_database_runtime()
        db = runtime.session_factory()
        try:
            with runtime.tenant_scope(db, tenant.id):
                person = web_auth._current_person(db, tenant.id, token)
                if person is None:
                    return _empty(request)

                access = facilitator_access(db, tenant_id=tenant.id, person_id=person.id)
                is_instructor = access.can_teach
                is_admin = access.is_admin
                current_area = nav.area_for_path(request.url.path)
                try:
                    unread = notif_svc.unread_count(db, tenant_id=tenant.id, person_id=person.id)
                except Exception:
                    unread = 0
                return {
                    "current_person": person,
                    "is_instructor": is_instructor,
                    "is_admin": is_admin,
                    "has_global_staff_access": access.has_global_staff_access,
                    "current_area": current_area,
                    "nav_areas": nav.areas_for_roles(is_instructor, is_admin),
                    "nav_sidebar": nav.sidebar_for(
                        current_area,
                        scoped_facilitator=(access.has_active_assignment and not access.has_global_staff_access),
                        has_cohort_scope=bool(access.cohort_ids),
                    ),
                    "branding_name": effective(db).branding_name,
                    "avatar_url": person.avatar_path,
                    "unread_notifications": unread,
                }
        finally:
            db.close()
    except Exception:
        return _empty(request)
