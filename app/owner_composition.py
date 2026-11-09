"""Installed owner declarations and inspection; no DDL or copied ORM mappings."""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

from collections.abc import Sequence

from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.modules import AnyManifest, ModuleRegistry
from dotmac_learning.manifest import module as learning_module

from app.admissions_declarations import ADMISSIONS_AUDIT_DECLARATION
from app.canonical_models import (
    InspectionGraph,
    ModelOwnershipError,
    inspect_canonical_models,
    kernel_owned_tables,
    product_owned_tables,
)
from app.invitation_declarations import INVITATION_AUDIT_DECLARATION
from app.reminder_declarations import REMINDER_AUDIT_DECLARATION


def compose_owner_declarations(academy_feature: FeatureManifest) -> tuple[AnyManifest, ...]:
    """Compose genuine installed declarations, without copying their vocabulary.

    Importing Learning's actual manifest imports its five actual model groups.
    Kernel built-in audit actions remain Kernel-owned; this product declares only
    its own feature actions. Disabled owners stay in the installed tuple.
    """
    return (
        academy_feature,
        learning_module,
        INVITATION_AUDIT_DECLARATION,
        ADMISSIONS_AUDIT_DECLARATION,
        REMINDER_AUDIT_DECLARATION,
    )


def inspect_owner_registry(
    registry: ModuleRegistry,
    *,
    declarations: Sequence[AnyManifest],
) -> InspectionGraph:
    """Check the actual public registry against the declared installed owners.

    Walk startup_order, never enabled_order: a deployment switch cannot remove
    model ownership, migrations, audit vocabulary or security declarations.
    The returned copied metadata is inspection-only and is never registered.
    """
    if not isinstance(registry, ModuleRegistry):
        raise ModelOwnershipError("an actual installed ModuleRegistry is required")
    expected = ModuleRegistry(declarations)
    if registry.startup_order() != expected.startup_order():
        raise ModelOwnershipError("installed registry differs from declared Academy owners")
    return inspect_canonical_models(
        registry,
        kernel_tables=kernel_owned_tables(),
        product_tables=product_owned_tables(),
    )


def compose_migration_owner_declarations() -> tuple[AnyManifest, ...]:
    """Use the same installed declarations without importing web routes."""
    from app.academy_declarations import ACADEMY_FEATURE_DECLARATION

    return compose_owner_declarations(ACADEMY_FEATURE_DECLARATION)
