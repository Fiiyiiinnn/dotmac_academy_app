"""applicants: evaluable profile + entrance-exam deadline + invite tracking.

Three gaps this closes in the applicant flow:

1. The application captured only name/email/phone/cohort — nothing you could
   actually *evaluate* a candidate on. Adds the profile fields an admissions
   decision needs (education, experience, location, practical readiness).

2. The exam link was rendered ONCE on the response page. Close the tab and the
   token was gone forever, with no email and no way back in — which is why 180
   applicants produced zero sittings. ``invite_sent_at`` tracks the emailed
   invitation.

3. ``assessment_deadline`` implements the deadline model: the link stays valid
   until a fixed date, so a candidate can pick their own good-connectivity
   moment rather than being pinned to a slot.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035_applicant_profile"
down_revision = "0034_entrance_resilience"
branch_labels = None
depends_on = None

# (column, type) — the evaluable application profile.
PROFILE = [
    ("date_of_birth", sa.Date()),
    ("state", sa.String(length=60)),
    ("city", sa.String(length=60)),
    ("highest_qualification", sa.String(length=60)),
    ("field_of_study", sa.String(length=120)),
    ("years_experience", sa.Integer()),
    ("current_role", sa.String(length=120)),
    ("has_device", sa.Boolean()),  # laptop or smartphone to study on
    ("has_internet", sa.Boolean()),  # reliable enough to sit a timed exam
    ("can_work_at_height", sa.Boolean()),  # a real screen for fibre field work
    ("available_from", sa.Date()),
    ("heard_from", sa.String(length=60)),
    ("cv_url", sa.String(length=500)),
]


def upgrade() -> None:
    for name, type_ in PROFILE:
        op.add_column("applicants", sa.Column(name, type_, nullable=True))
    op.add_column("applicants", sa.Column("assessment_deadline", sa.DateTime(timezone=True), nullable=True))
    op.add_column("applicants", sa.Column("invite_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("applicants", "invite_sent_at")
    op.drop_column("applicants", "assessment_deadline")
    for name, _ in reversed(PROFILE):
        op.drop_column("applicants", name)
