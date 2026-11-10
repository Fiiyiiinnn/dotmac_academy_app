"""Pure declared-owner inspection graph. Never a DDL or runtime-session owner.

The caller supplies the actual installed ModuleRegistry after importing installed
manifests and their model groups. ModuleManifest has no model-loader API: its
actual tables/platform_tables/db_schema declarations are the ownership authority.
Disabled installed modules remain part of this graph.
"""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import MappingProxyType
from typing import Mapping, Sequence, cast

import sqlalchemy as sa
from dotmac_kernel import SUPPORTED_MODULES
from dotmac_kernel.models import Base as KernelBase
from dotmac_kernel.modules import ModuleRegistry


class ModelOwnershipError(ValueError):
    """The source declarations do not define one closed physical table graph."""


@dataclass(frozen=True)
class OwnedTable:
    owner: str
    table: sa.Table


@dataclass(frozen=True)
class InspectionGraph:
    metadata: sa.MetaData
    owners: Mapping[tuple[str, str], str]


def physical_key(table: sa.Table) -> tuple[str, str]:
    return (table.schema or "public", table.name)


# Actual f674 reference registration imports plus supported public model owners.
# This is an Academy declaration list, not an invented Kernel registration API.
KERNEL_MODEL_MODULES = (
    "dotmac_kernel.models",
    "dotmac_kernel.models_platform",
    "dotmac_kernel.audit",
    "dotmac_kernel.entitlements",
    "dotmac_kernel.idempotency_models",
    "dotmac_kernel.settings_models",
    "dotmac_kernel.messaging.models",
    "dotmac_kernel.machine_models",
    "dotmac_kernel.consent_models",
    "dotmac_kernel.delivery_models",
    "dotmac_kernel.flag_models",
)


def kernel_owned_tables() -> tuple[sa.Table, ...]:
    """Register supported Kernel owners and collect their actual declarations."""
    tables: dict[tuple[str, str], sa.Table] = {}
    for name in KERNEL_MODEL_MODULES:
        if name not in SUPPORTED_MODULES:
            raise ModelOwnershipError(f"unsupported Kernel model owner: {name}")
        module = import_module(name)
        for value in vars(module).values():
            table = None
            if isinstance(value, type) and value.__module__ == name:
                table = getattr(value, "__table__", None)
            elif isinstance(value, sa.Table):
                # Include genuine standalone Core declarations as well as ORM.
                table = value
            if table is not None:
                key = physical_key(table)
                if key in tables and tables[key] is not table:
                    raise ModelOwnershipError(f"duplicate Kernel declaration: {key}")
                tables[key] = table
    return tuple(tables.values())


def product_owned_tables() -> tuple[sa.Table, ...]:
    """Explicit owners; copied identity FK references never enter this list."""
    from app.models.account_token import AccountToken
    from app.models.admissions import Applicant
    from app.models.email_outbox import EmailOutbox
    from app.models.entrance_defaults import TenantEntranceDefaults
    from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint, ERPImportedCompletionKey
    from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
    from app.models.lab import LabConsoleActivity, LabInstance, LabOperation, LabTemplate
    from app.models.lab_result_delivery import LabResultDelivery
    from app.models.notification import Notification
    from app.models.oidc_login import AcademyOidcLoginState
    from app.models.onboarding import OnboardingTask
    from app.models.reminder import ReminderLog, ReminderPreference

    return tuple(
        cast(sa.Table, model.__table__)
        for model in (
            AccountToken,
            Applicant,
            EmailOutbox,
            TenantEntranceDefaults,
            LabInstance,
            LabOperation,
            LabTemplate,
            LabConsoleActivity,
            LabResultDelivery,
            Notification,
            OnboardingTask,
            # legacy platform_settings is kernel DomainSetting here (kernel-owned)
            ReminderLog,
            ReminderPreference,
            ERPCompletionCheckpoint,
            ERPImportedCompletionKey,
            AcademyAccountProfile,
            AcademyCredentialPolicy,
            AcademyOidcLoginState,
            FacilitatorNomination,
            FacilitatorRequirement,
        )
    )


