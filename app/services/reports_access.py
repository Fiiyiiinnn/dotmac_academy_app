"""Academy product policy for the instance reports portal."""

# Ported at new-composition genesis (presentation-coupled wave).

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services.facilitator_access import facilitator_access
from app.services.identity import AcademyCurrentAccount
from app.services.roles import role_slugs
from app.services.settings_store import effective
from app.services.web_auth import require_web_user


def require_reports_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    """Compose live cookie identity, tenant-local grants and fresh stored policy."""
    tenant = require_tenant(request)
    policy = effective(db, require_store=True)
    grants = role_slugs(db, tenant.id, account.id)
    if not set(policy.reports_access_roles).intersection(grants):
        # Scoped facilitators teach from an active nomination, not a staff
        # grant; per-cohort scoping is enforced inside the handlers.
        if not facilitator_access(db, tenant_id=tenant.id, person_id=account.id).can_teach:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return account
