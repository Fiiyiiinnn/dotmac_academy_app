"""Academy's process-local database runtime, constructed only when requested."""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

from threading import Lock

from dotmac_kernel.session_runtime import DatabaseRuntime, bind_database_runtime

from app.config import settings

_runtime: DatabaseRuntime | None = None
_runtime_lock = Lock()
_identity_assembly = None
_identity_assembly_lock = Lock()


def get_academy_database_runtime() -> DatabaseRuntime:
    """Build one Kernel runtime from the product's deployment settings.

    Engine construction is lazy and opens no connections. The lock also keeps
    concurrent first callers from constructing competing engine/session pools.
    Pool sizing and the canonical tenant setting remain Kernel-owned defaults.
    """
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = DatabaseRuntime.from_urls(
                database_url=settings.database_url,
                hide_parameters=True,
                platform_database_url=settings.platform_database_url or settings.database_url,
            )
        return _runtime


def bind_academy_database_runtime() -> DatabaseRuntime:
    """Seal the product binding before any session use; conflicts propagate."""
    from dotmac_kernel.audit_actions import AuditActionRegistry, install_audit_actions
    from dotmac_kernel.identity_provisioning import IdentityProvisioningAssemblyV1, install_identity_provisioning

    from app.owner_composition import compose_migration_owner_declarations
    from app.services.bootstrap_identity import AcademyBootstrapProductPort

    global _identity_assembly
    runtime = get_academy_database_runtime()
    bind_database_runtime(runtime, required=True)
    with _identity_assembly_lock:
        if _identity_assembly is None:
            candidate = IdentityProvisioningAssemblyV1(
                runtime=runtime,
                source_application="dotmac_academy_app",
                product=AcademyBootstrapProductPort(),
            )
            # Same genuine installed owners as migration/web composition. Kernel
            # reserves only identity.history_imported and credential.password.assigned;
            # the Academy feature declares the other credential lifecycle actions.
            install_audit_actions(AuditActionRegistry.from_manifests(compose_migration_owner_declarations()))
            install_identity_provisioning(candidate)
            _identity_assembly = candidate
        else:
            install_identity_provisioning(_identity_assembly)
    return runtime