def declared_ownership(
    registry: ModuleRegistry,
    *,
    kernel_tables: Sequence[sa.Table],
    product_tables: Sequence[sa.Table] | None = None,
) -> tuple[OwnedTable, ...]:
    """Bind actual manifests and explicit Kernel/product declarations before copy.

    Kernel ownership comes only from the explicit public declaration modules,
    never from all KernelBase.metadata after installed modules share that Base.
    An explicit pre-registration snapshot may be supplied by the caller.
    """
    if not isinstance(registry, ModuleRegistry):
        raise ModelOwnershipError("an actual installed ModuleRegistry is required")
    table: sa.Table | None
    for table in kernel_tables:
        if table.metadata is not KernelBase.metadata or physical_key(table)[0] != "public":
            raise ModelOwnershipError(f"noncanonical Kernel table input: {physical_key(table)}")
    declarations = [OwnedTable("kernel", table) for table in kernel_tables]
    for manifest in registry.startup_order():
        for name in (*manifest.tables, *manifest.platform_tables):
            key = (manifest.db_schema or "public", name)
            table = KernelBase.metadata.tables.get(".".join(key))
            if table is None and key[0] == "public":
                table = KernelBase.metadata.tables.get(name)
            if table is None:
                raise ModelOwnershipError(f"unregistered declared module table: {manifest.code}:{key}")
            declarations.append(OwnedTable(manifest.code, table))
    products = product_owned_tables() if product_tables is None else product_tables
    declarations.extend(OwnedTable("academy", table) for table in products)
    claimed = {id(declaration.table) for declaration in declarations}
    # Shared Base is only a completeness check, never an ownership source.
    for table in KernelBase.metadata.tables.values():
        if id(table) not in claimed:
            raise ModelOwnershipError(f"unclaimed registered table: {physical_key(table)}")
    return tuple(declarations)


def compose_inspection_graph(declarations: Sequence[OwnedTable]) -> InspectionGraph:
    """Validate a closed owner graph and copy each owned table exactly once."""
    from dotmac_learning.manifest import module as learning_manifest

    legacy_learning = {("public", name) for name in learning_manifest.tables}
    owned: dict[tuple[str, str], OwnedTable] = {}
    for declaration in declarations:
        key = physical_key(declaration.table)
        if key in owned:
            raise ModelOwnershipError(f"duplicate physical table owner: {key}")
        if key in {("public", "people"), ("public", "person_roles")}:
            raise ModelOwnershipError(f"retired legacy identity mapping: {key}")
        if key in legacy_learning or (declaration.owner == "learning" and key[0] != "mod_learning"):
            raise ModelOwnershipError(f"legacy Learning mapping: {key}")
        if declaration.owner == "kernel" and key[0] != "public":
            raise ModelOwnershipError(f"module table claimed as Kernel owned: {key}")
        owned[key] = declaration
    # Resolve all source targets before copying: an absent owner is not repaired
    # by pulling a copied reference table into ownership.
    for key, declaration in owned.items():
        for fk in declaration.table.foreign_keys:
            try:
                column = fk.column
            except (sa.exc.NoReferencedTableError, sa.exc.NoReferencedColumnError) as exc:
                raise ModelOwnershipError(f"unresolved foreign key on {key}") from exc
            target = owned.get(physical_key(column.table))
            if target is None or column.name not in target.table.c:
                raise ModelOwnershipError(f"foreign key target has no declared owner: {key}")
    metadata = sa.MetaData()
    for declaration in owned.values():
        declaration.table.to_metadata(
            metadata,
            schema=physical_key(declaration.table)[0],
            referred_schema_fn=lambda table, schema, constraint, referred: referred or "public",
        )
    # Force copied FK resolution too; references spanning separate source
    # metadata collections must all land in the one inspection collection.
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            try:
                column = fk.column
            except (sa.exc.NoReferencedTableError, sa.exc.NoReferencedColumnError) as exc:
                raise ModelOwnershipError(f"unresolved copied foreign key on {physical_key(table)}") from exc
            if column.table.metadata is not metadata:
                raise ModelOwnershipError("copied FK escaped inspection metadata")
    return InspectionGraph(metadata, MappingProxyType({key: value.owner for key, value in owned.items()}))


def inspect_canonical_models(
    registry: ModuleRegistry,
    *,
    kernel_tables: Sequence[sa.Table],
    product_tables: Sequence[sa.Table] | None = None,
) -> InspectionGraph:
    """Inspection only; the caller owns actual model registration and startup."""
    return compose_inspection_graph(
        declared_ownership(
            registry,
            kernel_tables=kernel_tables,
            product_tables=product_tables,
        )
    )
