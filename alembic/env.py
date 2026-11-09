"""Alembic environment — the Academy assembly's migration environment.

Connects as app_admin (RLS bypass) — set MIGRATION_DATABASE_URL or
DATABASE_URL.

Composes three lineages: the KERNEL base migrations and the
`dotmac-learning` `lr` lineage (both shipped as installed package data,
resolved here from the distributions), plus this assembly's own
migrations (`alembic/versions`, empty at genesis: product tables arrive
only with the ports that own them).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from dotmac_kernel import (
    audit,  # noqa: F401  (register AuditEvent for autogenerate)
    entitlements,  # noqa: F401  (register TenantEntitlementGrant)
    idempotency_models,  # noqa: F401  (register Idempotency records)
    models_platform,  # noqa: F401  (register PlatformAdmin/PlatformSession)
    settings_models,  # noqa: F401  (register DomainSetting)
)
from dotmac_kernel.messaging import (
    models as messaging_models,  # noqa: F401  (register OutboxEvent peers)
)
from dotmac_kernel.migrations.configuration import (
    select_migration_database_url,
    set_alembic_database_url,
)
from dotmac_kernel.models import Base
from dotmac_kernel.planes import install_module_plane_selections
from dotmac_kernel.prerequisites import install_prerequisite_bindings
from sqlalchemy import engine_from_config, pool

from alembic import context
from app.assembly import assembly
from app.migration_bindings import ASSEMBLY_PREREQUISITE_BINDINGS

config = context.config
set_alembic_database_url(
    config,
    database_url=select_migration_database_url(
        migration_database_url=os.getenv("MIGRATION_DATABASE_URL"),
        fallback_database_url=os.getenv("DATABASE_URL"),
    ),
)

# Installed BEFORE the revision map is built: the composed learning
# lineage resolves its `depends_on` from these bindings at script-load
# time. See `app/migration_bindings.py`.
install_prerequisite_bindings(ASSEMBLY_PREREQUISITE_BINDINGS)
install_module_plane_selections(config.attributes.get("module_plane_selections", assembly.module_planes))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
