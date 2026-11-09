"""This assembly's answers to "which revision supplies that effect?".

A module lineage declares the database effects it needs
(`ModuleManifest.requires`, `dotmac_kernel.prerequisites`). It never names a
foreign revision, because the answer differs per assembly. This file is where
THIS assembly answers. It is the same decision as `alembic.ini`'s
`version_locations` — which lineages are composed, and which of their revisions
supply the shared effects — and lives in the assembly package rather than beside
`alembic.ini` only because `alembic` is the installed distribution's name.

The reference assembly runs the kernel lineage, so both answers are kernel
`0001`. That is not the interesting case — the point of the indirection is that
ERP, which hosts `public.tenants` in its own lineage and structurally cannot run
kernel `0001`, writes a different file here and installs the same modules.

Binding is not belief: `require_prerequisites` re-proves each effect against the
live catalog before the requiring migration runs, and the order canary requires
the named revision to be present in `alembic_version`. A wrong entry here fails
at `alembic upgrade`, before any DDL.
"""

from __future__ import annotations

from typing import Final

from dotmac_kernel.prerequisites import (
    IDEMPOTENCY_LEDGER_V1,
    MODULE_DATABASE_ROLES_V1,
    OUTBOX_RELAY_V1,
    PARTY_PERSON_CATALOG_V1,
    PLATFORM_AUDIT_LOG_V1,
    TENANT_AUDIT_LOG_V1,
    TENANT_SCOPE_CATALOG_V1,
    PrerequisiteBinding,
)

#: Kernel `0001` creates `tenants`/`tenant_domains` and `app_current_tenant_id()`
#: (`_create_tenants_table`, `_create_current_tenant_function`) and the three
#: database roles (`_ensure_roles`). It supplies far more than this — people,
#: credentials, sessions, RBAC, audit — which is exactly why a module requiring
#: a foreign-key target declares the effect rather than the revision.
ASSEMBLY_PREREQUISITE_BINDINGS: Final[tuple[PrerequisiteBinding, ...]] = (
    PrerequisiteBinding(
        prerequisite=TENANT_SCOPE_CATALOG_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    PrerequisiteBinding(
        prerequisite=MODULE_DATABASE_ROLES_V1.name,
        provider_revision="0001_initial_tenant_schema",
        provider_owner="kernel",
    ),
    # Kernel `0003` replaces the former Person table with Party plus the person
    # subtype and applies the inherited RLS/grant contract.  A module names the
    # effect; this assembly alone names the physical provider revision.
    PrerequisiteBinding(
        prerequisite=PARTY_PERSON_CATALOG_V1.name,
        provider_revision="0003_party_identity",
        provider_owner="kernel",
    ),
    # The at-most-once ledger arrives later in the same lineage — kernel `0018`
    # created `idempotency_records`/`platform_idempotency_records` (ADR-0014),
    # replacing the WS3 inbox tables. A binding names the revision that
    # SUPPLIES the effect, not the lineage's root, so this one is `0018`: bind
    # it to `0001` and the order canary would pass against a database stopped
    # at `0017`, where the tables do not exist.
    PrerequisiteBinding(
        prerequisite=IDEMPOTENCY_LEDGER_V1.name,
        provider_revision="0018_idempotency_one_owner",
        provider_owner="kernel",
    ),
    # The relay is assembled across THREE kernel revisions and the binding names
    # the last of them, for the same reason the ledger names `0018` rather than
    # the lineage root: `0008` created `outbox_events`, `0011` added the lease
    # columns, the reclaim index, the `outbox_dispatcher` role and the tenant
    # SECURITY DEFINER pair, and `0012` added the entire platform peer — table,
    # indexes, `platform_outbox_dispatcher` and its own function pair. Only
    # after `0012` is the effect whole. Bound to `0011`, a database stopped
    # there would pass the order canary with no platform plane at all, which is
    # precisely the half `verify_outbox_relay` would then refuse — loudly, but
    # one layer later than it should.
    PrerequisiteBinding(
        prerequisite=OUTBOX_RELAY_V1.name,
        provider_revision="0012_platform_outbox",
        provider_owner="kernel",
    ),
    # Kernel `0009` created `platform_audit_events`, but that historical shape
    # still let the online platform role rewrite and delete rows.  Revision
    # `0026` completes the named append-only effect by reducing that role to
    # SELECT + INSERT and removing column-level mutation paths.  Bind the
    # effect to the revision that makes the complete contract true.
    PrerequisiteBinding(
        prerequisite=PLATFORM_AUDIT_LOG_V1.name,
        provider_revision="0026_platform_audit_log",
        provider_owner="kernel",
    ),
    # Current tenant-audit history width and nullable historical persistence time
    # arrive in 0029. A stamp or the original table alone is insufficient.
    PrerequisiteBinding(
        prerequisite=TENANT_AUDIT_LOG_V1.name,
        provider_revision="0029_audit_history_shape",
        provider_owner="kernel",
    ),
)

__all__ = ["ASSEMBLY_PREREQUISITE_BINDINGS"]
