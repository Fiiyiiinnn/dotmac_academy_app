"""Config-driven navigation: role areas + contextual sidebars.

`AREAS` is the ordered source of truth for the app shell's top-level area tabs
(Learn / Teaching / Admin) and each area's sidebar. Helpers map roles → visible
areas, a request path → its area, and an area → its sidebar items.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

# Ordered top-level areas. `required` gates visibility:
#   None        → always visible (Learning)
#   "instructor"→ instructor OR admin
#   "admin"     → admin only
AREAS: list[dict] = [
    {
        "key": "learn",
        "label": "Learning",
        "home": "/learn",
        "required": None,
        "sidebar": [
            {"label": "Home", "path": "/learn"},
            {"label": "To-Do", "path": "/todo"},
            {"label": "Courses", "path": "/courses"},
            {"label": "Calendar", "path": "/calendar"},
            {"label": "Announcements", "path": "/announcements"},
            {"label": "Progress", "path": "/progress"},
        ],
    },
    {
        "key": "teaching",
        "label": "Teaching",
        "home": "/instructor",
        "required": "instructor",
        "sidebar": [
            {"label": "Home", "path": "/instructor"},
            {"label": "Cohorts", "path": "/instructor/cohorts"},
            {"label": "Course content", "path": "/instructor/courses"},
            {"label": "Announcements", "path": "/instructor/announcements"},
            {"label": "Reports", "path": "/instructor/reports"},
            {"label": "Success Queue", "path": "/success-queue"},
            {"label": "Gradebook", "path": "/instructor/gradebook"},
            {"label": "Lab monitor", "path": "/instructor/labs"},
        ],
    },
    {
        "key": "admin",
        "label": "Admin",
        "home": "/admin",
        "required": "admin",
        "sidebar": [
            {"label": "Console", "path": "/admin"},
            {"label": "Applications", "path": "/admin/applications"},
            {"label": "Users", "path": "/admin/users"},
            {"label": "Audit", "path": "/admin/audit"},
            {"label": "Reminders", "path": "/admin/reminders"},
            {"label": "Settings", "path": "/admin/settings"},
        ],
    },
]

_BY_KEY: dict[str, dict] = {a["key"]: a for a in AREAS}


def areas_for_roles(is_instructor: bool, is_admin: bool) -> list[dict]:
    """Return the areas visible to a person with the given role flags."""
    visible: list[dict] = []
    for area in AREAS:
        required = area["required"]
        if required is None:
            visible.append(area)
        elif required == "instructor" and (is_instructor or is_admin):
            visible.append(area)
        elif required == "admin" and is_admin:
            visible.append(area)
    return visible


def area_for_path(path: str) -> str:
    """Map a request path to its area key via prefix rules."""
    if path.startswith("/instructor") or path.startswith("/reports"):
        return "teaching"
    if path.startswith("/admin"):
        return "admin"
    return "learn"


def sidebar_for(
    area_key: str,
    *,
    scoped_facilitator: bool = False,
    has_cohort_scope: bool = True,
) -> list[dict]:
    """Return the sidebar items for an area, or an empty list if unknown."""
    area = _BY_KEY.get(area_key)
    if area is None:
        return []
    items = list(area["sidebar"])
    if area_key != "teaching" or not scoped_facilitator:
        return items
    excluded_paths = {
        "/instructor/announcements",
        "/success-queue",
        "/instructor/labs",
    }
    if not has_cohort_scope:
        excluded_paths.update(
            {
                "/instructor/cohorts",
                "/instructor/reports",
                "/instructor/gradebook",
            }
        )
    return [item for item in items if item["path"] not in excluded_paths]
