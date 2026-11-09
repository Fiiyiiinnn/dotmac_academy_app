"""Fixture-only Alembic environment: caller supplies connection and revisions."""

from alembic import context

config = context.config
connection = config.attributes["connection"]
if context.is_offline_mode():
    # SQL rendering only: reuse the supplied target dialect without connecting.
    context.configure(
        url=connection.engine.url,
        target_metadata=None,
        literal_binds=True,
        version_table_schema="public",
        transactional_ddl=True,
    )
else:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema="public",
        transactional_ddl=True,
        on_version_apply=config.attributes["on_version_apply"],
    )
with context.begin_transaction():
    context.run_migrations()
