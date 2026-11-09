"""Make lab-instance runtime state worker-owned.

Revision ID: 0056_lab_instance_worker
Revises: 0055_lab_operations
"""

from __future__ import annotations

from alembic import op

revision = "0056_lab_instance_worker"
down_revision = "0055_lab_operations"
branch_labels = None
depends_on = None

TABLE = "lab_instances"
OPERATIONS_TABLE = "lab_operations"


def upgrade() -> None:
    # Phase 1 deliberately left these enums unconstrained while the worker
    # contract was still settling. The worker now exists, so make its defensive
    # application checks a database invariant too.
    #
    # These are added NOT VALID + validated as separate statements rather than
    # a single ``op.create_check_constraint`` so the constraint is visible to
    # planners/other sessions immediately without holding the stronger lock a
    # validated add-and-check requires for the whole scan. That split only
    # matters if each half actually gets its own transaction, though: alembic's
    # env.py wraps the whole migration in one ``context.begin_transaction()``,
    # and Postgres locks are transaction-scoped, so two ``op.execute()`` calls
    # in the same migration would still hold the ADD's AccessExclusiveLock
    # through the VALIDATE scan — identical to a single validated ADD. Each
    # statement below therefore runs in its own ``autocommit_block()``, which
    # commits the ambient transaction, runs in autocommit mode, and resumes
    # afterward — this is the one place in this repo that needs it; don't
    # "simplify" it back to ``create_check_constraint`` or a plain ``execute``.
    # Each autocommit_block durably commits before alembic records this
    # revision as applied (that only happens at the very end of upgrade()), so
    # a later statement failing here (e.g. a GRANT on a table absent in some
    # environment, or VALIDATE catching a real data violation) would abort the
    # migration while the ADD already durably exists — a rerun would then
    # hard-fail on a duplicate constraint. The ADD is guarded with an
    # existence check so a rerun is a no-op instead of a manual-recovery
    # incident. The guard checks ``conrelid`` alongside ``conname`` because
    # Postgres constraint names are only unique per-table, not globally — a
    # same-named constraint on an unrelated table would otherwise make this
    # guard wrongly skip the ADD here. VALIDATE and the GRANTs below are NOT
    # guarded: Postgres already makes re-validating an already-valid
    # constraint and re-granting an already-held privilege no-ops on their
    # own.
    with op.get_context().autocommit_block():
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
            "'ck_lab_operations_kind' AND conrelid = 'lab_operations'::regclass) THEN "
            "ALTER TABLE lab_operations ADD CONSTRAINT ck_lab_operations_kind "
            "CHECK (kind IN ('deploy', 'destroy', 'check')) NOT VALID; "
            "END IF; END $$;"
        )
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE lab_operations VALIDATE CONSTRAINT ck_lab_operations_kind;")
    with op.get_context().autocommit_block():
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
            "'ck_lab_operations_state' AND conrelid = 'lab_operations'::regclass) THEN "
            "ALTER TABLE lab_operations ADD CONSTRAINT ck_lab_operations_state "
            "CHECK (state IN ('queued', 'claimed', 'succeeded', 'failed', 'cancelled')) "
            "NOT VALID; "
            "END IF; END $$;"
        )
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE lab_operations VALIDATE CONSTRAINT ck_lab_operations_state;")
    # The web tier may create a queued instance request and observe it. Runtime
    # state (status/consoles/error/timestamps) is written only by the dedicated
    # academy_lab_worker role. app_admin remains the migration/offline role;
    # platform_api has no lab-instance mutation responsibility.
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user, platform_api;")
    op.execute(
        f"GRANT INSERT (id, tenant_id, activity_id, person_id, instance_name, seed) "
        f"ON {TABLE} TO app_user;"
    )
    # The non-owner worker needs only to observe instances and update runtime
    # state. It does not create or delete instance rows.
    op.execute(f"GRANT SELECT, UPDATE ON {TABLE} TO academy_lab_worker;")
    # Read-only supporting data the worker looks up while executing operations
    # (templates, effective settings, activities/people for grading and
    # notification, tenants for cross-tenant reconciliation).
    op.execute(
        "GRANT SELECT ON lab_templates, platform_settings, activities, people, tenants "
        "TO academy_lab_worker;"
    )
    # Consequences the worker writes as part of settling a check operation:
    # the graded submission/score, the learning-events ledger entry, the
    # in-app notification, and the outbound email intent.
    op.execute(
        "GRANT SELECT, INSERT ON submissions, scores, learning_events, notifications, "
        "email_outbox TO academy_lab_worker;"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api, academy_lab_worker;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_user, platform_api;")
    # Revoke only what this migration granted the dedicated worker on supporting
    # tables — never REVOKE ALL, since earlier migrations may grant other
    # roles their own privileges on these same tables.
    op.execute(
        "REVOKE SELECT, INSERT ON submissions, scores, learning_events, notifications, "
        "email_outbox FROM academy_lab_worker;"
    )
    op.execute(
        "REVOKE SELECT ON lab_templates, platform_settings, activities, people, tenants "
        "FROM academy_lab_worker;"
    )
    op.drop_constraint("ck_lab_operations_state", OPERATIONS_TABLE, type_="check")
    op.drop_constraint("ck_lab_operations_kind", OPERATIONS_TABLE, type_="check")
