"""Merge the parallel migration branches: cohort tracks (0036, born on prod)
and the zero-admin admissions chain (0038/0039, born on main).

No schema ops — this converges the two heads so `upgrade head` works on both
databases that applied 0036 first (prod) and fresh databases building the
whole graph.
"""

from __future__ import annotations

revision = "0040_merge_tracks_admissions"
down_revision = ("0036_tracks", "0039_auto_accept_threshold")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
