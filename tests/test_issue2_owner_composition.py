"""Maintained port of legacy_probe/test_ordinary_owner_composition.py (issue #2).

DISPOSITIONS: test_owner_composition_exact_declared_owners. v2 census
re-derived: ``platform_settings`` is superseded by kernel
``domain_settings`` (kernel-owned, not product); ``lab_result_deliveries``
is product-owned in v2. Product table count remains 19.

Genuine factory/owner checks for hosted CI; never executed locally.

These require the qualified installed Kernel/Learning successors and the complete
Academy source/runtime composition. They do not substitute packages or bypass
factory security validation, startup validation, or registry installation.
"""

from dataclasses import replace

import pytest
from dotmac_kernel.api_documentation import environment_api_documentation_policy
from dotmac_kernel.audit_actions import active_audit_actions
from dotmac_kernel.modules import DuplicateModuleError, ModuleRegistry
from dotmac_learning.manifest import module as learning_module

from app.admissions_declarations import APPLICANT_TRANSITION_ACTION
from app.api_identity_declarations import API_IDENTITY_AUDIT_ACTIONS
from app.assembly import OWNER_DECLARATIONS, academy_feature, assembly
from app.canonical_models import ModelOwnershipError, kernel_owned_tables, physical_key, product_owned_tables
from app.config import Settings, settings
from app.invitation_declarations import INVITATION_COMPLETED_ACTION
from app.kernel_runtime import AcademyInstrumentationMiddleware, create_academy_app
from app.owner_composition import inspect_owner_registry

LEARNING_ACTIONS = (
    "scheduling.update_session",
    "success_queue.acknowledge",
    "success_queue.assign",
    "success_queue.resolve",
    "success_queue.message_segment",
)


def test_ordinary_factory_uses_all_actual_owner_declarations():
    app = create_academy_app()
    registry = app.state.module_registry
    assert isinstance(registry, ModuleRegistry)
    assert registry.startup_order() == ModuleRegistry(OWNER_DECLARATIONS).startup_order()
    assert assembly.modules == OWNER_DECLARATIONS
    assert registry.get("learning") is learning_module
    assert registry.codes() == {
        "academy",
        "learning",
        "academy_invitations",
        "academy_admissions",
        "academy_reminders",
    }
    assert any(middleware.cls is AcademyInstrumentationMiddleware for middleware in app.user_middleware)
    graph = inspect_owner_registry(registry, declarations=OWNER_DECLARATIONS)
    assert {key for key, owner in graph.owners.items() if owner == "learning"} == {
        (learning_module.db_schema, name) for name in (*learning_module.tables, *learning_module.platform_tables)
    }
    products = product_owned_tables()
    # Reviewed literal inventory of the product-owned tables (not derived from the code under test).
    assert {physical_key(table) for table in products} == {
        ("public", name)
        for name in (
            "academy_account_profiles",
            "academy_credential_policies",
            "academy_erp_completion_checkpoints",
            "academy_erp_imported_completion_keys",
            "account_tokens",
            "applicants",
            "email_outbox",
            "facilitator_nominations",
            "facilitator_requirements",
            "lab_console_activity",
            "lab_instances",
            "lab_operations",
            "lab_templates",
            "lab_result_deliveries",
            "notifications",
            "onboarding_tasks",
            "reminder_log",
            "reminder_preferences",
            "tenant_entrance_defaults",
        )
    }
    assert len(products) == 19
    assert {key for key, owner in graph.owners.items() if owner == "academy"} == {
        physical_key(table) for table in products
    }
    assert {key for key, owner in graph.owners.items() if owner == "kernel"} == {
        physical_key(table) for table in kernel_owned_tables()
    }
    assert graph.owners[("public", "tenants")] == "kernel"
    assert all(table.metadata is not graph.metadata for table in products)
    assert len(graph.metadata.tables) == len(graph.owners)
    assert all(
        fk.column.table.metadata is graph.metadata
        for table in graph.metadata.tables.values()
        for fk in table.foreign_keys
    )
    actions = active_audit_actions()
    assert actions.owner("identity.history_imported") == "kernel"
    assert all(actions.owner(action) == "academy" for action in API_IDENTITY_AUDIT_ACTIONS)
    assert actions.owner(INVITATION_COMPLETED_ACTION) == "academy_invitations"
    assert actions.owner(APPLICANT_TRANSITION_ACTION) == "academy_admissions"
    assert all(actions.owner(action) == "learning" for action in LEARNING_ACTIONS)
    assert not set(LEARNING_ACTIONS).intersection(academy_feature.audit_actions)


def test_disabled_learning_retains_tables_migration_owner_and_audit_vocabulary():
    spec = replace(assembly, disabled_modules=frozenset({"learning", "academy"}), web_enabled=False)
    app = create_academy_app(spec)
    registry = app.state.module_registry
    assert registry.get("learning") is learning_module
    assert "learning" not in registry.enabled_codes(spec.disabled_modules)
    graph = inspect_owner_registry(registry, declarations=OWNER_DECLARATIONS)
    assert len([owner for owner in graph.owners.values() if owner == "learning"]) == len(learning_module.tables)
    assert learning_module.migration_owner() in registry.namespaces().owners()
    assert all(active_audit_actions().owner(action) == "learning" for action in LEARNING_ACTIONS)
    assert registry.get("learning").permissions == learning_module.permissions
    assert registry.get("learning").requires == learning_module.requires
    assert not any(getattr(route, "path", "").startswith("/admin") for route in app.routes)


def test_factory_refuses_missing_or_changed_installed_owner():
    without_learning = tuple(item for item in assembly.modules if item is not learning_module)
    with pytest.raises(ModelOwnershipError, match="differs from declared Academy owners"):
        create_academy_app(replace(assembly, modules=without_learning))
    changed_learning = replace(learning_module, tables=learning_module.tables[:-1])
    changed = tuple(changed_learning if item is learning_module else item for item in assembly.modules)
    with pytest.raises(ModelOwnershipError, match="differs from declared Academy owners"):
        create_academy_app(replace(assembly, modules=changed))


def test_factory_refuses_duplicate_owner_declaration():
    with pytest.raises(DuplicateModuleError):
        create_academy_app(replace(assembly, modules=(*assembly.modules, learning_module)))


def test_ordinary_spec_requires_product_database_and_settings_documentation_policy():
    from app.database_runtime import get_academy_database_runtime

    assert assembly.database_runtime is get_academy_database_runtime()
    assert assembly.require_database_runtime is True
    assert assembly.api_documentation == environment_api_documentation_policy(settings.environment)
    assert assembly.disabled_modules == frozenset(settings.disabled_feature_set)
    assert assembly.web_enabled is settings.web_enabled
    assert assembly.startup_checks
    assert assembly.startup_hooks


@pytest.mark.parametrize("environment", ["dev", "test", "production", "staging", ""])
def test_real_factory_consumes_explicit_settings_backed_documentation_policy(environment):
    configured = Settings(_env_file=None, environment=environment)
    policy = environment_api_documentation_policy(configured.environment)
    app = create_academy_app(replace(assembly, api_documentation=policy))
    from dotmac_kernel.api_documentation import audit_api_documentation

    assert not audit_api_documentation(app, policy)
