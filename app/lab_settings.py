"""Effective lab capacity settings seam.

The legacy app merged DB-stored operator settings over env defaults
(`settings_store.effective`). In this composition, operator settings
are kernel-owned and that merge ports with the settings slice; until
then the env-backed product config is the whole truth. The signature
keeps the legacy call shape so ported services do not change.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings


@dataclass(frozen=True)
class EffectiveLabSettings:
    max_concurrent_labs: int
    max_concurrent_labs_per_tenant: int


def effective(db: Session | None = None) -> EffectiveLabSettings:
    del db
    return EffectiveLabSettings(
        max_concurrent_labs=settings.max_concurrent_labs,
        max_concurrent_labs_per_tenant=settings.max_concurrent_labs_per_tenant,
    )
