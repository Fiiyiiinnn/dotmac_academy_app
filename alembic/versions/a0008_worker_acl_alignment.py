"""Worker ACL alignment with the legacy privilege matrix (a0008).

Closes the divergences found by the issue #2 disposition audit
against the production 0067 worker posture:

- the worker's reconcile paths enqueue repair operations, so
  `lab_operations` needs INSERT (legacy matrix grants it);
- the worker reads its strict execution policy through
  `settings_store`, so it needs SELECT on kernel `domain_settings`
  (legacy read `platform_settings`; storage superseded, read stays);
- facilitator activation runs from the app_admin sweep session
  (`app/cli.py` reminders-sweep) and web request roles, never from
  the lab worker — revoke the a0007 worker EXECUTE widening.
"""

from __future__ import annotations

from alembic import op

revision = "a0008_worker_acl_alignment"
down_revision = "a0007_facilitator_activation"
branch_labels = None
depends_on = None

_SIGNATURE = "public.activate_eligible_facilitator_nominations(uuid, uuid, uuid, uuid, integer, uuid)"


def upgrade() -> None:
    op.execute("GRANT INSERT ON TABLE public.lab_operations TO academy_lab_worker")
    op.execute("GRANT SELECT ON TABLE public.domain_settings TO academy_lab_worker")
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_SIGNATURE} FROM academy_lab_worker")


def downgrade() -> None:
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO academy_lab_worker")
    op.execute("REVOKE SELECT ON TABLE public.domain_settings FROM academy_lab_worker")
    op.execute("REVOKE INSERT ON TABLE public.lab_operations FROM academy_lab_worker")
